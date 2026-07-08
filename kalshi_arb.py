"""Within-Kalshi structural arbitrage — guaranteed money, one exchange.

Kalshi lists many markets as MUTUALLY-EXCLUSIVE ladders: a temperature event's
buckets, an election's candidate field — exactly ONE market in the event
resolves YES. If you can buy one contract of EVERY leg for less than $1 total
(after fees), that $1 payout is locked no matter which leg wins: risk-free.

This scans configured series for such baskets. It FAILS CLOSED — a basket is
only an arb (and only ever auto-placed) when:
  1. Kalshi marks the event mutually_exclusive (exactly one YES by
     construction — the completeness guarantee), AND
  2. every leg has a live yes_ask (no missing/again unquoted leg — a gap means
     the basket isn't complete and the "arb" could lose), AND
  3. the guaranteed profit clears ARB_MIN_PROFIT_CENTS after per-leg fees.
Anything it can't prove is reported detect-only, never traded.

Residual risk (disclosed): Kalshi has no atomic multi-leg order, so auto-place
legs the basket one order at a time. If a leg fails to fill you're left with a
partial, non-risk-free position — the runner logs any failure loudly.

    python kalshi_arb.py            # read-only scan, prints baskets
    ARB_SERIES=KXHIGHNY,KXHIGHCHI python kalshi_arb.py
"""

import os
import sys
from pathlib import Path

from kalshi_client import KalshiClient
from strategy_weather import price_cents, taker_fee_cents
from trade_logger import get_logger, setup_logging

log = get_logger("kalshi_arb")

ROOT = Path(__file__).resolve().parent
# Series to scan. Weather temperature ladders are textbook MECE baskets; add
# politics/macro series tickers here once confirmed from a live run's log.
ARB_SERIES = [s.strip() for s in os.getenv(
    "ARB_SERIES",
    "KXHIGHNY,KXHIGHCHI,KXHIGHMIA,KXHIGHDEN,KXHIGHLAX,KXHIGHAUS").split(",")
    if s.strip()]
# guaranteed profit must clear this AFTER fees (a buffer for fee rounding and
# any slippage between scan and fill). Cents per 1-contract basket.
ARB_MIN_PROFIT_CENTS = float(os.getenv("ARB_MIN_PROFIT_CENTS", "2"))


def evaluate_event(event: dict, markets: list) -> dict:
    """Return the best risk-free basket on a complete mutually-exclusive event,
    else None. Two directions, both guaranteed by exactly-one-YES:
      • YES basket — buy YES on every leg; the one winner pays $1. Profit when
        sum(yes_ask) < 100.
      • NO basket — buy NO on every leg; all but the winner pay $1 (N-1 total).
        Profit when sum(yes_bid) > 100 (the book is rich on the bid side).
    Fails closed on every ambiguity (not MECE, a closed leg, any unquoted leg)."""
    if not event.get("mutually_exclusive"):
        return None                      # can't prove exactly-one-YES -> skip
    tickers, yes_asks, yes_bids = [], [], []
    for m in markets:
        if m.get("status") not in (None, "active", "open"):
            return None                  # a closed/settled leg -> basket broken
        tickers.append(m.get("ticker"))
        yes_asks.append(price_cents(m, "yes_ask"))
        yes_bids.append(price_cents(m, "yes_bid"))
    n = len(tickers)
    if n < 2:
        return None
    meta = dict(event_ticker=event.get("event_ticker") or event.get("ticker"),
                title=event.get("title", ""), n=n)
    candidates = []

    # YES basket: needs every yes_ask quoted; buy YES at the ask (marketable).
    if all(a and 0 < a < 100 for a in yes_asks):
        cost = sum(yes_asks)
        fees = sum(taker_fee_cents(a) for a in yes_asks)
        profit = 100.0 - cost - fees     # exactly one leg pays $1
        if profit >= ARB_MIN_PROFIT_CENTS:
            candidates.append(dict(meta, side="yes", payout_cents=100.0,
                cost_cents=cost, fees_cents=fees, profit_cents=profit,
                legs=[(t, a, "yes") for t, a in zip(tickers, yes_asks)]))

    # NO basket: needs every yes_bid quoted; buy NO at no_ask = 100 - yes_bid.
    if all(b and 0 < b < 100 for b in yes_bids):
        no_asks = [100.0 - b for b in yes_bids]
        cost = sum(no_asks)
        fees = sum(taker_fee_cents(x) for x in no_asks)
        payout = (n - 1) * 100.0         # all but the single winner pay $1
        profit = payout - cost - fees    # == sum(yes_bid) - 100 - fees
        if profit >= ARB_MIN_PROFIT_CENTS:
            candidates.append(dict(meta, side="no", payout_cents=payout,
                cost_cents=cost, fees_cents=fees, profit_cents=profit,
                legs=[(t, x, "no") for t, x in zip(tickers, no_asks)]))

    return max(candidates, key=lambda a: a["profit_cents"], default=None)


def scan(client: KalshiClient, series: list = None) -> list:
    """Every complete, below-$1 mutually-exclusive basket across the series."""
    series = series or ARB_SERIES
    found = []
    for s in series:
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": s, "status": "open",
                        "with_nested_markets": "true", "limit": 100})
        except Exception as exc:
            log.warning("Skipping %s: %s", s, exc)
            continue
        for event in data.get("events", []):
            arb = evaluate_event(event, event.get("markets") or [])
            if arb:
                found.append(arb)
    found.sort(key=lambda a: -a["profit_cents"])
    return found


def main() -> int:
    setup_logging()
    client = KalshiClient(env=os.getenv("KALSHI_ENV", "prod"))
    arbs = scan(client)
    for a in arbs:
        log.info("ARB: %s (%s) — buy %s on all %d legs, cost %.0fc + %.1fc "
                 "fees, pays %.0fc -> GUARANTEED +%.1fc / basket",
                 a["title"], a["event_ticker"], a["side"].upper(), a["n"],
                 a["cost_cents"], a["fees_cents"], a["payout_cents"],
                 a["profit_cents"])
    log.info("%s risk-free basket(s) found. NO ORDERS placed by this script.",
             len(arbs) or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
