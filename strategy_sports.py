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

Line movement is RECORDED, not required. Requiring the sharp line to have
already moved toward our side meant entering after the move — "betting the
post-steam price is not capturing edge but paying fair value, minus the vig" —
and the model measured -6.0c CLV doing exactly that. Every signal now carries a
SIGNED `steam` (positive = late, negative = early) plus `is_home`, so CLV can
tell us which actually predicts beating the close. The prior line lives in
sports_line_history.json. SPORTS_REQUIRE_STEAM=true restores the old gate.

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
from statistics import NormalDist

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
#
# The *GAME suffix is load-bearing. `KXWNBA` and `KXNBA` are the SEASON
# CHAMPIONSHIP series ("Women's Pro Basketball Champion", "2027 Pro Basketball
# Champion"), not tonight's game. On 2026-08-07 that mismatch bought 30 shares
# of Atlanta-to-win-the-title at 8c because match_team saw the label "Atlanta",
# compared it to Atlanta's *game* win probability of 66%, and reported a 58c
# edge. The per-game series are KXWNBAGAME / KXNBAGAME.
SERIES = [
    dict(series="KXMLBGAME", sport="baseball_mlb", name="MLB"),
    dict(series="KXNBAGAME", sport="basketball_nba", name="NBA"),
    dict(series="KXNFLGAME", sport="americanfootball_nfl", name="NFL"),
    dict(series="KXNHLGAME", sport="icehockey_nhl", name="NHL"),
    dict(series="KXWNBAGAME", sport="basketball_wnba", name="WNBA"),
]
# Rebuilt 2026-07-08 into a SELECTIVE sharp-line tracker (owner call): the
# old model bet every EV gap and bled. Now it follows where the sharp money
# is moving and takes only the few best plays a day — a pick must clear a
# confidence floor AND show a real steam move AND beat fees, and only the
# top SPORTS_MAX_PER_DAY by edge are taken across all games. MLB is back in.
ENABLED_LEAGUES = {s.strip().lower() for s in os.getenv(
    "SPORTS_LEAGUES", "mlb,nba,nfl,nhl,wnba").split(",") if s.strip()}


def league_enabled(cfg: dict) -> bool:
    return cfg["sport"].split("_")[-1] in ENABLED_LEAGUES
SPORTS_LIST_URL = "https://api.the-odds-api.com/v4/sports/"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
ODDS_REGIONS = "us"
MIN_START_H = 0.15    # skip games starting within ~10 min (execution risk)
MAX_START_H = 36.0    # and beyond 36h (odds too soft that far out)
MIN_EDGE_CENTS = 5.0

# An edge this big is not an opportunity, it is a bug. Our own model is a
# devigged consensus of the same public books Kalshi's traders read; a genuine
# disagreement is a few cents. The 58c "edge" that bought a championship future
# came from comparing a game probability to a season-long price, and nothing in
# the pipeline objected — the sizing model happily staked the maximum because a
# larger edge always looks better to Kelly. This ceiling is the backstop for
# every mismatch of that shape, including ones nobody has thought of yet.
SPORTS_MAX_EDGE_CENTS = float(os.getenv("SPORTS_MAX_EDGE_CENTS", "25"))

# A market must settle on the GAME we priced. Game markets expire within a few
# hours of the first pitch/tip; a championship market expires months later. The
# window is deliberately loose (extra innings, overtime, rain delays) because it
# only has to separate "this game" from "some other contract about this team".
EXPIRY_LEAD_H = float(os.getenv("SPORTS_EXPIRY_LEAD_H", "2"))
EXPIRY_LAG_H = float(os.getenv("SPORTS_EXPIRY_LAG_H", "12"))

PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_sports.csv"
LINE_HISTORY = Path(__file__).resolve().parent / "sports_line_history.json"

# Line-movement (steam). RECORDED ON EVERY SIGNAL, GATED ON BY DEFAULT NO MORE.
#
# This used to require the sharp probability to have already moved TOWARD our
# side before entering. That is chasing steam, and the betting literature is
# blunt about where it leads: "by chasing steam it is very difficult to get
# closing line value — betting the post-steam price is not capturing edge but
# paying fair value, minus the vig." Entering only after the move mechanically
# guarantees we buy at or behind the new price, and the model duly measured
# **-6.0c average CLV** over its 9 scored bets. The filter was not unlucky, it
# was pointed the wrong way.
#
# So the move is now a MEASURED FEATURE rather than a precondition: every
# signal carries a SIGNED `steam` value (positive = the line moved toward us
# before we bet, negative = we are early / fading the drift), and the CLV
# tracker scores the bet either way. Once enough bets are scored we can test
# which sign actually predicts beating the close, instead of assuming.
#
# Set SPORTS_REQUIRE_STEAM=true to restore the old gate.
SPORTS_REQUIRE_STEAM = os.getenv(
    "SPORTS_REQUIRE_STEAM", "false").strip().lower() not in ("false", "0", "no")
SPORTS_MIN_MOVE = float(os.getenv("SPORTS_MIN_MOVE", "0.01"))   # prob points
# How confident the sharp price must make our side. Lowered from 0.60 to 0.52:
# the old floor excluded the entire 50-60c band, which is where sharp-vs-Kalshi
# disagreement is widest and where the favourite-longshot bias (the other
# model's thesis) does not apply. The EV floor still does the real filtering.
#
# 0.52 STRUCTURALLY EXCLUDED EVERY UNDERDOG. An underdog is a side our model
# puts below 50% by definition, so a floor above 0.50 could only ever back
# favourites — and the moneyline leg duly reported "0 qualifying" on every run
# for its whole life. The gate was not selective, it was closed.
#
# The floor is now 0.25, opening the 25-50% band where sharp-vs-Kalshi
# disagreement is widest. It stops there rather than going lower because the
# favourite-longshot bias is most severe among true longshots: their prices are
# systematically inflated, so apparent edge down there is more likely to be our
# own model error than a real mispricing. Shin's devig already models that bias
# (which is why this repo uses Shin rather than proportional devig), but
# modelling a bias is not the same as being immune to it.
#
# Whether underdogs actually beat the close is now a question the CLV
# scoreboard can answer, because `is_underdog` is recorded on every signal.
# Nothing here assumes they will.
SPORTS_MIN_PROB = float(os.getenv("SPORTS_MIN_PROB", "0.25"))
# Back-compat: SPORTS_MIN_CONFIDENCE still works if it is set explicitly.
SPORTS_MIN_CONFIDENCE = float(
    os.getenv("SPORTS_MIN_CONFIDENCE", str(SPORTS_MIN_PROB)))
# SEPARATE daily budgets: a few moneyline plays AND a few over/under plays,
# each capped independently and each taking only its best by edge.
SPORTS_MAX_ML_PER_DAY = int(os.getenv("SPORTS_MAX_ML_PER_DAY", "2"))
SPORTS_MAX_TOTALS_PER_DAY = int(os.getenv("SPORTS_MAX_TOTALS_PER_DAY", "2"))

# --- over/under (totals) ---
# Kalshi "Over X.5 runs" ladders per league. We devig the book's total to an
# implied MEAN, model the game total as Normal(mean, TOTAL_SIGMA), and price
# each Kalshi threshold off that — only within TOTAL_MAX_OFFSET of the mean,
# where the normal approximation is least unreliable. SIGMA is a modelling
# assumption (MLB run totals ~3): tune SPORTS_TOTAL_SIGMA once we have data.
TOTALS_SERIES = {"baseball_mlb": "KXMLBTOTAL"}
TOTAL_SIGMA = float(os.getenv("SPORTS_TOTAL_SIGMA", "3.0"))
TOTAL_MAX_OFFSET = float(os.getenv("SPORTS_TOTAL_MAX_OFFSET", "2.5"))
# require the sharp TOTAL line to have moved toward our side (runs), like the
# moneyline steam gate — sharp confirmation, not just our model vs Kalshi
SPORTS_TOTAL_MIN_MOVE = float(os.getenv("SPORTS_TOTAL_MIN_MOVE", "0.2"))


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


def fair_total_mean(game: dict):
    """Pinnacle-weighted implied MEAN game total from the books' totals
    market. Each book's over/under at its line is devigged, then the mean is
    backed out under Normal(mean, TOTAL_SIGMA): P(total>line)=p_over implies
    mean = line + sigma·Φ⁻¹(p_over). None if no usable totals quote."""
    wsum, wtot = 0.0, 0.0
    for book in game.get("bookmakers", []):
        for market in book.get("markets", []):
            if market.get("key") != "totals":
                continue
            over = under = line = None
            for o in market.get("outcomes", []):
                nm = (o.get("name") or "").lower()
                if nm == "over":
                    over, line = o.get("price"), o.get("point")
                elif nm == "under":
                    under = o.get("price")
            if over and under and over > 1 and under > 1 and line is not None:
                p_over = shin_two_way(over, under)
                p_over = min(max(p_over, 1e-4), 1 - 1e-4)
                mean = line + TOTAL_SIGMA * NormalDist().inv_cdf(p_over)
                w = PINNACLE_WEIGHT if book.get("key") == "pinnacle" else 1.0
                wsum += w * mean
                wtot += w
    return wsum / wtot if wtot else None


def over_prob(mean: float, strike: float) -> float:
    """P(game total > strike) under Normal(mean, TOTAL_SIGMA)."""
    return 1.0 - NormalDist(mean, TOTAL_SIGMA).cdf(strike)


def match_total_game(event_title: str, games: list):
    """Match a Kalshi totals event ('Colorado vs Los Angeles D: Total Runs')
    to the odds-API game by both teams' city word. Fails closed: only when
    exactly one game has both cities in the title (Kalshi truncates team
    names, so we key off the leading city token, not the full name)."""
    tl = (event_title or "").lower()
    hits = []
    for g in games:
        home = (g.get("home_team") or "").split()
        away = (g.get("away_team") or "").split()
        if (home and away and home[0].lower() in tl
                and away[0].lower() in tl):
            hits.append(g)
    return hits[0] if len(hits) == 1 else None


def evaluate_total_market(market: dict, mean: float, move: float = None) -> list:
    """Signals for an 'Over X.5' market vs our modelled total. Only prices
    strikes within TOTAL_MAX_OFFSET of the mean (where Normal is least
    unreliable); needs the confidence floor, the edge, and — like moneylines
    — a real move in the sharp total toward our side."""
    strike = market.get("floor_strike")
    if strike is None:
        return []
    try:
        strike = float(strike)
    except (TypeError, ValueError):
        return []
    if abs(strike - mean) > TOTAL_MAX_OFFSET:
        return []
    p_over = over_prob(mean, strike)
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "")

    def steam_ok(back_over: bool) -> bool:
        if not SPORTS_REQUIRE_STEAM:
            return True
        if move is None:                    # no prior total to confirm a move
            return False
        toward = move if back_over else -move
        return toward >= SPORTS_TOTAL_MIN_MOVE

    signals = []
    yes_ask = price_cents(market, "yes_ask")
    if (yes_ask and 0 < yes_ask < 100 and p_over >= SPORTS_MIN_CONFIDENCE
            and steam_ok(True)):
        ev = 100.0 * p_over - yes_ask - taker_fee_cents(yes_ask)
        if edge_ok(ev, market.get("ticker")):
            signals.append(dict(side="yes", price_cents=yes_ask,
                                model_prob=p_over, ev_cents=ev,
                                steam=(move if move is not None else None)))
    yes_bid = price_cents(market, "yes_bid")
    if (yes_bid and 0 < yes_bid < 100 and (1.0 - p_over) >= SPORTS_MIN_CONFIDENCE
            and steam_ok(False)):
        no_price = 100.0 - yes_bid
        ev = 100.0 * (1.0 - p_over) - no_price - taker_fee_cents(no_price)
        if edge_ok(ev, market.get("ticker")):
            signals.append(dict(side="no", price_cents=no_price,
                                model_prob=1.0 - p_over, ev_cents=ev,
                                steam=(-move if move is not None else None)))
    for s in signals:
        # totals have no home/away side; None keeps the column honest rather
        # than defaulting to False and inventing a category
        s.update(ticker=market.get("ticker"), subtitle=label, is_home=None)
    return signals


def load_line_history() -> dict:
    """Prior sharp fair-home probability per game id (from the last run)."""
    try:
        return json.loads(LINE_HISTORY.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_line_history(hist: dict) -> None:
    LINE_HISTORY.write_text(json.dumps(hist, indent=0, sort_keys=True))


def parse_iso(iso_time):
    """UTC datetime from an API timestamp, or None if it isn't one."""
    if not iso_time:
        return None
    try:
        t = datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def hours_until(iso_time: str):
    t = parse_iso(iso_time)
    if t is None:
        return None
    return (t - datetime.now(timezone.utc)).total_seconds() / 3600.0


def market_expiry(market: dict):
    """When this market is expected to settle. `close_time` is the last moment
    trading is allowed and runs days past a game, so it is only the fallback —
    `expected_expiration_time` is the one that tracks the event itself."""
    for field in ("expected_expiration_time", "close_time",
                  "latest_expiration_time"):
        t = parse_iso(market.get(field))
        if t is not None:
            return t
    return None


def covers_game(market: dict, game: dict) -> bool:
    """Does this Kalshi market settle on THIS game?

    Team names alone cannot answer that: "Atlanta" appears on tonight's
    moneyline and on the season championship future alike, and match_team is
    happy with both. Settlement time separates them — a game market expires
    hours after first pitch, a futures market months later.

    Unknown timestamps return False. Refusing to trade a market we cannot place
    in time is the same convention match_team already follows for a team name
    it cannot place in a game: skip rather than guess."""
    start = parse_iso(game.get("commence_time"))
    expiry = market_expiry(market)
    if start is None or expiry is None:
        return False
    lead = (start - expiry).total_seconds() / 3600.0     # expiry before start
    lag = (expiry - start).total_seconds() / 3600.0      # expiry after start
    return lead <= EXPIRY_LEAD_H and lag <= EXPIRY_LAG_H


def edge_ok(ev_cents: float, ticker: str = "") -> bool:
    """Is this edge in the range a real disagreement can produce?

    Both ends matter. Below MIN_EDGE_CENTS the fee eats it; above
    SPORTS_MAX_EDGE_CENTS it is not an edge at all but a sign we priced the
    wrong contract, and that end is the dangerous one — Kelly stakes hardest
    exactly where the number is most absurd."""
    if ev_cents < MIN_EDGE_CENTS:
        return False
    if ev_cents > SPORTS_MAX_EDGE_CENTS:
        log.warning("SKIP %s: %.1fc edge exceeds the %.0fc sanity ceiling — "
                    "treating this as a mispriced match, not an opportunity",
                    ticker or "?", ev_cents, SPORTS_MAX_EDGE_CENTS)
        return False
    return True


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
    _note_quota(resp)          # free call, but the headers still tell us where we stand
    resp.raise_for_status()
    return {s["key"] for s in resp.json()
            if s.get("active") and not s.get("has_outrights")}


class OddsBudgetExhausted(RuntimeError):
    """Raised instead of spending the last of the monthly odds quota."""


# The Odds API free tier is 500 credits a MONTH, and a call costs
# markets x regions — h2h+totals over us = 2 credits per league per call.
# The previous generation ran ~5 leagues every 15 minutes and burned the whole
# month in ~3 days, then returned 401 for the remaining 27 while every workflow
# still reported success. Never again: refuse to spend below a reserve, and
# record what the API says is left so the reserve survives a process restart.
ODDS_MIN_REMAINING = float(os.getenv("ODDS_MIN_REMAINING", "50"))
ODDS_QUOTA_FILE = Path(__file__).resolve().parent / "odds_quota.json"


def read_quota() -> dict:
    try:
        return json.loads(ODDS_QUOTA_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _note_quota(resp) -> None:
    """Persist what the API reports about our remaining credits."""
    headers = getattr(resp, "headers", None) or {}
    rem = headers.get("x-requests-remaining")
    if rem is None:
        return
    try:
        remaining = float(rem)
    except ValueError:
        return
    try:
        ODDS_QUOTA_FILE.write_text(json.dumps(dict(
            remaining=remaining,
            used=headers.get("x-requests-used"),
            last_call_cost=headers.get("x-requests-last"),
            checked=datetime.now(timezone.utc).isoformat(timespec="seconds"))))
    except OSError:
        pass
    if remaining <= ODDS_MIN_REMAINING:
        log.warning("Odds API credits down to %.0f (reserve %.0f) — pausing "
                    "paid odds calls until the monthly reset.",
                    remaining, ODDS_MIN_REMAINING)


def budget_left() -> bool:
    """False when the last known balance is at or under the reserve."""
    rem = read_quota().get("remaining")
    return True if rem is None else float(rem) > ODDS_MIN_REMAINING


def fetch_games(api_key: str, sport: str) -> list:
    if not budget_left():
        raise OddsBudgetExhausted(
            f"odds credits at or below the {ODDS_MIN_REMAINING:.0f} reserve")
    resp = requests.get(
        ODDS_URL.format(sport=sport),
        params={"apiKey": api_key, "regions": ODDS_REGIONS,
                "markets": "h2h,totals", "oddsFormat": "decimal"},
        timeout=20,
    )
    _note_quota(resp)
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
    if not covers_game(market, game):
        # The label named a team that plays tonight, but this contract does not
        # settle on tonight's game — a championship future, a series price, a
        # season win total. Pricing it off a single game's win probability is
        # how we bought Atlanta at 8c to win the WNBA title.
        log.warning("SKIP %s: settles %s, not near %s kickoff — not this game",
                    market.get("ticker"),
                    (market_expiry(market) or "?"), game.get("commence_time"))
        return []
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

    # SIGNED steam, per side: positive means the sharp line moved toward the
    # side we are backing (we are late), negative means it moved away (we are
    # early, or fading the drift). Recorded, never assumed — see the note on
    # SPORTS_REQUIRE_STEAM.
    def steam_for(back_side: str):
        if move_home is None:
            return None
        return move_home if back_side == "home" else -move_home

    signals = []
    yes_ask = price_cents(market, "yes_ask")
    if (yes_ask and 0 < yes_ask < 100 and steam_ok(side)
            and p >= SPORTS_MIN_CONFIDENCE):
        ev = 100.0 * p - yes_ask - taker_fee_cents(yes_ask)
        if edge_ok(ev, market.get("ticker")):
            signals.append(dict(side="yes", price_cents=yes_ask,
                                model_prob=p, ev_cents=ev,
                                steam=steam_for(side), backing=side))
    yes_bid = price_cents(market, "yes_bid")
    if (yes_bid and 0 < yes_bid < 100 and steam_ok(other)
            and (1.0 - p) >= SPORTS_MIN_CONFIDENCE):
        no_price = 100.0 - yes_bid
        ev = 100.0 * (1.0 - p) - no_price - taker_fee_cents(no_price)
        if edge_ok(ev, market.get("ticker")):
            signals.append(dict(side="no", price_cents=no_price,
                                model_prob=1.0 - p, ev_cents=ev,
                                steam=steam_for(other), backing=other))
    for s in signals:
        # is_underdog records whether the side we backed is priced under 50c.
        # Recorded, not rewarded: the favourite-longshot literature says dogs
        # are systematically OVERpriced, so this exists to be tested against
        # our own CLV, exactly like steam and is_home.
        s["is_underdog"] = s["price_cents"] < 50
        # is_home records whether the side we backed is the home team. The
        # home-underdog edge is real in the literature but largely arbitraged
        # away (Gray & Gray: profitable 7 of 8 seasons, then 3 of 11; recent
        # work finds the NFL home bias "nearly eliminated"). So it is logged as
        # a feature to test against our own CLV, NOT used to adjust the price.
        s.update(ticker=market.get("ticker"), subtitle=label,
                 is_home=(s.get("backing") == "home"))
    return signals


def _sports_placed_today(kind: str = "all", now: datetime = None) -> int:
    """How many real sports orders were placed today — kind 'ml', 'totals',
    or 'all'. Totals live in KX*TOTAL tickers; moneylines don't. The two
    daily budgets count against their own kind."""
    from ledger import EXEC_LOG
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if not EXEC_LOG.exists():
        return 0
    n = 0
    with open(EXEC_LOG, newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("model") != "sports"
                    or not (row.get("placed_at_utc") or "").startswith(today)):
                continue
            is_total = "TOTAL" in (row.get("ticker") or "").upper()
            if kind == "all" or (kind == "totals") == is_total:
                n += 1
    return n


def sports_deployed_today(now: datetime = None) -> float:
    """USD of real sports orders placed today, from the executed ledger.

    The per-order caps bound one bet; this bounds the DAY. Both are needed:
    with full Kelly and a 20% ceiling, the 4-orders-a-day budget alone would
    permit roughly 80% of the bankroll out the door between two sunrises,
    which is not what a daily order count was ever meant to allow."""
    from ledger import EXEC_LOG
    now = now or datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if not EXEC_LOG.exists():
        return 0.0
    total = 0.0
    with open(EXEC_LOG, newline="") as fh:
        for row in csv.DictReader(fh):
            if (row.get("model") != "sports"
                    or not (row.get("placed_at_utc") or "").startswith(today)):
                continue
            try:
                total += float(row.get("cost_usd") or 0)
            except (TypeError, ValueError):
                continue
    return total


def scan(api_key: str) -> list:
    """Selective sharp-line tracker: collect every play that clears the
    steam + confidence + edge gates across all games, then return only the
    top few by edge, capped so at most SPORTS_MAX_PER_DAY are placed per day.
    The cap reads the executed ledger, which the runner appends to as it
    places, so the budget holds across polls within a session. Result shape
    matches the other models; 'date' carries the event ticker."""
    client = KalshiClient(env="prod")
    history = load_line_history()   # sharp lines as of the previous run
    new_history = {}                # what we'll persist for the next run
    try:
        active = in_season_sports(api_key)
    except Exception as exc:
        log.warning("Could not fetch in-season list (%s); trying all sports", exc)
        active = {c["sport"] for c in SERIES}

    try:                            # don't spend budget on markets we hold
        positions = client.get_positions()
        held = {p.get("ticker") for p in positions.get("market_positions", [])
                if float(p.get("position", 0) or 0) != 0}
    except Exception:
        held = set()

    ml_cands, tot_cands = [], []    # moneyline / totals, budgeted separately
    for cfg in SERIES:
        if not league_enabled(cfg):
            log.info("%s: not in SPORTS_LEAGUES, skipping", cfg["name"])
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

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for g in games:            # steam memory: sharp win-prob AND total
            if not g.get("id"):
                continue
            hp, tm = fair_home_prob(g), fair_total_mean(g)
            rec = dict(home_team=g.get("home_team"),
                       away_team=g.get("away_team"), updated=now_iso)
            if hp is not None:
                rec["home_prob"] = round(hp, 4)
            if tm is not None:
                rec["total_mean"] = round(tm, 3)
            new_history[g["id"]] = rec

        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": cfg["series"], "status": "open",
                        "with_nested_markets": "true", "limit": 60})
        except Exception as exc:
            log.warning("Skipping %s markets: %s", cfg["name"], exc)
            data = {"events": []}
        for event in data.get("events", []):
            event_ticker = event.get("event_ticker") or event.get("ticker") or ""
            for market in event.get("markets") or []:
                if market.get("status") not in (None, "active", "open"):
                    continue
                for s in evaluate_market(market, games, history) or []:
                    if s["ticker"] in held:
                        continue
                    ml_cands.append(dict(event_ticker=event_ticker,
                                         title=event.get("title", ""),
                                         league=cfg["name"], signal=s))

        # --- totals (over/under) for this league, if Kalshi lists them ---
        series = TOTALS_SERIES.get(cfg["sport"])
        if not series:
            continue
        try:
            tdata = client._request(
                "GET", "/events",
                params={"series_ticker": series, "status": "open",
                        "with_nested_markets": "true", "limit": 60})
        except Exception as exc:
            log.warning("Skipping %s totals: %s", cfg["name"], exc)
            continue
        for event in tdata.get("events", []):
            game = match_total_game(event.get("title"), games)
            if not game:
                continue
            mean = fair_total_mean(game)
            if mean is None:
                continue
            prev = (history or {}).get(game.get("id")) or {}
            pm = prev.get("total_mean")
            move = (mean - pm) if pm is not None else None
            et = event.get("event_ticker") or event.get("ticker") or ""
            for market in event.get("markets") or []:
                if market.get("status") not in (None, "active", "open"):
                    continue
                # Same settlement check the moneyline path applies. A totals
                # ladder is per-game today, but the title match that got us
                # here is as loose as match_team was, so hold it to the same
                # standard rather than trusting the ticker convention.
                if not covers_game(market, game):
                    log.warning("SKIP %s: settles %s, not near %s start",
                                market.get("ticker"),
                                (market_expiry(market) or "?"),
                                game.get("commence_time"))
                    continue
                for s in evaluate_total_market(market, mean, move):
                    if s["ticker"] in held:
                        continue
                    tot_cands.append(dict(event_ticker=et,
                                          title=event.get("title", ""),
                                          league=cfg["name"] + " O/U",
                                          signal=s))
    if new_history:
        save_line_history(new_history)

    # separate daily budgets: the best few moneylines AND the best few totals
    chosen = []
    for cands, kind, cap in ((ml_cands, "ml", SPORTS_MAX_ML_PER_DAY),
                             (tot_cands, "totals", SPORTS_MAX_TOTALS_PER_DAY)):
        placed = _sports_placed_today(kind)
        budget = max(0, cap - placed)
        cands.sort(key=lambda c: -c["signal"]["ev_cents"])
        take = cands[:budget]
        log.info("Sports %s: %d qualifying, %d placed today, budget %d -> %d",
                 kind, len(cands), placed, budget, len(take))
        chosen.extend(take)

    by_event = {}
    for c in chosen:
        g = by_event.setdefault(c["event_ticker"],
                                dict(date=c["event_ticker"], mu=0.0,
                                     city=c["league"], title=c["title"],
                                     signals=[]))
        g["signals"].append(c["signal"])
    return list(by_event.values())


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
