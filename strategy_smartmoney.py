"""Smart-money model: follow Polymarket's consistently profitable wallets.

Polymarket's public read-only APIs expose the full trade tape, every
wallet's positions, and each wallet's daily PnL curve (the profile chart).
Polymarket geoblocks US order placement, so we use it purely as a SIGNAL
source and EXECUTE on Kalshi, where the same games trade as moneylines.

Pipeline per run:
  1. Sample the tape for recent big-money trades (public, no key) and
     rank the wallets behind them by their measured 2-WEEK PnL — profits
     over both the fortnight and the last week, so one lucky bet doesn't
     qualify. Top SHARP_N survivors are "sharp".
  2. Collect each sharp wallet's fresh BUYs (last CONS_WINDOW_H hours).
     Only a market+outcome bought by >= MIN_WALLETS DISTINCT sharps with
     real stakes counts — one whale is noise, several sharps agreeing is
     the signal.
  3. Map consensus onto Kalshi: big-league moneylines (MLB/NBA/NFL/NHL/
     WNBA -> KX*GAME series) and World Cup regulation match-winner/draw
     markets (fifwc slugs -> KXFIFAGAME). Our fair prob is the sharps'
     stake-weighted entry plus a small premium, so EV math refuses to
     chase a line that has already run away from their entry.

Non-mappable consensus (crypto 15-minute markets, totals/props/exact
scores, team-to-advance — a different bet than regulation winner — and
venues without a Kalshi twin) is logged for visibility and skipped.

    python strategy_smartmoney.py    # read-only scan, no orders
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from strategy_sports import SERIES, _words
from strategy_weather import (price_cents, score_pending_paper_trades,
                              taker_fee_cents)
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_smartmoney")

TAPE_URL = "https://data-api.polymarket.com/trades"
PNL_URL = "https://user-pnl-api.polymarket.com/user-pnl"
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_smartmoney.csv"

# --- sharp-wallet discovery ---
TAPE_MIN_USDC = float(os.getenv("SM_TAPE_MIN_USDC", "250"))   # big trades only
TAPE_PAGES = int(os.getenv("SM_TAPE_PAGES", "3"))             # x500 trades
CANDIDATES_MAX = int(os.getenv("SM_CANDIDATES_MAX", "40"))    # PnL lookups cap
SHARP_MIN_PNL_2W = float(os.getenv("SM_MIN_PNL_2W", "500"))   # $ over 2 weeks
SHARP_N = int(os.getenv("SM_SHARP_N", "15"))                  # wallets tracked

# --- consensus rule (the point of the model) ---
CONS_WINDOW_H = float(os.getenv("SM_CONS_WINDOW_H", "36"))
MIN_WALLETS = int(os.getenv("SM_MIN_WALLETS", "3"))           # distinct sharps
MIN_STAKE_USDC = float(os.getenv("SM_MIN_STAKE", "50"))       # per-sharp stake

# --- pricing ---
# Sharps demand an edge, so fair value sits above their entry: prob =
# entry + PREMIUM. With MIN_EDGE below, the net effect is "take the trade
# only within ~PREMIUM-MIN_EDGE-fee cents of the sharps' entry" — copy
# them, never chase a line that already ran.
PREMIUM_PTS = float(os.getenv("SM_PREMIUM_PTS", "8"))
MAX_PROB = 0.97
MIN_EDGE_CENTS = float(os.getenv("SM_MIN_EDGE_CENTS", "3"))

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# plain moneyline slugs look like mlb-phi-kc-2026-07-06; spread/total/prop
# slugs carry suffixes (-spread-away-1pt5, ...) and are rejected
MONEYLINE_RE = re.compile(
    r"^(mlb|nba|nfl|nhl|wnba)-[a-z0-9]+-[a-z0-9]+-(\d{4})-(\d{2})-(\d{2})$")
LEAGUE_SERIES = {c["sport"].split("_")[-1]: c["series"] for c in SERIES}
LEAGUE_SERIES.update({"mlb": "KXMLBGAME", "nba": "KXNBA", "nfl": "KXNFLGAME",
                      "nhl": "KXNHLGAME", "wnba": "KXWNBA"})

# World Cup: Polymarket fifwc-prt-esp-2026-07-06-esp is the regulation
# match-winner ("Will Spain win on ...?") and -draw the tie. Kalshi's game
# series uses the SAME FIFA trigrams in its event tickers
# (KXFIFAGAME-26JUL06PRTESP, probe-verified). team-to-advance is a
# DIFFERENT bet (draw -> penalties still advances someone) with no
# verified Kalshi twin, so it is deliberately NOT mapped; likewise all
# totals/exact-score/player props.
WC_SERIES = "KXFIFAGAME"
WC_RE = re.compile(
    r"^fifwc-([a-z]{3})-([a-z]{3})-(\d{4})-(\d{2})-(\d{2})-([a-z]{3}|draw)$")


def _get(url: str, **params) -> list:
    resp = requests.get(url, params=params, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.json()


def fetch_big_trades() -> list:
    """Recent tape entries above TAPE_MIN_USDC, a few pages deep."""
    out = []
    for page in range(TAPE_PAGES):
        out.extend(_get(TAPE_URL, limit=500, offset=page * 500,
                        filterType="CASH", filterAmount=int(TAPE_MIN_USDC)))
    return out


def fetch_pnl_curve(wallet: str) -> list:
    """[(unix_ts, cumulative_pnl_$), ...] oldest->newest for the last month
    — the same data behind the polymarket.com profile chart."""
    data = _get(PNL_URL, user_address=wallet, interval="1m", fidelity="1d")
    return [(d["t"], d["p"]) for d in data]


def pnl_change(curve: list, days: float, now_ts: float = None) -> float:
    """PnL over the trailing `days`: last point minus the closest point at
    or before now-days. Uses the curve's start when history is shorter."""
    if not curve:
        return 0.0
    cutoff = (now_ts or datetime.now(timezone.utc).timestamp()) - days * 86400
    base = curve[0][1]
    for t, p in curve:
        if t <= cutoff:
            base = p
        else:
            break
    return curve[-1][1] - base


def select_sharp_wallets() -> dict:
    """wallet -> 2-week PnL for the top consistently profitable wallets
    found behind recent big-money flow."""
    stake = {}
    for tr in fetch_big_trades():
        w = tr.get("proxyWallet")
        usdc = float(tr.get("size", 0)) * float(tr.get("price", 0))
        if w:
            stake[w] = stake.get(w, 0.0) + usdc
    candidates = sorted(stake, key=stake.get, reverse=True)[:CANDIDATES_MAX]

    sharp = {}
    for w in candidates:
        try:
            curve = fetch_pnl_curve(w)
        except Exception as exc:
            log.debug("PnL fetch failed for %s (%s)", w, exc)
            continue
        pnl2w = pnl_change(curve, 14)
        pnl1w = pnl_change(curve, 7)
        # profitable over the fortnight AND still up over the last week —
        # filters the one-lucky-parlay wallets
        if pnl2w >= SHARP_MIN_PNL_2W and pnl1w > 0:
            sharp[w] = pnl2w
    top = dict(sorted(sharp.items(), key=lambda kv: -kv[1])[:SHARP_N])
    log.info("Sharp wallets: %d of %d candidates (2w PnL $%.0f..$%.0f)",
             len(top), len(candidates),
             min(top.values(), default=0), max(top.values(), default=0))
    return top


def fetch_wallet_buys(wallet: str, window_h: float,
                      now_ts: float = None) -> list:
    """This wallet's BUY entries within the window, meaningful stakes only."""
    now = now_ts or datetime.now(timezone.utc).timestamp()
    out = []
    try:
        trades = _get(TAPE_URL, user=wallet, limit=100)
    except Exception as exc:
        log.debug("trades fetch failed for %s (%s)", wallet, exc)
        return out
    for tr in trades:
        usdc = float(tr.get("size", 0)) * float(tr.get("price", 0))
        if (tr.get("side") == "BUY"
                and now - float(tr.get("timestamp", 0)) <= window_h * 3600
                and usdc >= MIN_STAKE_USDC):
            out.append(tr)
    return out


def build_consensus(sharp_wallets: dict, now_ts: float = None) -> list:
    """Markets where >= MIN_WALLETS distinct sharps bought the SAME outcome
    recently. Returns [{slug, title, outcome, wallets, stake, avg_price}]."""
    groups = {}
    for w in sharp_wallets:
        for tr in fetch_wallet_buys(w, CONS_WINDOW_H, now_ts):
            key = (tr.get("slug") or tr.get("conditionId") or "",
                   tr.get("outcome") or str(tr.get("outcomeIndex")))
            g = groups.setdefault(key, dict(wallets=set(), stake=0.0,
                                            weighted=0.0,
                                            title=tr.get("title", "")))
            usdc = float(tr.get("size", 0)) * float(tr.get("price", 0))
            g["wallets"].add(w)
            g["stake"] += usdc
            g["weighted"] += usdc * float(tr.get("price", 0))
    out = []
    for (slug, outcome), g in groups.items():
        if len(g["wallets"]) >= MIN_WALLETS and g["stake"] > 0:
            out.append(dict(slug=slug, outcome=outcome, title=g["title"],
                            wallets=len(g["wallets"]), stake=g["stake"],
                            avg_price=g["weighted"] / g["stake"]))
    out.sort(key=lambda c: -c["stake"])
    return out


def kalshi_date_token(y: str, m: str, d: str) -> str:
    """('2026','07','06') -> '26JUL06' as embedded in Kalshi game tickers."""
    return f"{y[2:]}{MONTHS[int(m) - 1]}{d}"


def _priced_signal(cons: dict, market: dict, event: dict) -> dict:
    """YES signal on this Kalshi market at the sharps' price + premium, or
    None when the ask already ran past their entry (no chasing)."""
    p = min(cons["avg_price"] + PREMIUM_PTS / 100.0, MAX_PROB)
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "")
    yes_ask = price_cents(market, "yes_ask")
    if not yes_ask or not 0 < yes_ask < 100:
        return None
    ev = 100.0 * p - yes_ask - taker_fee_cents(yes_ask)
    if ev < MIN_EDGE_CENTS:   # line already ran past the sharps
        log.info("No chase: %s ask %.0fc vs sharp entry %.0fc",
                 label, yes_ask, 100 * cons["avg_price"])
        return None
    return dict(side="yes", price_cents=yes_ask, model_prob=p,
                ev_cents=ev, ticker=market.get("ticker"),
                subtitle=f"{label} ({cons['wallets']} sharps, "
                         f"${cons['stake']:.0f})",
                event_ticker=event.get("event_ticker")
                or event.get("ticker") or "",
                event_title=event.get("title", ""))


def consensus_signal(cons: dict, events: list) -> dict:
    """Map one US-league moneyline consensus onto a Kalshi game market.
    Returns a signal dict (plus event_ticker/title) or None."""
    m = MONEYLINE_RE.match(cons["slug"])
    if not m:
        return None
    token = kalshi_date_token(m.group(2), m.group(3), m.group(4))
    team_words = _words(cons["outcome"])
    if not team_words:
        return None
    for event in events:
        event_ticker = event.get("event_ticker") or event.get("ticker") or ""
        if token not in event_ticker:
            continue
        for market in event.get("markets") or []:
            if market.get("status") not in (None, "active", "open"):
                continue
            label = (market.get("yes_sub_title") or market.get("subtitle")
                     or market.get("title") or "")
            if team_words <= _words(label):
                return _priced_signal(cons, market, event)
    return None


def wc_signal(cons: dict, events: list) -> dict:
    """Map a World Cup match-winner (or draw) consensus onto the matching
    KXFIFAGAME market. Every uncertainty fails CLOSED: unknown suffix, no
    matching event, or no matching side market -> no trade."""
    m = WC_RE.match(cons["slug"])
    if not m:
        return None
    a, b, suffix = m.group(1), m.group(2), m.group(6)
    if suffix not in (a, b, "draw"):     # a prop that looks like a trigram
        return None
    token = kalshi_date_token(m.group(3), m.group(4), m.group(5))
    tails = (token + a.upper() + b.upper(), token + b.upper() + a.upper())
    want = ("-TIE", "-DRAW") if suffix == "draw" \
        else (f"-{suffix.upper()}",)
    for event in events:
        event_ticker = event.get("event_ticker") or event.get("ticker") or ""
        if not event_ticker.endswith(tails[0]) \
                and not event_ticker.endswith(tails[1]):
            continue
        for market in event.get("markets") or []:
            if market.get("status") not in (None, "active", "open"):
                continue
            if any(str(market.get("ticker", "")).endswith(w) for w in want):
                return _priced_signal(cons, market, event)
    return None


def scan() -> list:
    """Standard model result shape; 'date' carries the Kalshi event ticker
    so one-bet-per-event grouping and the sports theme cap apply."""
    from kalshi_client import KalshiClient
    sharps = select_sharp_wallets()
    if not sharps:
        log.info("No wallets passed the sharp filter this run.")
        return []
    consensus = build_consensus(sharps)
    log.info("Consensus markets (>=%d sharps agreeing): %d",
             MIN_WALLETS, len(consensus))
    if not consensus:
        return []

    client = KalshiClient(env="prod")
    events_by_series = {}

    def events_for(series: str) -> list:
        if series not in events_by_series:
            try:
                data = client._request(
                    "GET", "/events",
                    params={"series_ticker": series, "status": "open",
                            "with_nested_markets": "true", "limit": 60})
                events_by_series[series] = data.get("events", [])
            except Exception as exc:
                log.warning("Kalshi events fetch failed for %s: %s",
                            series, exc)
                events_by_series[series] = []
        return events_by_series[series]

    results = []
    for cons in consensus:
        ml = MONEYLINE_RE.match(cons["slug"])
        if ml:
            sig = consensus_signal(cons,
                                   events_for(LEAGUE_SERIES[ml.group(1)]))
        elif WC_RE.match(cons["slug"]):
            sig = wc_signal(cons, events_for(WC_SERIES))
        else:
            log.info("Unmappable consensus (no Kalshi venue): %s | %s | "
                     "%d sharps $%.0f @ %.0fc", cons["title"], cons["outcome"],
                     cons["wallets"], cons["stake"], 100 * cons["avg_price"])
            continue
        if not sig:
            continue
        event_ticker = sig.pop("event_ticker")
        title = sig.pop("event_title")
        log.info("SMART MONEY: %d sharps ($%.0f) on %s -> %s",
                 cons["wallets"], cons["stake"], cons["outcome"],
                 sig["ticker"])
        results.append(dict(date=event_ticker, mu=100 * cons["avg_price"],
                            city=f"{cons['wallets']} sharps",
                            title=title, signals=[sig]))
    return results


def main() -> int:
    setup_logging()
    try:
        score_pending_paper_trades(PAPER_LOG)
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)
    results = scan()
    total = sum(len(r["signals"]) for r in results)
    for r in results:
        for s in r["signals"]:
            log.info("  SIGNAL: buy %s %s @ %.0fc | prob %.0f%% | EV +%.1fc | %s",
                     s["side"].upper(), s["ticker"], s["price_cents"],
                     100 * s["model_prob"], s["ev_cents"], s["subtitle"])
    log.info("%s smart-money signal(s). NO ORDERS placed by this script.",
             total or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
