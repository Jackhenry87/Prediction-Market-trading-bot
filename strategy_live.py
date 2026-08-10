"""LIVE (in-play) model: win expectancy from game state vs Kalshi's live price.

    python strategy_live.py     # read-only scan, prints every comparison

THE THESIS, AND WHY IT IS ONLY A THESIS
---------------------------------------
Pregame, our edge came from devigging sportsbook consensus. In play there is no
free consensus to devig, so fair value is computed from the game itself: base,
out, inning, score -> P(home wins). If Kalshi's live price diverges from that by
more than fees, one of us is wrong.

The first live observation says it is usually us. On 2026-08-09, Houston at San
Diego, bottom of the 2nd, San Diego down 2-1:

    model    42.8c
    Kalshi   43c bid / 44c ask     spread 1c, volume 2.4M contracts

A 1.2c disagreement against a 1c spread, with a taker fee near 1.7c at that
price. The in-play market is the TIGHTEST on the exchange, not the loosest —
tomorrow's game in the same series quoted 7,290 contracts against 2.4 million.

So the gates here are deliberately harsher than the pregame model's:

  * LIVE_MIN_EDGE_CENTS is larger, because the fee is paid on every entry and
    a 1c spread leaves nothing to spare.
  * The edge ceiling is TIGHTER. A huge in-play disagreement almost always
    means our state is stale or misparsed — a run scored that we have not seen
    — and stale state is the single most dangerous input to a live model.
  * Games in the last outs of a blowout are skipped: win expectancy there is
    ~1.0, the price is 98-99c, and there is nothing to win but plenty to lose
    to a bad parse.

Nothing here places an order. live_scan.py records what it finds so the
question "does a live edge exist at our latency?" gets a measured answer.
"""

import os
import sys

import live_state
import win_expectancy as we
from kalshi_client import KalshiClient
from strategy_sports import covers_game, market_expiry, parse_iso
from strategy_weather import price_cents, taker_fee_cents
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_live")

SERIES = os.getenv("LIVE_SERIES", "KXMLBGAME")

# Harsher than pregame (5c): the spread is 1c and the fee near 1.7c, so a 5c
# "edge" here is a far smaller cushion than the same number pregame.
LIVE_MIN_EDGE_CENTS = float(os.getenv("LIVE_MIN_EDGE_CENTS", "7"))
# Tighter than pregame (25c). A 30c in-play disagreement is a stale scoreboard,
# not an opportunity.
LIVE_MAX_EDGE_CENTS = float(os.getenv("LIVE_MAX_EDGE_CENTS", "20"))
# Stay out of decided games: no edge to win, full exposure to a misparse.
LIVE_MIN_PROB = float(os.getenv("LIVE_MIN_PROB", "0.10"))
LIVE_MAX_PROB = float(os.getenv("LIVE_MAX_PROB", "0.90"))


def _words(text):
    return {w for w in (text or "").upper().replace(".", " ").split()
            if len(w) >= 3 and w not in ("THE", "LOS", "NEW", "SAN")}


def match_market(market: dict, game: dict):
    """Which side of `game` this Kalshi market pays on, or None.

    Same refuse-rather-than-guess rule as the pregame matcher: a label that
    could be either team, or neither, is skipped."""
    label = _words(market.get("yes_sub_title") or market.get("title"))
    if not label:
        return None
    home, away = _words(game.get("home_team")), _words(game.get("away_team"))
    hit_home, hit_away = label <= home, label <= away
    if hit_home == hit_away:            # both or neither: ambiguous
        return None
    return "home" if hit_home else "away"


def evaluate(market: dict, game: dict, p_home: float) -> dict:
    """Compare our win expectancy to this market's ask. None if it is no bet."""
    side = match_market(market, game)
    if side is None:
        return None
    p = p_home if side == "home" else 1.0 - p_home
    if not (LIVE_MIN_PROB <= p <= LIVE_MAX_PROB):
        return None
    ask = price_cents(market, "yes_ask")
    if not ask or not 0 < ask < 100:
        return None
    edge = 100.0 * p - ask - taker_fee_cents(ask)
    return dict(ticker=market.get("ticker"), side="yes", backing=side,
                price_cents=ask, model_prob=p, ev_cents=edge,
                game=game["short"], detail=game["detail"],
                inning=game["inning"], half=game["half"],
                score=f"{game['away_score']}-{game['home_score']}",
                tradeable=LIVE_MIN_EDGE_CENTS <= edge <= LIVE_MAX_EDGE_CENTS)


def scan(client=None, sims: int = None) -> list:
    """Every live game matched to its Kalshi market, with the edge on each.

    Returns ALL comparisons, not just the tradeable ones — the point of the
    first weeks is to learn the distribution of disagreement, and filtering to
    the tail before measuring it would hide exactly what we need to see.
    """
    client = client or KalshiClient(env=os.getenv("KALSHI_ENV", "prod"))
    games = live_state.live_games()
    if not games:
        log.info("No games in progress.")
        return []
    log.info("%d game(s) in progress", len(games))

    try:
        data = client._request("GET", "/events",
                               params={"series_ticker": SERIES, "status": "open",
                                       "with_nested_markets": "true",
                                       "limit": 60})
    except Exception as exc:
        log.error("Could not list %s events: %s", SERIES, exc)
        return []

    out = []
    for game in games:
        p_home = we.win_probability_for(game, sims=sims)
        for event in data.get("events") or []:
            for market in event.get("markets") or []:
                if market.get("status") not in (None, "active", "open"):
                    continue
                # A live game's market must still be THIS game. The pregame
                # model bought a championship future for want of this check.
                if not _same_game(market, game):
                    continue
                row = evaluate(market, game, p_home)
                if row:
                    row["p_home"] = round(p_home, 4)
                    out.append(row)
    return out


def _same_game(market: dict, game: dict) -> bool:
    """Reuse the pregame settlement guard, with the game's start inferred from
    the market itself — ESPN gives us no commence time for a game already in
    progress, and inventing one would defeat the check."""
    if match_market(market, game) is None:
        return False
    expiry = market_expiry(market)
    if expiry is None:
        return False
    # An in-progress game settles within hours; a future settles months out.
    from datetime import datetime, timezone
    hours = (expiry - datetime.now(timezone.utc)).total_seconds() / 3600.0
    return -2.0 <= hours <= 12.0


def main() -> int:
    setup_logging()
    rows = scan()
    if not rows:
        log.info("Nothing to compare.")
        return 0
    for r in sorted(rows, key=lambda x: -x["ev_cents"]):
        log.info("%-14s %-18s | %s %s | model %.1fc vs ask %.0fc -> %+.1fc %s",
                 r["game"], r["detail"], r["score"], r["backing"],
                 100 * r["model_prob"], r["price_cents"], r["ev_cents"],
                 "TRADEABLE" if r["tradeable"] else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
