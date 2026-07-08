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
# Discovery: by default the scanner pages the WHOLE open-events feed and checks
# every mutually-exclusive ladder exchange-wide (~1000+: sports futures,
# elections, econ ranges, entertainment, weather...). One pass is ~30-40 API
# calls and needs no maintained list — new ladders appear automatically, settled
# ones drop off. Optionally restrict:
#   ARB_CATEGORIES=Elections,Economics   only these categories
#   ARB_SERIES=KXNEXTAG,KXHIGHNY         only these series (skips discovery)
ARB_CATEGORIES = [c.strip() for c in os.getenv("ARB_CATEGORIES", "").split(",")
                  if c.strip()]
ARB_SERIES = [s.strip() for s in os.getenv("ARB_SERIES", "").split(",")
              if s.strip()]
ARB_MAX_PAGES = int(os.getenv("ARB_MAX_PAGES", "60"))   # events-feed page cap
# guaranteed profit must clear this AFTER fees (a buffer for fee rounding and
# any slippage between scan and fill). Cents per 1-contract basket.
ARB_MIN_PROFIT_CENTS = float(os.getenv("ARB_MIN_PROFIT_CENTS", "2"))
# ...and must NOT exceed this. Kalshi's mutually_exclusive flag means AT MOST
# one YES, NOT exactly one: candidate/nominee fields ("next Pope", "51st
# state") carry an untradeable "none of the above" outcome, so their YES
# baskets sum far below $1 and look like a huge "arb" that actually LOSES when
# the none-outcome hits. A real basket on a truly exhaustive, liquid ladder
# sits a few cents under par at most — so an implausibly large profit is the
# signature of a non-exhaustive market and is rejected. (NO baskets are safe on
# non-exhaustive events, but their yes_bid sum stays far below 100 there, so
# they never trigger anyway.)
ARB_MAX_PROFIT_CENTS = float(os.getenv("ARB_MAX_PROFIT_CENTS", "7"))
# Depth-aware sizing caps. Go as big as the arb genuinely supports, but:
#   - never commit more than this % of balance to one basket, and
#   - always keep ARB_RESERVE_USD unlocked (so a thin arb can't strand you).
ARB_MAX_BALANCE_PCT = float(os.getenv("ARB_MAX_BALANCE_PCT", "100"))
ARB_RESERVE_USD = float(os.getenv("ARB_RESERVE_USD", "0"))


ARB_REQUIRE_EXHAUSTIVE = os.getenv(
    "ARB_REQUIRE_EXHAUSTIVE", "true").strip().lower() not in ("false", "0", "no")


def _exhaustive_numeric(markets: list) -> bool:
    """True only if the markets form a NUMERIC partition with both open tails —
    a 'less' bottom (≤X), a 'greater' top (≥Y), and 'between' buckets — which
    provably covers every outcome (collectively exhaustive). Any categorical
    leg (a candidate/entity name, no numeric strike) means an untradeable 'none
    of the above' outcome exists, so the basket is NOT risk-free -> reject.
    This is what separates a real temperature/econ-range arb from a bogus
    'next Pope' / 'election winner' one that loses when the field candidate wins."""
    low = high = 0
    for m in markets:
        st = m.get("strike_type")
        if st == "less":
            low += 1
        elif st == "greater":
            high += 1
        elif st == "between":
            if m.get("floor_strike") is None or m.get("cap_strike") is None:
                return False
        else:
            return False        # categorical / unknown strike -> not provable
    return low == 1 and high == 1


def evaluate_event(event: dict, markets: list) -> dict:
    """Return the best risk-free basket on a complete mutually-exclusive event,
    else None. Two directions, both guaranteed by exactly-one-YES:
      • YES basket — buy YES on every leg; the one winner pays $1. Profit when
        sum(yes_ask) < 100.
      • NO basket — buy NO on every leg; all but the winner pay $1 (N-1 total).
        Profit when sum(yes_bid) > 100 (the book is rich on the bid side).
    Fails closed on every ambiguity (not MECE, a closed leg, any unquoted leg)."""
    if not event.get("mutually_exclusive"):
        return None                      # can't prove at-most-one-YES -> skip
    if ARB_REQUIRE_EXHAUSTIVE and not _exhaustive_numeric(markets):
        return None                      # not provably collectively exhaustive
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
        if ARB_MIN_PROFIT_CENTS <= profit <= ARB_MAX_PROFIT_CENTS:
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
        if ARB_MIN_PROFIT_CENTS <= profit <= ARB_MAX_PROFIT_CENTS:
            candidates.append(dict(meta, side="no", payout_cents=payout,
                cost_cents=cost, fees_cents=fees, profit_cents=profit,
                legs=[(t, x, "no") for t, x in zip(tickers, no_asks)]))

    return max(candidates, key=lambda a: a["profit_cents"], default=None)


def _ladder_for_buy(orderbook: dict, want_side: str) -> list:
    """Ascending (buy_price_cents, qty) you can BUY `want_side` at, cheapest
    first. On Kalshi's single book, buying YES matches resting NO orders
    (yes_price = 100 - no_price) and buying NO matches resting YES orders."""
    opp = orderbook.get("no" if want_side == "yes" else "yes") or []
    out = []
    for level in opp:
        try:
            price, qty = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        buy = 100.0 - price
        if 0 < buy < 100 and qty > 0:
            out.append((buy, qty))
    out.sort(key=lambda x: x[0])
    return out


def _avg_fill(ladder: list, n: int):
    """Average cents to buy n contracts, consuming the ladder cheapest-first.
    None if the ladder can't supply n (insufficient depth)."""
    filled, cost = 0.0, 0.0
    for price, qty in ladder:
        take = min(qty, n - filled)
        cost += take * price
        filled += take
        if filled >= n:
            return cost / n
    return None


def basket_econ(ladders: list, side: str, n_legs: int, n: int):
    """(total_cost_cents, profit_per_contract_cents) to buy n on every leg at
    the achievable average fills. None if any leg lacks depth for n."""
    avgs = []
    for lad in ladders:
        a = _avg_fill(lad, n)
        if a is None:
            return None
        avgs.append(a)
    fees = sum(taker_fee_cents(a) for a in avgs)
    payout = 100.0 if side == "yes" else (n_legs - 1) * 100.0
    profit_per = payout - sum(avgs) - fees
    return n * sum(avgs), profit_per


def size_basket(client: KalshiClient, arb: dict, balance_cents: float,
                max_pct: float = None, reserve_usd: float = None,
                buffer_cents: float = None) -> dict:
    """Largest equal contract count across all legs whose AVERAGE fill still
    clears the profit buffer, capped by the balance-% and reserve guards. Reads
    each leg's live order book. Returns the arb enriched with count / real
    cost / locked profit, or None if size 0 (book too thin, or budget spent)."""
    max_pct = ARB_MAX_BALANCE_PCT if max_pct is None else max_pct
    reserve = (ARB_RESERVE_USD if reserve_usd is None else reserve_usd) * 100.0
    buf = ARB_MIN_PROFIT_CENTS if buffer_cents is None else buffer_cents
    budget = min(balance_cents * max_pct / 100.0, balance_cents - reserve)
    if budget <= 0:
        return None

    ladders = []
    for ticker, _, side in arb["legs"]:
        try:
            book = client.get_orderbook(ticker)
        except Exception as exc:
            log.warning("No book for %s (%s) — cannot size safely.", ticker, exc)
            return None
        lad = _ladder_for_buy(book, side)
        if not lad:
            return None
        ladders.append(lad)
    depth_cap = int(min(sum(q for _, q in lad) for lad in ladders))
    if depth_cap < 1:
        return None

    # profit_per is monotone-decreasing in n, cost monotone-increasing -> the
    # feasible set is [1, N*]; binary-search the largest feasible N.
    def ok(n):
        econ = basket_econ(ladders, arb["side"], arb["n"], n)
        if not econ:
            return False
        cost, profit_per = econ
        return profit_per >= buf and cost <= budget

    if not ok(1):
        return None
    lo, hi = 1, depth_cap
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if ok(mid):
            lo = mid
        else:
            hi = mid - 1
    cost, profit_per = basket_econ(ladders, arb["side"], arb["n"], lo)
    return dict(arb, count=lo, cost_cents=cost, fees_cents=None,
                profit_cents=profit_per, basket_profit_usd=profit_per * lo / 100.0)


def _scan_series(client: KalshiClient, series: list) -> list:
    """Only the given series (one /events call each) — used when ARB_SERIES is
    set explicitly, e.g. for testing or to pin the hunt to a few ladders."""
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
    return found


def _scan_discover(client: KalshiClient, categories: list) -> list:
    """Page the whole open-events feed and check EVERY mutually-exclusive ladder
    exchange-wide. Optional category allow-list. Order books for sizing are only
    fetched later for the few detected arbs, so a pass is just the paging cost."""
    catset = {c.lower() for c in categories} if categories else None
    found, cursor, seen = [], None, 0
    for _ in range(ARB_MAX_PAGES):
        params = {"status": "open", "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = client._request("GET", "/events", params=params)
        except Exception as exc:
            log.warning("Events page failed: %s", exc)
            break
        events = data.get("events", [])
        seen += len(events)
        for event in events:
            if catset and str(event.get("category", "")).lower() not in catset:
                continue
            arb = evaluate_event(event, event.get("markets") or [])
            if arb:
                found.append(arb)
        cursor = data.get("cursor")
        if not cursor or not events:
            break
    log.info("Scanned %d open events -> %d risk-free basket(s)", seen, len(found))
    return found


def scan(client: KalshiClient, series: list = None,
         categories: list = None) -> list:
    """All complete, below-$1 mutually-exclusive baskets. Discovers across the
    whole exchange by default; restricts to `series` (or ARB_SERIES) when given.
    Sorted best-profit first."""
    series = series if series is not None else ARB_SERIES
    if series:
        found = _scan_series(client, series)
    else:
        found = _scan_discover(
            client, categories if categories is not None else ARB_CATEGORIES)
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
