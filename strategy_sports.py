"""Sports model: devigged sportsbook consensus vs Kalshi moneylines,
across every 2-way US league in season (MLB, NBA, NFL, NHL, WNBA).

We don't predict games. Sportsbooks' odds, with their profit margin (vig)
stripped out, are the sharpest public estimate of win probability there
is — the aggregated smart money that beats the cappers. When Kalshi's
price for a team differs from that fair value by more than fees, we take
Kalshi's side of the gap.

Odds come from The Odds API (the-odds-api.com, set ODDS_API_KEY). Each
book's two-way price is devigged with Shin's method (which models the
favorite-longshot bias directly rather than scaling the vig out
proportionally), then the books are averaged with Pinnacle weighted
PINNACLE_WEIGHT× the soft books. Only pregame moneylines; only leagues
currently in season (the free /v4/sports listing costs no credits, so we
query odds only for sports that are actually active). Soccer is
deliberately excluded: its 3-way lines (draw) need different devig math
and Kalshi structuring.

Line-movement (steam) filter: we only take a side when the sharp fair
probability has moved TOWARD it since the previous run — confirmation that
smart money agrees. The prior line is remembered in sports_line_history.json
(committed by the workflow). Toggle with SPORTS_REQUIRE_STEAM / SPORTS_MIN_MOVE.

    python strategy_sports.py     # read-only scan, no orders
"""

import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from kalshi_client import KalshiClient
from strategy_weather import (price_cents, score_pending_paper_trades,
                              taker_fee_cents)
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_sports")

# Each 2-way US league: The Odds API sport key + best-known Kalshi game
# series ticker. A sport out of season is skipped automatically (no games);
# a wrong Kalshi ticker just returns no events and is skipped with a warning
# — correct any that never produce events after the first live run.
SERIES = [
    dict(series="KXMLBGAME", sport="baseball_mlb", name="MLB"),
    dict(series="KXNBA", sport="basketball_nba", name="NBA"),
    dict(series="KXNFLGAME", sport="americanfootball_nfl", name="NFL"),
    dict(series="KXNHLGAME", sport="icehockey_nhl", name="NHL"),
    dict(series="KXWNBA", sport="basketball_wnba", name="WNBA"),
]
# Owner call (2026-07-07): MLB is cut from the hourly devig model — its
# record didn't earn the slot. MLB bets now happen ONLY when profitable
# smart-money cappers are on them (the copy runner keeps its MLB mapping).
# Re-enable anytime with repo Variable SPORTS_LEAGUES=mlb,nba,nfl,nhl,wnba.
ENABLED_LEAGUES = {s.strip().lower() for s in os.getenv(
    "SPORTS_LEAGUES", "nba,nfl,nhl,wnba").split(",") if s.strip()}


def league_enabled(cfg: dict) -> bool:
    return cfg["sport"].split("_")[-1] in ENABLED_LEAGUES
SPORTS_LIST_URL = "https://api.the-odds-api.com/v4/sports/"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
ODDS_REGIONS = "us"
MIN_START_H = 0.15    # skip games starting within ~10 min (execution risk)
MAX_START_H = 36.0    # and beyond 36h (odds too soft that far out)
MIN_EDGE_CENTS = 5.0
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_sports.csv"
LINE_HISTORY = Path(__file__).resolve().parent / "sports_line_history.json"

# Line-movement (steam) filter. We only take a side when the sharp fair
# probability has moved TOWARD that side since we last looked — i.e. smart
# money is agreeing with us, not fading us. A gap that appears while the
# sharp line is drifting against you is usually the market telling you
# something you don't know. Requires at least one prior observation of the
# game (so the very first sighting never trades). Disable with
# SPORTS_REQUIRE_STEAM=false; SPORTS_MIN_MOVE sets how big the move must be.
SPORTS_REQUIRE_STEAM = os.getenv(
    "SPORTS_REQUIRE_STEAM", "true").strip().lower() not in ("false", "0", "no")
SPORTS_MIN_MOVE = float(os.getenv("SPORTS_MIN_MOVE", "0.0"))   # prob points


PINNACLE_WEIGHT = 3.0   # trust the sharpest book ~3x a soft book


def shin_devig(odds: list) -> list:
    """Fair probabilities from decimal odds via Shin's method — it models the
    favorite-longshot bias (insider fraction z) instead of just proportionally
    scaling out the vig. Reduces to the additive method for two outcomes.
    Solves for z by bisection; falls back to proportional if there's no vig."""
    q = [1.0 / o for o in odds]
    book = sum(q)
    if book <= 1:                       # no overround -> nothing to remove
        return [qi / book for qi in q]

    def p_of_z(qi, z):
        return (math.sqrt(z * z + 4 * (1 - z) * qi * qi / book) - z) / (2 * (1 - z))

    lo, hi = 0.0, 0.9
    for _ in range(80):                 # sum(p) decreases as z rises
        z = (lo + hi) / 2
        if sum(p_of_z(qi, z) for qi in q) > 1:
            lo = z
        else:
            hi = z
    z = (lo + hi) / 2
    return [p_of_z(qi, z) for qi in q]


def shin_two_way(odds_home: float, odds_away: float) -> float:
    """Shin fair probability of the home side from two-way decimal odds."""
    return shin_devig([odds_home, odds_away])[0]


def fair_home_prob(game: dict):
    """Devigged home-win probability, Pinnacle-weighted across books.
    Each book's two-way price is devigged with Shin's method, then averaged
    with Pinnacle weighted PINNACLE_WEIGHT× the soft books. None if no usable
    two-way quote exists."""
    home, away = game.get("home_team"), game.get("away_team")
    wsum, wtot = 0.0, 0.0
    for book in game.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key") != "h2h":
                continue
            prices = {o.get("name"): o.get("price")
                      for o in market.get("outcomes", [])}
            oh, oa = prices.get(home), prices.get(away)
            if oh and oa and oh > 1 and oa > 1:
                p = shin_two_way(oh, oa)
                w = PINNACLE_WEIGHT if book.get("key") == "pinnacle" else 1.0
                wsum += w * p
                wtot += w
    return wsum / wtot if wtot else None


def load_line_history() -> dict:
    """Prior sharp fair-home probability per game id (from the last run)."""
    try:
        return json.loads(LINE_HISTORY.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_line_history(hist: dict) -> None:
    LINE_HISTORY.write_text(json.dumps(hist, indent=0, sort_keys=True))


def hours_until(iso_time: str):
    try:
        t = datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t - datetime.now(timezone.utc)).total_seconds() / 3600.0


def _words(text: str) -> set:
    return {w for w in re.split(r"[^A-Za-z]+", (text or "").upper())
            if len(w) >= 3 and w not in ("THE", "LOS", "NEW", "SAN")}


def match_team(label: str, games: list):
    """Find which game/side a Kalshi team label refers to. Returns
    (game, 'home'|'away') only when the match is unambiguous — one team in
    one game. Anything unclear is skipped rather than guessed."""
    words = _words(label)
    if not words:
        return None
    hits = []
    for game in games:
        for side in ("home", "away"):
            if words <= _words(game.get(f"{side}_team")):
                hits.append((game, side))
    return hits[0] if len(hits) == 1 else None


def in_season_sports(api_key: str) -> set:
    """Sport keys currently active. The /v4/sports listing costs zero
    API credits, so this lets us pull paid odds only for live leagues."""
    resp = requests.get(SPORTS_LIST_URL, params={"apiKey": api_key}, timeout=20)
    resp.raise_for_status()
    return {s["key"] for s in resp.json()
            if s.get("active") and not s.get("has_outrights")}


def fetch_games(api_key: str, sport: str) -> list:
    resp = requests.get(
        ODDS_URL.format(sport=sport),
        params={"apiKey": api_key, "regions": ODDS_REGIONS,
                "markets": "h2h", "oddsFormat": "decimal"},
        timeout=20,
    )
    resp.raise_for_status()
    games = []
    for game in resp.json():
        h = hours_until(game.get("commence_time"))
        if h is not None and MIN_START_H <= h <= MAX_START_H:
            games.append(game)
    return games


def evaluate_market(market: dict, games: list, history: dict = None) -> list:
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "")
    matched = match_team(label, games)
    if not matched:
        return []
    game, side = matched
    p_fair = fair_home_prob(game)
    if p_fair is None:
        return None  # game found but no usable odds
    p = p_fair if side == "home" else 1.0 - p_fair

    # Steam gate: has the sharp home probability moved toward the team we'd be
    # backing since the last run? move_home > 0 means it drifted toward home.
    prev = (history or {}).get(game.get("id")) if history is not None else None
    prev_home = prev.get("home_prob") if isinstance(prev, dict) else None
    move_home = (p_fair - prev_home) if prev_home is not None else None
    other = "away" if side == "home" else "home"

    def steam_ok(back_side: str) -> bool:
        if not SPORTS_REQUIRE_STEAM:
            return True
        if move_home is None:               # no prior line to confirm a move
            return False
        toward = move_home if back_side == "home" else -move_home
        return toward >= SPORTS_MIN_MOVE

    signals = []
    yes_ask = price_cents(market, "yes_ask")
    if yes_ask and 0 < yes_ask < 100 and steam_ok(side):
        ev = 100.0 * p - yes_ask - taker_fee_cents(yes_ask)
        if ev >= MIN_EDGE_CENTS:
            signals.append(dict(side="yes", price_cents=yes_ask,
                                model_prob=p, ev_cents=ev))
    yes_bid = price_cents(market, "yes_bid")
    if yes_bid and 0 < yes_bid < 100 and steam_ok(other):
        no_price = 100.0 - yes_bid
        ev = 100.0 * (1.0 - p) - no_price - taker_fee_cents(no_price)
        if ev >= MIN_EDGE_CENTS:
            signals.append(dict(side="no", price_cents=no_price,
                                model_prob=1.0 - p, ev_cents=ev))
    for s in signals:
        s.update(ticker=market.get("ticker"), subtitle=label)
    return signals


def scan(api_key: str) -> list:
    """Same result shape as the other models; 'date' carries the event
    ticker so one-bet-per-event grouping works."""
    client = KalshiClient(env="prod")
    results = []
    history = load_line_history()   # sharp lines as of the previous run
    new_history = {}                # what we'll persist for the next run
    try:
        active = in_season_sports(api_key)
    except Exception as exc:
        log.warning("Could not fetch in-season list (%s); trying all sports", exc)
        active = {c["sport"] for c in SERIES}

    for cfg in SERIES:
        if not league_enabled(cfg):
            log.info("%s: cut from the hourly model (owner call) — copy "
                     "runner still takes smart-money %s bets", cfg["name"],
                     cfg["name"])
            continue
        if cfg["sport"] not in active:
            log.info("%s: out of season, skipping (no odds credits spent)",
                     cfg["name"])
            continue
        try:
            games = fetch_games(api_key, cfg["sport"])
        except Exception as exc:
            log.warning("Skipping %s (odds fetch failed: %s)", cfg["name"], exc)
            continue
        log.info("%s: %d upcoming games with odds", cfg["name"], len(games))
        if not games:
            continue

        # record each game's current sharp home prob so the next run can tell
        # which way the line moved (the steam filter's memory)
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for g in games:
            hp = fair_home_prob(g)
            if hp is not None and g.get("id"):
                new_history[g["id"]] = dict(
                    home_prob=round(hp, 4),
                    home_team=g.get("home_team"),
                    away_team=g.get("away_team"),
                    updated=now_iso)
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": cfg["series"], "status": "open",
                        "with_nested_markets": "true", "limit": 60},
            )
        except Exception as exc:
            log.warning("Skipping %s markets: %s", cfg["name"], exc)
            continue

        for event in data.get("events", []):
            event_ticker = event.get("event_ticker") or event.get("ticker") or ""
            signals = []
            for market in event.get("markets") or []:
                if market.get("status") not in (None, "active", "open"):
                    continue
                got = evaluate_market(market, games, history)
                if got:
                    signals.extend(got)
            if not signals:
                continue
            signals.sort(key=lambda s: -s["ev_cents"])
            results.append(dict(date=event_ticker, mu=0.0,
                                city=cfg["name"],
                                title=event.get("title", ""),
                                signals=signals))
    if new_history:
        save_line_history(new_history)
    return results


def append_paper_trades(signals: list, event: str) -> None:
    new_file = not PAPER_LOG.exists()
    with open(PAPER_LOG, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["scanned_at_utc", "event", "ticker", "side",
                             "price_cents", "model_prob", "ev_cents",
                             "outcome"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for s in signals:
            writer.writerow([now, event, s["ticker"], s["side"],
                             f"{s['price_cents']:.0f}",
                             f"{s['model_prob']:.3f}",
                             f"{s['ev_cents']:.1f}", ""])


def main() -> int:
    import os
    setup_logging()
    api_key = os.getenv("ODDS_API_KEY", "").strip()
    if not api_key:
        log.error("ODDS_API_KEY not set. Get a free key at the-odds-api.com "
                  "and add it to .env / repo secrets.")
        return 1
    try:
        score_pending_paper_trades(PAPER_LOG)
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)

    results = scan(api_key)
    total = 0
    for r in results:
        log.info("%s (%s):", r["title"], r["date"])
        for s in r["signals"]:
            log.info("  SIGNAL: buy %s %s @ %.0fc | fair %.0f%% | EV +%.1fc | %s",
                     s["side"].upper(), s["ticker"], s["price_cents"],
                     100 * s["model_prob"], s["ev_cents"], s["subtitle"])
        append_paper_trades(r["signals"], r["date"])
        total += len(r["signals"])
    log.info("%s signal(s). NO ORDERS PLACED by this script.", total or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
