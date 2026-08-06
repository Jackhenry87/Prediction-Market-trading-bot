"""Our OWN probability estimate for a market he just bet.

THE DESIGN DECISION THIS MODULE ENCODES
---------------------------------------
A copied trade is not a signal to bet — it is a signal to LOOK. The owner's
call: his trade is the trigger, and the `p` that goes into Kelly comes from
this repo's own models. If no model of ours can price the market, the copier
does not place. Ever.

That rule exists because of what his record actually shows. Across 115
settled positions he is +$448,676 — but $586,308 of that is five trades.
Remove them and the record is -$137,632. His stakes also land on round $1,000
tiers (52% of his losses are exactly $100,000.00, $50,000.00, $20,000.00 …),
which is hand-typed sizing, not Kelly output. So "he bet it" is not evidence
that a bet is good. Our model has to agree.

WHY NOT JUST CALL scan()
------------------------
Each strategy's `scan()` applies its own edge and steam filters
(`strategy_sports.MIN_EDGE_CENTS = 5.0`, `SPORTS_REQUIRE_STEAM`, the daily
cap) before emitting a signal. A market absent from `scan()` output is not a
market the model has no view on — it is one the model would not have picked
on its own. We want the view, so we call the underlying fair-probability
functions directly and let the copier do its own EV maths.

API BUDGET
----------
`ODDS_API_KEY` is a 500-request/month free tier. Nothing here polls on a
timer: probabilities are fetched only when a notification actually arrives,
behind a TTL cache, and the first-inning path fetches odds for the ONE game
in question rather than the whole slate.

COVERAGE IS PARTIAL, BY DESIGN
------------------------------
Of his 115 settled trades roughly a third fall in reach of our models (MLB
and NBA moneylines, game totals, first-inning runs). The rest — World Cup
soccer, golf, tennis, UFC, spreads, combos — get (None, None) and are logged
to the paper ledger without an order. Notably his single largest winner
($205k on a Chicago C moneyline) IS in covered territory.
"""

import os
import time

import strategy_crypto
import strategy_nrfi
import strategy_sports
import strategy_weather
from trade_logger import get_logger

log = get_logger("follow_prob")

ODDS_TTL = float(os.getenv("FOLLOW_ODDS_TTL", "300"))
ODDS_BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"

# Kalshi series prefix -> the odds-API sport key our sports model uses.
SPORTS_SERIES = {
    "KXMLBGAME": "baseball_mlb",
    "KXNBA": "basketball_nba",
    "KXNFLGAME": "americanfootball_nfl",
    "KXNHLGAME": "icehockey_nhl",
    "KXWNBA": "basketball_wnba",
}

# Market families we deliberately do NOT price. Spreads, correct scores,
# goalscorers and combos are real markets he trades often — we simply have no
# model for them, and saying so is the honest answer.
UNSUPPORTED_MARKS = (
    "spread", "correct score", "goalscorer", "combo", "top 20",
    "winner", "advance", "btts", "both teams to score", "1st quarter",
    "first half", "1h ", "regulation time team total",
)

_odds_cache = {}          # sport -> (fetched_at, games)


def _cached_games(sport: str, api_key: str) -> list:
    """Odds-API games for a sport, cached. Empty list on any failure — a
    dead odds feed must mean 'no view', never a crash mid-copy."""
    hit = _odds_cache.get(sport)
    if hit and time.time() - hit[0] < ODDS_TTL:
        return hit[1]
    try:
        games = strategy_sports.fetch_games(api_key, sport)
    except Exception as exc:
        log.warning("odds fetch for %s failed: %s", sport, exc)
        games = (hit[1] if hit else [])
    _odds_cache[sport] = (time.time(), games)
    return games


def series_of(ticker: str) -> str:
    """KXMLBGAME-26AUG06LADCHC-CHC -> KXMLBGAME."""
    return (ticker or "").split("-")[0].upper()


def sport_for(ticker: str):
    """The odds-API sport key for a Kalshi ticker, or None."""
    series = series_of(ticker)
    for prefix, sport in SPORTS_SERIES.items():
        if series.startswith(prefix):
            return sport
    return None


def is_unsupported(event_title: str, market: dict) -> bool:
    """True for market families no model here can price."""
    blob = " ".join(str(x or "") for x in (
        event_title, market.get("title"), market.get("subtitle"))).lower()
    return any(w in blob for w in UNSUPPORTED_MARKS)


def is_total_market(event_title: str, market: dict) -> bool:
    blob = f"{event_title or ''} {market.get('title') or ''}".lower()
    return "total" in blob and "runs" in blob or "total goals" in blob


# --------------------------- per-model routes ---------------------------

def sports_moneyline_prob(market: dict, ticker: str, api_key: str):
    """P(YES) for a moneyline market: the devigged, Pinnacle-weighted sharp
    consensus, flipped to whichever team this market's YES represents."""
    sport = sport_for(ticker)
    if not sport:
        return None
    games = _cached_games(sport, api_key)
    if not games:
        return None
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "")
    hit = strategy_sports.match_team(label, games)
    if not hit:
        log.info("no unambiguous team match for %r", label)
        return None
    game, side = hit
    p_home = strategy_sports.fair_home_prob(game)
    if p_home is None:
        return None
    return p_home if side == "home" else 1.0 - p_home


def sports_total_prob(market: dict, event_title: str, ticker: str,
                      api_key: str):
    """P(YES) for an 'Over X.5' market from the books' implied mean total."""
    sport = sport_for(ticker)
    if not sport:
        return None
    games = _cached_games(sport, api_key)
    game = strategy_sports.match_total_game(event_title, games)
    if not game:
        return None
    mean = strategy_sports.fair_total_mean(game)
    strike = market.get("floor_strike")
    if mean is None or strike is None:
        return None
    try:
        return strategy_sports.over_prob(mean, float(strike))
    except (TypeError, ValueError):
        return None


def nrfi_prob(event_title: str, api_key: str):
    """P(YES = a run scores in the first inning), from the sharp
    first-inning total for THIS game only.

    Deliberately targeted: the runner's `_odds_first_inning` walks the whole
    slate at one API credit per game. We spend one credit, not fifteen.
    """
    import requests
    try:
        evs = requests.get(f"{ODDS_BASE}/events",
                           params={"apiKey": api_key}, timeout=20)
        evs.raise_for_status()
        game = strategy_sports.match_total_game(event_title, evs.json())
        if not game:
            return None
        r = requests.get(f"{ODDS_BASE}/events/{game['id']}/odds",
                         params={"apiKey": api_key, "regions": "us",
                                 "markets": "totals_1st_1_innings",
                                 "oddsFormat": "decimal"}, timeout=20)
        r.raise_for_status()
    except Exception as exc:
        log.warning("first-inning odds failed: %s", exc)
        return None

    probs = []
    for book in r.json().get("bookmakers", []):
        for m in book.get("markets", []):
            if m.get("key") != "totals_1st_1_innings":
                continue
            over = under = None
            for o in m.get("outcomes", []):
                if o.get("name") == "Over":
                    over = o.get("price")
                elif o.get("name") == "Under":
                    under = o.get("price")
            p = strategy_nrfi.fair_yrfi(over, under)
            if p is not None:
                probs.append(p)
    return strategy_nrfi.consensus_yrfi(probs)


def crypto_prob(market: dict, ticker: str):
    """P(YES) for a BTC/ETH bucket under the zero-drift lognormal model."""
    series = series_of(ticker)
    asset = next((a for a in strategy_crypto.ASSETS
                  if series.startswith(a["series"])), None)
    if not asset:
        return None
    try:
        spot, sigma = strategy_crypto.get_spot_and_vol(asset["pair"])
    except Exception as exc:
        log.warning("crypto spot/vol failed: %s", exc)
        return None
    tau_h = strategy_crypto.hours_to_close(market)
    if tau_h is None or tau_h <= 0:
        return None
    return strategy_crypto.bucket_probability(
        spot, sigma, tau_h / (24 * 365),
        market.get("floor_strike"), market.get("cap_strike"))


def weather_prob(market: dict, event_ticker: str, ticker: str):
    """P(YES) for a daily-high bucket from the NWS forecast, de-biased and
    widened by the station's measured error."""
    from nws import get_daily_high_forecasts

    series = series_of(ticker)
    city = next((c for c in strategy_weather.CITIES
                 if series.startswith(c["series"])), None)
    if not city:
        return None
    date = strategy_weather.date_from_event_ticker(event_ticker or ticker)
    if not date:
        return None
    cal = strategy_weather.station_cal(city)
    try:
        forecasts = get_daily_high_forecasts(city["lat"], city["lon"],
                                             city["name"])
    except Exception as exc:
        log.warning("NWS forecast failed for %s: %s", city["name"], exc)
        return None
    if date not in forecasts:
        return None
    mu = forecasts[date] - cal["bias"]
    return strategy_weather.bucket_probability(
        mu, market.get("floor_strike"), market.get("cap_strike"),
        sigma=cal["sigma"])


# ------------------------------ the router ------------------------------

def model_prob(ticker: str, market: dict, event_ticker: str = "",
               event_title: str = "", odds_api_key: str = "") -> tuple:
    """(p, model_name) for this market, or (None, None) when no model of ours
    can price it. Never raises — a failure to form a view is a skip, not an
    outage."""
    try:
        if is_unsupported(event_title, market):
            return None, None

        series = series_of(ticker)

        if series.startswith("KXMLBRFI") or "first inning" in (
                event_title or "").lower():
            if not odds_api_key:
                return None, None
            p = nrfi_prob(event_title, odds_api_key)
            return (p, "nrfi") if p is not None else (None, None)

        if sport_for(ticker):
            if not odds_api_key:
                return None, None
            if is_total_market(event_title, market):
                p = sports_total_prob(market, event_title, ticker,
                                      odds_api_key)
                return (p, "sports-total") if p is not None else (None, None)
            p = sports_moneyline_prob(market, ticker, odds_api_key)
            return (p, "sports") if p is not None else (None, None)

        if series.startswith(("KXBTC", "KXETH")):
            p = crypto_prob(market, ticker)
            return (p, "crypto") if p is not None else (None, None)

        if series.startswith(("KXHIGH", "KXLOW")):
            p = weather_prob(market, event_ticker, ticker)
            return (p, "weather") if p is not None else (None, None)

    except Exception as exc:
        log.warning("model_prob failed for %s: %s", ticker, exc)
        return None, None

    log.info("no model covers %s (%s) — skipping", ticker, event_title)
    return None, None
