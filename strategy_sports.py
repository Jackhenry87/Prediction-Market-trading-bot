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

import collections
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

import pitchers

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
# A call costs markets x regions. "h2h,totals" over "us" is 2 credits; ONE
# market is 1. That factor of two is the difference between guaranteeing a
# daily refresh on a depleted quota and falling ~25% short of one.
ODDS_MARKETS = os.getenv("ODDS_MARKETS", "h2h,totals")
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
# Above this claimed edge, the STARTING PITCHING must not contradict the side
# we are backing. Below it, the pitcher check is recorded but not enforced.
#
# The books already price the starters, so this cannot sharpen our fair value —
# it would be double-counting the same information. What it catches is a number
# that is not about this game at all. Both bad trades this month were exactly
# that: a WNBA championship future priced off one game (58c "edge"), and "Los
# Angeles A" priced off the DODGERS (21.4c "edge") while the Angels ran a 7.27
# ERA starter against Texas's 3.56. Neither was an edge; both were a different
# game. A large edge that the pitching flatly contradicts is the signature.
SPORTS_CORROBORATE_ABOVE_CENTS = float(
    os.getenv("SPORTS_CORROBORATE_ABOVE_CENTS", "10"))

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
# ONE daily budget across both bet types, filled by edge (owner call,
# 2026-08-10: "whatever the bot is most confident in that day").
#
# It used to be two reserved pools of 2. Reserved slots mean the day's best
# four picks are NOT what gets taken: if moneylines show nothing, their two
# slots simply go unused while a fifth-best total is left on the table — and
# moneylines showed nothing on every run for a week. A single pool ranked by
# edge takes the four the model is actually most confident in, whatever they
# are.
#
# The per-kind ceilings survive as OPTIONAL limits, unset by default, for
# capping concentration in one bet type if CLV ever shows it deserves it.
SPORTS_MAX_PER_DAY = int(os.getenv("SPORTS_MAX_PER_DAY", "4"))
SPORTS_MAX_ML_PER_DAY = int(os.getenv("SPORTS_MAX_ML_PER_DAY",
                                      str(SPORTS_MAX_PER_DAY)))
SPORTS_MAX_TOTALS_PER_DAY = int(os.getenv("SPORTS_MAX_TOTALS_PER_DAY",
                                          str(SPORTS_MAX_PER_DAY)))

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
        # than defaulting to False and inventing a category.
        #
        # is_underdog IS set, and means "we backed the cheaper side" — not an
        # underdog TEAM, which a total does not have. Without it every totals
        # signal carried None, and with ODDS_MARKETS=totals that is every
        # signal we produce, so the feature CLV is meant to test would have
        # been absent from 100% of the data it was added to explain.
        s.update(ticker=market.get("ticker"), subtitle=label, is_home=None,
                 is_underdog=s["price_cents"] < 50)
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


# Why moneyline candidates die. Three separate wrong guesses about "0
# qualifying" — first the series ticker, then the confidence floor, then the
# settlement guard — is two too many. The gates now count themselves, so the
# next run says which one is binding instead of us inferring it from silence.
REJECTS = collections.Counter()


def _reject(reason: str) -> None:
    REJECTS[reason] += 1


def edge_ok(ev_cents: float, ticker: str = "", counter: bool = True) -> bool:
    """Is this edge in the range a real disagreement can produce?

    Both ends matter. Below MIN_EDGE_CENTS the fee eats it; above
    SPORTS_MAX_EDGE_CENTS it is not an edge at all but a sign we priced the
    wrong contract, and that end is the dangerous one — Kelly stakes hardest
    exactly where the number is most absurd."""
    if ev_cents < MIN_EDGE_CENTS:
        if counter:
            _reject("edge below floor" if ev_cents > 0 else "no edge")
        return False
    if ev_cents > SPORTS_MAX_EDGE_CENTS:
        if counter:
            _reject("edge above sanity ceiling")
        log.warning("SKIP %s: %.1fc edge exceeds the %.0fc sanity ceiling — "
                    "treating this as a mispriced match, not an opportunity",
                    ticker or "?", ev_cents, SPORTS_MAX_EDGE_CENTS)
        return False
    return True


def _words(text: str) -> set:
    """Kept for callers that want a loose bag of words. Not used for matching
    a team any more — see _tokens and label_matches_team."""
    return {w for w in re.split(r"[^A-Za-z]+", (text or "").upper())
            if len(w) >= 3 and w not in ("THE", "LOS", "NEW", "SAN")}


def _tokens(text: str) -> list:
    """ORDERED tokens, keeping short ones. The short ones are the whole point:
    Kalshi disambiguates same-city teams with a trailing initial."""
    return [w for w in re.split(r"[^A-Za-z]+", (text or "").upper()) if w]


def label_matches_team(label: str, team: str) -> bool:
    """Does this Kalshi label name this team?

    The old matcher discarded every token shorter than three characters, which
    threw away exactly the character that disambiguates: "New York Y" and
    "New York M" both collapsed to {YORK}, matched BOTH New York teams, and
    were dropped as ambiguous. One live run rejected 71 moneyline candidates
    that way — more than every other reason combined, and the real reason the
    moneyline leg never once qualified.

    Kalshi's forms, all seen live: "Boston", "New York Y", "Chicago C",
    "Chicago WS", "Los Angeles D". So after the city words match exactly, a
    single remaining token must either PREFIX the nickname ("Y" -> Yankees) or
    be its INITIALS ("WS" -> White Sox).
    """
    lt, tt = _tokens(label), _tokens(team)
    if not lt or not tt:
        return False
    i = 0
    while i < len(lt) and i < len(tt) and lt[i] == tt[i]:
        i += 1
    rest_label, rest_team = lt[i:], tt[i:]
    if not rest_label:
        return True                      # label is a leading part of the name
    if len(rest_label) != 1 or not rest_team:
        return False
    token = rest_label[0]
    if rest_team[0].startswith(token):
        return True
    return token == "".join(w[0] for w in rest_team)


def match_team(label: str, games: list):
    """Find which game/side a Kalshi team label refers to. Returns
    (game, 'home'|'away') only when the match is unambiguous — one team in
    one game. Anything unclear is skipped rather than guessed."""
    if not _tokens(label):
        return None
    hits = [(g, side) for g in games for side in ("home", "away")
            if label_matches_team(label, g.get(f"{side}_team"))]
    if len(hits) == 1:
        return hits[0]
    if hits:
        return None              # genuinely ambiguous, e.g. a bare "New York"

    # Fallback for a NICKNAME-ONLY label ("Yankees"), which is not a leading
    # part of "New York Yankees" and so cannot match the precise rule.
    #
    # RESTRICTED TO A SINGLE TOKEN, and that restriction is load-bearing. The
    # first version applied it to any label the precise rule missed, and on
    # 2026-08-12 it bought "Los Angeles A" priced off the DODGERS: the Angels'
    # game had already started and been filtered out, so the precise rule found
    # nothing, and _words("Los Angeles A") is {ANGELES} — "LOS" is a stopword
    # and "A" is too short to survive — which is a subset of
    # {ANGELES, DODGERS}. Both games were that same night in that same city, so
    # covers_game could not tell them apart either, and a 21.4c "edge" against
    # a 45-74 team passed the 25c ceiling and staked 19% of the bankroll.
    #
    # A multi-token label names a city it must match precisely or not at all.
    # Only a bare nickname may fall back.
    tokens = _tokens(label)
    if len(tokens) != 1:
        return None
    words = _words(label)
    if not words:
        return None
    loose = [(g, side) for g in games for side in ("home", "away")
             if words <= _words(g.get(f"{side}_team"))]
    return loose[0] if len(loose) == 1 else None


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


# Odds cache. The model must keep evaluating on EVERY run; only the paid
# refresh is rationed. Without this, "we cannot afford a call right now" and
# "the model does not run" were the same thing — on 2026-08-10 the free tier
# hit its reserve and the sports model went completely dark, placing nothing
# for the rest of the month rather than trading on the lines it already had.
#
# The raw payload is cached and re-filtered by start time on every use, so a
# cached slate naturally drops games that have already begun.
ODDS_CACHE_FILE = Path(__file__).resolve().parent / "odds_cache.json"
ODDS_CACHE_TTL_MIN = float(os.getenv("ODDS_CACHE_TTL_MIN", "1200"))   # 20h

# Only pay for a refresh when a game is within this many hours. The whole
# monthly budget is 500 credits = 250 calls at h2h+totals over one region, so
# every avoided call is ~0.4% of the month. A US sport's slate is clustered in
# the evening, so gating on proximity skips the overnight refreshes entirely.
ODDS_REFRESH_LEAD_H = float(os.getenv("ODDS_REFRESH_LEAD_H", "14"))


def _cache_all() -> dict:
    try:
        return json.loads(ODDS_CACHE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def cached_odds(sport: str):
    """(payload, age_minutes) for this sport, or (None, None)."""
    entry = _cache_all().get(sport)
    if not isinstance(entry, dict) or "games" not in entry:
        return None, None
    fetched = parse_iso(entry.get("fetched_at"))
    if fetched is None:
        return None, None
    age = (datetime.now(timezone.utc) - fetched).total_seconds() / 60.0
    return entry["games"], age


def _cache_store(sport: str, games: list) -> None:
    data = _cache_all()
    data[sport] = dict(
        fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        games=games)
    try:
        ODDS_CACHE_FILE.write_text(json.dumps(data))
    except OSError as exc:
        log.warning("Could not write the odds cache: %s", exc)


def _in_window(games: list) -> list:
    """Only games inside the tradeable start-time window, evaluated NOW —
    which is why the cache holds the raw payload rather than the filtered one."""
    out = []
    for game in games or []:
        h = hours_until(game.get("commence_time"))
        if h is not None and MIN_START_H <= h <= MAX_START_H:
            out.append(game)
    return out


def fetch_games(api_key: str, sport: str) -> list:
    """Lines for `sport`, from cache when it is fresh or when credits are gone.

    Order of preference: a fresh cache (free), a paid refresh (2 credits), a
    STALE cache (free). Only an empty cache with no budget is fatal — running
    on yesterday's lines is worse than running on today's, and far better than
    not running, which is what the model did before this existed.
    """
    games, age = cached_odds(sport)
    if games is not None and age is not None and age < ODDS_CACHE_TTL_MIN:
        log.info("%s: using cached lines (%.0f min old, TTL %.0f)",
                 sport, age, ODDS_CACHE_TTL_MIN)
        return _in_window(games)

    # Do not pay to re-price a slate we cannot bet on yet. Lines matter as the
    # game approaches; refreshing at 06:00 UTC when the first pitch is fourteen
    # hours away buys a number that will have moved before it is usable. The
    # cached slate tells us when the next game starts, so this costs nothing to
    # check. Overnight is roughly half the day for a US sport, so skipping it
    # is close to a 2x saving on a fixed TTL.
    if games is not None:
        soonest = min((h for h in (hours_until(g.get("commence_time"))
                                   for g in games) if h is not None and h > 0),
                      default=None)
        if soonest is not None and soonest > ODDS_REFRESH_LEAD_H:
            log.info("%s: next game %.1fh away (> %.0fh lead) — keeping the "
                     "%.0f min old cache instead of spending a credit",
                     sport, soonest, ODDS_REFRESH_LEAD_H, age or 0.0)
            return _in_window(games)

    if not budget_left():
        if games is not None:
            log.warning("%s: odds credits at the %.0f reserve — running on "
                        "STALE cached lines (%.0f min old)",
                        sport, ODDS_MIN_REMAINING, age or 0.0)
            return _in_window(games)
        raise OddsBudgetExhausted(
            f"odds credits at or below the {ODDS_MIN_REMAINING:.0f} reserve "
            f"and no cached {sport} lines to fall back on")

    resp = requests.get(
        ODDS_URL.format(sport=sport),
        params={"apiKey": api_key, "regions": ODDS_REGIONS,
                "markets": ODDS_MARKETS, "oddsFormat": "decimal"},
        timeout=20,
    )
    _note_quota(resp)
    resp.raise_for_status()
    payload = resp.json()
    _cache_store(sport, payload)
    return _in_window(payload)


def evaluate_market(market: dict, games: list, history: dict = None,
                    matchups: dict = None) -> list:
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "")
    matched = match_team(label, games)
    if not matched:
        _reject("no unambiguous team match")
        return []
    game, side = matched
    if not covers_game(market, game):
        _reject("settles on a different game")
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
    if not (yes_ask and 0 < yes_ask < 100):
        _reject("no usable ask")
    elif not steam_ok(side):
        _reject("steam gate (SPORTS_REQUIRE_STEAM)")
    elif p < SPORTS_MIN_CONFIDENCE:
        _reject("below the probability floor")
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
    # Starting pitching: recorded on every signal, ENFORCED only above
    # SPORTS_CORROBORATE_ABOVE_CENTS. Missing data fails open — vetoing every
    # game with an unannounced starter would disable the model on mornings when
    # lineups are not yet posted, a worse failure than the one it prevents.
    mu = (matchups or {}).get(game.get("id"))
    kept = []
    for s in signals:
        s["home_era"] = (mu or {}).get("home_era")
        s["away_era"] = (mu or {}).get("away_era")
        s["pitcher_lean"] = pitchers.favours(mu)
        if (s["ev_cents"] >= SPORTS_CORROBORATE_ABOVE_CENTS
                and not pitchers.supports_side(mu, s["backing"])):
            _reject("pitching contradicts a large edge")
            log.warning("SKIP %s: %.1fc edge backing %s, but the starters "
                        "favour %s (home ERA %s vs away ERA %s)",
                        market.get("ticker"), s["ev_cents"], s["backing"],
                        s["pitcher_lean"], s["home_era"], s["away_era"])
            continue
        kept.append(s)
    signals = kept

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
    REJECTS.clear()      # per-scan, not cumulative across polls

    # Probable starters for every scheduled game, once per scan. Free endpoint,
    # no key, no odds credits — the same scoreboard live_state already polls.
    # A failure here must not stop the model: the pitcher check is a veto on
    # implausible edges, not a precondition for trading, so an empty map simply
    # means nothing gets vetoed.
    try:
        _pm = pitchers.matchups()
        log.info("Probable starters loaded for %d scheduled game(s)", len(_pm))
    except Exception as exc:
        log.warning("Pitcher matchups unavailable (%s) — edges will not be "
                    "corroborated this scan", exc)
        _pm = []

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

        # Keyed by the odds-API game id. The team matcher is INJECTED so this
        # cannot become a second, subtly different naming rule — the one that
        # crossed the Angels with the Dodgers was exactly that kind of drift.
        matchup_by_id = {}
        for g in games:
            if not g.get("id"):
                continue
            # commence_time is load-bearing: Texas and the Angels played three
            # consecutive nights with a different starter each time, so a
            # team-only match returns whichever game came first in the slate
            # and the veto corroborates against the wrong pitchers.
            found = pitchers.find(_pm, g.get("home_team"), g.get("away_team"),
                                  label_matches_team,
                                  commence=g.get("commence_time"))
            if found:
                matchup_by_id[g["id"]] = found

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
                for s in evaluate_market(market, games, history,
                                         matchup_by_id) or []:
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

    if REJECTS:
        log.info("Moneyline candidates rejected: %s",
                 ", ".join(f"{n}x {why}"
                           for why, n in REJECTS.most_common()))

    # ONE budget, filled by edge, whatever the bet type. Per-kind ceilings
    # still apply if someone sets them, but they no longer RESERVE slots — an
    # empty moneyline day no longer costs the day two picks.
    for cands, kind in ((ml_cands, "ml"), (tot_cands, "totals")):
        log.info("Sports %s: %d qualifying, %d placed today", kind,
                 len(cands), _sports_placed_today(kind))

    budget = max(0, SPORTS_MAX_PER_DAY - _sports_placed_today("all"))
    pool = sorted(ml_cands + tot_cands,
                  key=lambda c: -c["signal"]["ev_cents"])
    chosen, per_kind = [], {"ml": _sports_placed_today("ml"),
                            "totals": _sports_placed_today("totals")}
    caps = {"ml": SPORTS_MAX_ML_PER_DAY, "totals": SPORTS_MAX_TOTALS_PER_DAY}
    for c in pool:
        if len(chosen) >= budget:
            break
        kind = "totals" if "TOTAL" in c["signal"]["ticker"].upper() else "ml"
        if per_kind[kind] >= caps[kind]:
            continue
        per_kind[kind] += 1
        chosen.append(c)
    log.info("Sports: %d candidate(s) across both types, budget %d -> taking "
             "%d by edge", len(pool), budget, len(chosen))

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
