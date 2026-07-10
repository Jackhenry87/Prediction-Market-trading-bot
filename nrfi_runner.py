"""NRFI/YRFI Martingale runner (real money, tiny).

Once per day (credit-frugal — first-inning odds are billed per game), it fetches
sharp first-inning odds, devigs vs Kalshi's KXMLBRFI, and locks the day's
direction + up to 3 staggered +EV legs (strategy_nrfi.decide). Then, on every
15-min tick, it plays the legs as a 1-2-4 Martingale off FREE Kalshi settlements:
place leg -> when its first inning resolves, win = stop, loss = place the next
leg at the next multiplier. State in nrfi_state.json.

Every order still passes the shared safety gate (longshot floor, exposure caps).
Fails closed: a game it can't confidently match or price is simply skipped.

    python nrfi_runner.py --once
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

import strategy_nrfi as nrfi
from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import ExposureError, current_exposure_usd
from ledger import log_execution
from safety import check_order
from strategy_sports import match_total_game
from strategy_weather import price_cents
from trade_logger import get_logger, setup_logging

log = get_logger("nrfi_runner")

STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nrfi_state.json")
ODDS_BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"
KALSHI_RFI_SERIES = os.getenv("NRFI_SERIES", "KXMLBRFI")
NRFI_BASE_USD = float(os.getenv("NRFI_BASE_USD", "1"))
DECISION_UTC_HOUR = int(os.getenv("NRFI_DECISION_UTC_HOUR", "15"))  # ~11am ET
OPEN = (None, "", "active", "open", "initialized")


def _load():
    try:
        with open(STATE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save(state):
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=2)


def _ts(iso):
    try:
        return int(datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


# ---------------- data ----------------
def _odds_first_inning(key: str) -> list:
    """[{home_team, away_team, commence(unix), fair_yrfi}] — ONE credit-costly
    pass over the day's games (per-event additional market)."""
    out = []
    evs = requests.get(f"{ODDS_BASE}/events", params={"apiKey": key},
                       timeout=25)
    if evs.status_code != 200:
        log.error("odds events failed %s: %s", evs.status_code, evs.text[:200])
        return out
    for ev in evs.json():
        r = requests.get(f"{ODDS_BASE}/events/{ev['id']}/odds",
                         params={"apiKey": key, "regions": "us",
                                 "markets": "totals_1st_1_innings",
                                 "oddsFormat": "decimal"}, timeout=25)
        if r.status_code != 200:
            continue
        probs = []
        for b in r.json().get("bookmakers", []):
            for m in b.get("markets", []):
                if m.get("key") != "totals_1st_1_innings":
                    continue
                over = under = None
                for o in m.get("outcomes", []):
                    if o.get("name") == "Over":
                        over = o.get("price")
                    elif o.get("name") == "Under":
                        under = o.get("price")
                p = nrfi.fair_yrfi(over, under)
                if p is not None:
                    probs.append(p)
        fair = nrfi.consensus_yrfi(probs)
        if fair is None:
            continue
        out.append(dict(home_team=ev.get("home_team"),
                        away_team=ev.get("away_team"),
                        commence=_ts(ev.get("commence_time")), fair_yrfi=fair))
    return out


def _kalshi_rfi(client) -> list:
    """Open KXMLBRFI markets: [{event_title, ticker, yes_ask, yes_bid, status}]."""
    data = client._request("GET", "/events",
                           params={"series_ticker": KALSHI_RFI_SERIES,
                                   "status": "open",
                                   "with_nested_markets": "true", "limit": 200})
    out = []
    for ev in data.get("events", []):
        mks = ev.get("markets") or []
        if not mks:
            continue
        m = mks[0]
        out.append(dict(event_title=ev.get("title", ""), ticker=m.get("ticker"),
                        yes_ask=price_cents(m, "yes_ask"),
                        yes_bid=price_cents(m, "yes_bid"),
                        status=m.get("status")))
    return out


def _build_games(client, key) -> list:
    """Join sharp first-inning odds to Kalshi RFI markets (fail-closed match)."""
    odds = _odds_first_inning(key)
    log.info("first-inning odds for %d game(s)", len(odds))
    games = []
    for k in _kalshi_rfi(client):
        g = match_total_game(k["event_title"], odds)   # exactly-one-match or None
        if not g or k["yes_ask"] is None or k["yes_bid"] is None:
            continue
        games.append(dict(ticker=k["ticker"], commence=g["commence"],
                          fair_yrfi=g["fair_yrfi"], yes_ask=k["yes_ask"],
                          no_ask=100.0 - k["yes_bid"]))
    log.info("matched %d game(s) to sharp first-inning odds", len(games))
    return games


# ---------------- execution ----------------
def _place_leg(client, settings, state, exposure) -> None:
    leg = state["legs"][state["step"]]
    mult = nrfi.stake_mult(state["step"])
    try:
        m = client.get_market(leg["ticker"])
    except Exception as exc:
        log.warning("no market for %s (%s) — retry next tick", leg["ticker"], exc)
        return
    if m.get("status") not in OPEN:
        log.warning("leg %s market not open (%s) — window passed, aborting day.",
                    leg["ticker"], m.get("status"))
        state["status"] = "aborted"
        _save(state)
        return
    direction = state["direction"]
    if direction == "yes":
        price = price_cents(m, "yes_ask")
    else:
        yb = price_cents(m, "yes_bid")
        price = None if yb is None else 100.0 - yb
    if not price or not 0 < price < 100:
        log.info("leg %s: no usable %s quote yet — retry next tick.",
                 leg["ticker"], direction)
        return
    stake = NRFI_BASE_USD * mult
    count = max(1, int(round(stake * 100 / price)))
    problems = check_order(settings, "BUY", price / 100.0, count, exposure)
    if problems:
        for p in problems:
            log.warning("BLOCKED %s: %s", leg["ticker"], p)
        return
    price_c = int(round(price))
    try:
        order = client.create_limit_order(leg["ticker"], direction, "buy",
                                          count, price_c)
    except Exception as exc:
        log.error("place failed %s: %s", leg["ticker"], exc)
        return
    leg["order_id"] = str(order.get("order_id", "") if isinstance(order, dict)
                          else "")
    leg["entry_price"], leg["count"] = price_c, count
    try:
        log_execution("nrfi", leg["ticker"], direction, count, price_c,
                      leg["order_id"])
    except Exception as exc:
        log.warning("ledger write failed: %s", exc)
    log.info("PLACED leg %d/%d (%dx): buy %s %d x %s @ %dc ($%.2f) [%s]",
             state["step"] + 1, len(state["legs"]), mult, direction.upper(),
             count, leg["ticker"], price_c, stake, state["direction"])
    _save(state)


def _check_settlement(client, state) -> None:
    leg = state["legs"][state["step"]]
    try:
        m = client.get_market(leg["ticker"])
    except Exception as exc:
        log.warning("settlement check failed %s: %s", leg["ticker"], exc)
        return
    result = m.get("result")
    if result not in ("yes", "no"):
        return                                    # 1st inning still in progress
    won = (result == state["direction"])
    leg["result"] = "win" if won else "loss"
    if won:
        state["status"] = "done_win"
        log.info("LEG WON (%s) — day complete, Martingale stops. ✅",
                 leg["ticker"])
    else:
        state["step"] += 1
        if state["step"] >= len(state["legs"]):
            state["status"] = "done_loss"
            log.info("LEG LOST (%s) — no legs left, day is a full loss. ❌",
                     leg["ticker"])
        else:
            log.info("LEG LOST (%s) — advancing to leg %d at %dx.",
                     leg["ticker"], state["step"] + 1,
                     nrfi.stake_mult(state["step"]))
    _save(state)


def _decide_today(client, settings, et_date) -> dict:
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        log.error("ODDS_API_KEY not set — cannot decide.")
        return {"date": et_date, "status": "no_bet", "legs": [], "step": 0}
    games = _build_games(client, key)
    d = nrfi.decide(games)
    if not d:
        log.info("No +EV, staggered %s slate today — standing down.",
                 "NRFI/YRFI")
        return {"date": et_date, "status": "no_bet", "legs": [], "step": 0}
    legs = [dict(ticker=l["ticker"], commence=l["commence"], order_id=None,
                 entry_price=None, count=None, result=None) for l in d["legs"]]
    log.info("TODAY: %s on %d game(s): %s", d["direction"].upper(), len(legs),
             ", ".join(l["ticker"] for l in legs))
    return {"date": et_date, "direction": d["direction"], "legs": legs,
            "step": 0, "status": "active"}


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Config error: %s", exc)
        return 1
    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path, settings.kalshi_env)
    state = _load()
    now = datetime.now(timezone.utc)
    et_date = (now - timedelta(hours=4)).date().isoformat()   # ET slate date

    # ---- daily decision: once per ET-day, after odds are reliably posted ----
    if state.get("date") != et_date:
        if now.hour < DECISION_UTC_HOUR:
            log.info("Before decision hour (%02d:00 UTC) — waiting, no fetch.",
                     DECISION_UTC_HOUR)
            return 0
        state = _decide_today(client, settings, et_date)
        _save(state)

    if state.get("status") != "active":
        log.info("Nothing active today (status=%s).", state.get("status"))
        return 0

    # ---- Martingale execution ----
    try:
        exposure = current_exposure_usd(client)
    except ExposureError as exc:
        log.error("REFUSING TO PLACE: %s (failing closed)", exc)
        return 1
    leg = state["legs"][state["step"]]
    if leg.get("order_id"):
        _check_settlement(client, state)
    else:
        _place_leg(client, settings, state, exposure)
    return 0


if __name__ == "__main__":
    sys.exit(main())
