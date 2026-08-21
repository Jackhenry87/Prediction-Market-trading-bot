"""Scan-only arbitrage recorder. Places NOTHING, ever.

    python arb_scan.py

WHY THIS EXISTS
---------------
A Polymarket trader (@swisstony) turned $23M in a year running one strategy:
buy every outcome of a mutually exclusive set when the combined price is under
$1, hold to resolution, repeat 17 times a minute. Zero directional risk — his
open book's unrealised P&L sits flat at -1% while he cycles $2.9M a day.

We cannot copy the returns: his edge is 3.5% *of turnover*, and turnover is the
product. At 17 fills/minute against our one run per 30 minutes, he has taken
every one of those baskets ~512 scans before we would look.

But the underlying mechanism is venue-agnostic, and `kalshi_arb.py` already
implements it for Kalshi. The open question is purely empirical: **do
risk-free baskets actually appear on Kalshi, at our cadence, often enough and
large enough to matter?** Guessing is free; measuring is nearly free. This
records every sighting so that in two weeks the answer is data instead of an
opinion.

WHAT IT RECORDS
---------------
One row per basket sighting per scan, on BOTH sides of the same ladders:

  side=yes / no    a TAKEABLE basket — buy every leg at the quote right now
  side=yes-maker   a RESTABLE basket — what the same $1 would cost if you
                   posted a tick inside each leg's bid and every one filled

Logging repeat sightings is deliberate: the questions are how OFTEN a basket
appears and how LONG it survives, and both need the duplicates. A scan finding
nothing still logs a heartbeat line so a silent failure can't be mistaken for a
quiet market.

WHAT THE FIRST FULL SWEEP FOUND (2026-08-21, 12,000 open events)
----------------------------------------------------------------
Takeable baskets are close to nonexistent: 2 of 3,609 fully-quoted
mutually-exclusive ladders priced under $1, both resolving in 2027, both worth
about +2.3c on a 93c basket. Every daily-resolving ladder sat 4-18c ABOVE par
at the ask, so taking one is a guaranteed LOSS. Restable baskets are common by
comparison — 38 of 110 well-formed exhaustive ladders, the best at 47c for a
guaranteed $1 — which is precisely what you would expect, because a resting
quote costs nothing and promises nothing.

HONEST LIMITS
-------------
Takeable profit is computed from top-of-book quotes, so a sighting is an UPPER
BOUND — it ignores depth. `kalshi_arb.size_basket()` walks the real ladder when
a sighting is worth pricing properly.

Restable profit is not a profit at all until a fill rate is known. Both numbers
are recorded so that when one is finally acted on, it is against evidence.
"""

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import kalshi_arb
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("arb_scan")

SCAN_LOG = Path(__file__).resolve().parent / "arb_scan.csv"

COLUMNS = ["scanned_at_utc", "event_ticker", "title", "side", "n_legs",
           "cost_cents", "fees_cents", "payout_cents", "profit_cents",
           "profit_pct_of_cost", "take_cost_cents", "dead_legs"]


def record(arbs: list, path: Path = None, now: str = None) -> int:
    """Append one row per basket found. Returns rows written."""
    path = path or SCAN_LOG
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not arbs:
        return 0
    new_file = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(COLUMNS)
        for a in arbs:
            cost = float(a.get("cost_cents") or 0)
            profit = float(a.get("profit_cents") or 0)
            w.writerow([
                now,
                a.get("event_ticker", ""),
                (a.get("title") or "")[:90],
                a.get("side", ""),
                a.get("n", ""),
                f"{cost:.1f}",
                f"{float(a.get('fees_cents') or 0):.1f}",
                f"{float(a.get('payout_cents') or 0):.1f}",
                f"{profit:.1f}",
                f"{100.0 * profit / cost:.2f}" if cost else "",
                a.get("take_cost_cents", ""),
                a.get("dead_legs", ""),
            ])
    return len(arbs)


def summarise(path: Path = None) -> str:
    """One line answering the question this scanner was built to answer.

    The two kinds are counted SEPARATELY and never pooled. A maker basket
    posting at 47c looks like a +53c sighting next to a +2.3c takeable one,
    and a combined "best basket +53c" would be the single most misleading
    number this file could print: the 2.3c is money you can have today and the
    53c is a quote nobody has filled.
    """
    path = path or SCAN_LOG
    if not path.exists():
        return "no sightings recorded yet"
    with open(path, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("profit_cents")]
    if not rows:
        return "no sightings recorded yet"
    days, parts = set(), []
    take, make = [], []
    for r in rows:
        days.add(r["scanned_at_utc"][:10])
        try:
            profit = float(r["profit_cents"])
        except (ValueError, KeyError):
            continue
        (make if str(r.get("side", "")).endswith("maker") else take).append(
            (profit, r.get("event_ticker", "")))
    for name, group in (("takeable", take), ("restable", make)):
        if not group:
            continue
        best = max(p for p, _ in group)
        parts.append(f"{len(group)} {name} sighting(s) across "
                     f"{len({e for _, e in group})} event(s), best +{best:.1f}c")
    if not parts:
        return "no sightings recorded yet"
    return f"over {len(days)} day(s): " + "; ".join(parts)


def main() -> int:
    setup_logging()
    # No credentials: market data is public and this process must not be able
    # to place an order even by accident. Neither scan can place.
    client = KalshiClient(env=os.getenv("KALSHI_ENV", "prod"))
    try:
        arbs, restable = kalshi_arb.scan_both(client)
    except Exception as exc:
        log.error("Arb scan failed: %s", exc)
        return 1

    n = record(arbs + restable)
    if not arbs:
        # Heartbeat: a quiet market and a broken scanner look identical in the
        # ledger otherwise.
        log.info("Arb scan complete: 0 TAKEABLE risk-free baskets found.")
    else:
        log.info("Arb scan complete: %d takeable basket(s).", len(arbs))
        for a in arbs[:10]:
            log.info("  TAKE %s (%s) buy %s x%d legs | cost %.0fc + %.1fc fees "
                     "-> pays %.0fc | GUARANTEED +%.1fc",
                     (a.get("title") or "")[:52], a.get("event_ticker"),
                     str(a.get("side")).upper(), a.get("n", 0),
                     a.get("cost_cents", 0), a.get("fees_cents", 0),
                     a.get("payout_cents", 0), a.get("profit_cents", 0))

    # The maker side of the same ladders. Recorded, never placed — a resting
    # quote is not a fill, and until we know the fill rate this is the size of
    # a prize, not an edge. See kalshi_arb.restable_basket.
    log.info("Restable (maker) baskets priced under $1: %d", len(restable))
    for a in restable[:5]:
        log.info("  REST %s (%s) x%d legs | post %.0fc for $1 (+%.0fc) | "
                 "taking the same basket costs %.0fc | %d leg(s) have no bid",
                 (a.get("title") or "")[:46], a.get("event_ticker"),
                 a.get("n", 0), a.get("cost_cents", 0), a.get("profit_cents", 0),
                 a.get("take_cost_cents", 0), a.get("dead_legs", 0))
    if n:
        log.info("Running total: %s", summarise())
    return 0


if __name__ == "__main__":
    sys.exit(main())
