"""Favourite-bias maker model — the one surviving strategy.

WHY THIS MODEL AND NOT THE OTHERS
---------------------------------
The weather/sports/crypto/commodities models each tried to out-forecast the
market with a home-grown probability estimate. Scored against real settlement
they produced Brier 0.308 and 0.278 (a coin flip is 0.25) and negative CLV.
They were paying the taker fee to cross the spread and buy a price they had no
demonstrated ability to beat.

This model makes the opposite bet, and a much better documented one. It does
not claim to forecast anything. It claims two structural facts about the
venue, both measured by other people on large samples:

  1. FAVOURITE-LONGSHOT BIAS. Low-priced contracts are systematically
     overpriced and high-priced contracts systematically underpriced. On
     Kalshi transaction data (300k+ contracts, Bürgi/Deng/Whelan) contracts
     under 10c lose over 60% of stake, while high-priced contracts win more
     often than break-even and return a small positive amount. This is the
     most replicated anomaly in prediction and betting markets.

  2. MAKER >> TAKER. The same study splits returns by execution style: on
     longshot contracts takers lose ~32% while makers lose ~10%. Kalshi
     charges makers NO fee at all; takers pay 0.07*p*(1-p) per contract.
     Resting an order rather than crossing is worth more than most models'
     entire claimed edge.

So: buy the FAVOURITE side, and only ever as a MAKER. Both legs of the trade
are structural, not predictive.

WHAT THIS MODEL ASSUMES (and what CLV is here to test)
------------------------------------------------------
FAV_BIAS_CENTS is the assumed size of the mispricing — how many cents a
favourite in our band is underpriced by. It is an ASSUMPTION UNDER TEST, not
a measurement. Everything else in the edge decomposition is arithmetic we
control:

    edge = FAV_BIAS_CENTS        <- the assumption being tested
         + (ask - entry)         <- spread we keep by resting instead of crossing
         + taker_fee_cents(ask)  <- fee we don't pay because we're the maker

If mean CLV over 100+ filled signals comes out positive, the assumption
survived and real money is justified. If it comes out negative or flat, the
bias is not harvestable at retail size on this venue and this model dies too.
That is the point of running it paper-only first.

DEPENDENCIES
------------
Public Kalshi market data ONLY. No paid odds API. The previous live model
depended on the-odds-api.com and silently returned 401 for weeks while every
workflow still reported success — this model has no such single point of
failure and no per-request quota to exhaust.

    python strategy_favorite.py        # read-only scan, places nothing
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from ledger import log_signals
from strategy_weather import price_cents, taker_fee_cents
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_favorite")

PAPER_LEDGER = Path(__file__).resolve().parent / "paper_trades_favorite.csv"

# --- the price band we consider a "favourite" -------------------------------
# Below FAV_MIN the documented underpricing has not kicked in; above FAV_MAX
# the absolute return per contract is too small to survive one bad fill, and
# the book is usually one-sided anyway.
FAV_MIN_PRICE = float(os.getenv("FAV_MIN_PRICE", "85"))
FAV_MAX_PRICE = float(os.getenv("FAV_MAX_PRICE", "97"))

# The assumption under test: how underpriced a favourite in the band is.
# Deliberately conservative — the literature's favourite-side excess return is
# small (low single-digit percent), and half of any larger number would be
# wishful thinking.
FAV_BIAS_CENTS = float(os.getenv("FAV_BIAS_CENTS", "1.0"))

# A maker order has to have somewhere to rest. We enter at best_bid + 1, which
# is only a maker price if it stays strictly below the ask — so we need at
# least 2c of spread.
MIN_SPREAD_CENTS = float(os.getenv("FAV_MIN_SPREAD", "2"))

# Skip markets about to close: CLV needs a closing line measured AFTER entry,
# and a market that closes in minutes gives the price no room to move.
MIN_HOURS_TO_CLOSE = float(os.getenv("FAV_MIN_HOURS_TO_CLOSE", "2"))

# Thin books produce fills that would never have happened at size. Require a
# market to have actually traded.
MIN_VOLUME = float(os.getenv("FAV_MIN_VOLUME", "50"))

# How many signals one scan may emit. Keeps the paper ledger from filling with
# 400 near-identical rows from one event on day one.
MAX_SIGNALS = int(os.getenv("FAV_MAX_SIGNALS", "25"))

# Correlated-market guard: at most this many signals sharing an event prefix.
MAX_PER_EVENT = int(os.getenv("FAV_MAX_PER_EVENT", "1"))


def list_open_markets(client, page_cap: int = 20) -> list:
    """Every open market, paged. Uses the public /markets endpoint — no
    credentials needed, so this works even with a broken/absent API key."""
    out, cursor = [], None
    for _ in range(page_cap):
        params = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = client._request("GET", "/markets", params=params)
        page = data.get("markets") or []
        out += page
        cursor = data.get("cursor")
        if not cursor or not page:
            break
    return out


def _hours_to_close(market: dict, now: datetime = None) -> float:
    """Hours until the market stops trading. None if unparseable — callers
    treat that as 'unknown', which is a skip (fail closed)."""
    raw = market.get("close_time") or market.get("expiration_time")
    if not raw:
        return None
    try:
        close = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    return (close - now).total_seconds() / 3600.0


def favourite_side(market: dict):
    """Which side is the favourite, and that side's book.

    Kalshi quotes one book in YES terms. The NO book is its mirror:
    no_bid = 100 - yes_ask, no_ask = 100 - yes_bid. Returns
    (side, bid, ask) in the FAVOURITE's own prices, or None if the market
    has no usable two-sided quote.
    """
    yes_bid = price_cents(market, "yes_bid")
    yes_ask = price_cents(market, "yes_ask")
    if not yes_bid or not yes_ask:
        return None
    if not (0 < yes_bid < yes_ask < 100):
        return None
    yes_mid = (yes_bid + yes_ask) / 2.0
    if yes_mid >= 50.0:
        return ("yes", yes_bid, yes_ask)
    return ("no", 100.0 - yes_ask, 100.0 - yes_bid)


def evaluate_market(market: dict, now: datetime = None):
    """A maker signal for this market, or None. Pure — no I/O — so the
    filters are unit-testable without a client."""
    book = favourite_side(market)
    if not book:
        return None
    side, bid, ask = book

    spread = ask - bid
    if spread < MIN_SPREAD_CENTS:
        return None

    # Rest one cent better than the current best bid. Strictly inside the
    # spread, so it is always a maker order and never crosses.
    entry = bid + 1.0
    if entry >= ask:
        return None
    if not FAV_MIN_PRICE <= entry <= FAV_MAX_PRICE:
        return None

    if float(market.get("volume") or 0) < MIN_VOLUME:
        return None

    hours = _hours_to_close(market, now)
    if hours is None or hours < MIN_HOURS_TO_CLOSE:
        return None

    # Edge decomposition — see the module docstring. Only the first term is an
    # assumption; the other two are mechanical consequences of resting.
    spread_saved = ask - entry
    fee_avoided = taker_fee_cents(ask)
    ev = FAV_BIAS_CENTS + spread_saved + fee_avoided

    # model_prob is the price we believe is fair, i.e. our entry plus the
    # assumed bias. Recording it makes the assumption falsifiable: the
    # scoreboard scores this number against settlement like any other model.
    model_prob = min((entry + FAV_BIAS_CENTS) / 100.0, 0.99)

    return dict(ticker=market.get("ticker", ""), side=side,
                price_cents=entry, model_prob=model_prob, ev_cents=ev,
                bid=bid, ask=ask, spread=spread, hours_to_close=hours,
                subtitle=(market.get("title") or market.get("subtitle") or ""))


def _event_of(ticker: str) -> str:
    """KXHIGHNY-26JUL02-B99.5 -> KXHIGHNY-26JUL02. Markets in one event are
    the same underlying bet; we take at most MAX_PER_EVENT of them."""
    if ticker and ticker.count("-") >= 2:
        return ticker.rsplit("-", 1)[0]
    return ticker or ""


def scan(client=None, now: datetime = None) -> list:
    """Scan every open market for favourite-side maker entries.

    Returns the ledger's results shape: [{date, signals: [...]}] so the
    existing ledger.log_signals / scoreboard machinery works unchanged.
    """
    client = client or KalshiClient(env=os.getenv("KALSHI_ENV", "prod"))
    markets = list_open_markets(client)
    log.info("Scanned %d open markets", len(markets))

    signals = []
    for m in markets:
        try:
            sig = evaluate_market(m, now)
        except Exception as exc:                    # one bad market must not
            log.debug("skip %s: %s", m.get("ticker"), exc)   # kill the scan
            continue
        if sig:
            signals.append(sig)

    # Best edge first, then thin out correlated markets from the same event.
    signals.sort(key=lambda s: -s["ev_cents"])
    per_event, kept = {}, []
    for s in signals:
        ev_key = _event_of(s["ticker"])
        if per_event.get(ev_key, 0) >= MAX_PER_EVENT:
            continue
        per_event[ev_key] = per_event.get(ev_key, 0) + 1
        kept.append(s)
        if len(kept) >= MAX_SIGNALS:
            break

    log.info("%d favourite-side maker signals (%d before event/limit cull)",
             len(kept), len(signals))
    for s in kept:
        log.info("  %s %s @ %.0fc (book %.0f/%.0f, %.1fc edge, closes in "
                 "%.1fh) | %s", s["side"].upper(), s["ticker"],
                 s["price_cents"], s["bid"], s["ask"], s["ev_cents"],
                 s["hours_to_close"], s["subtitle"][:60])

    if not kept:
        return []
    date = datetime.now(timezone.utc).date().isoformat()
    return [dict(date=date, model="favorite", signals=kept)]


def main() -> int:
    """Scan and record. This model is PAPER-ONLY BY CONSTRUCTION: it never
    imports an order-placing path, so there is no code here that can send an
    order even if DRY_RUN were misconfigured. Promotion to real money is a
    deliberate future change gated on the CLV scoreboard, not a flag flip."""
    setup_logging()
    try:
        results = scan()
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        return 1
    if not results:
        log.info("No qualifying favourite-side maker entries this scan.")
        return 0
    written = log_signals(results, PAPER_LEDGER)
    log.info("Recorded %d new paper signal(s) to %s (dedup by ticker)",
             written, PAPER_LEDGER.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
