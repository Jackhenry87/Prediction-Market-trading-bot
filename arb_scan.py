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
One row per basket sighting per scan. Logging repeat sightings is deliberate:
the questions are how OFTEN a basket appears and how LONG it survives, and both
need the duplicates. A scan finding nothing still logs a heartbeat line so a
silent failure can't be mistaken for a quiet market.

HONEST LIMIT
------------
Profit here is computed from top-of-book quotes, so a sighting is an UPPER
BOUND on what is really takeable — it ignores depth. If sightings turn out to
be common, the next step is `kalshi_arb.size_basket()`, which walks the real
ladder. There is no point paying for that detail before we know sightings exist
at all.
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
           "profit_pct_of_cost"]


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
            ])
    return len(arbs)


def summarise(path: Path = None) -> str:
    """One line answering the question this scanner was built to answer."""
    path = path or SCAN_LOG
    if not path.exists():
        return "no sightings recorded yet"
    with open(path, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("profit_cents")]
    if not rows:
        return "no sightings recorded yet"
    profits = []
    for r in rows:
        try:
            profits.append(float(r["profit_cents"]))
        except ValueError:
            pass
    days = len({r["scanned_at_utc"][:10] for r in rows})
    events = len({r["event_ticker"] for r in rows})
    best = max(profits) if profits else 0.0
    return (f"{len(rows)} sightings across {events} distinct events over "
            f"{days} day(s); best basket +{best:.1f}c")


def main() -> int:
    setup_logging()
    # No credentials: market data is public and this process must not be able
    # to place an order even by accident. kalshi_arb.scan only reads.
    client = KalshiClient(env=os.getenv("KALSHI_ENV", "prod"))
    try:
        arbs = kalshi_arb.scan(client)
    except Exception as exc:
        log.error("Arb scan failed: %s", exc)
        return 1

    if not arbs:
        # Heartbeat: a quiet market and a broken scanner look identical in the
        # ledger otherwise.
        log.info("Arb scan complete: 0 risk-free baskets found.")
        return 0

    n = record(arbs)
    log.info("Arb scan complete: %d basket(s) found and recorded.", n)
    for a in arbs[:10]:
        log.info("  %s (%s) buy %s x%d legs | cost %.0fc + %.1fc fees -> "
                 "pays %.0fc | GUARANTEED +%.1fc",
                 (a.get("title") or "")[:52], a.get("event_ticker"),
                 str(a.get("side")).upper(), a.get("n", 0),
                 a.get("cost_cents", 0), a.get("fees_cents", 0),
                 a.get("payout_cents", 0), a.get("profit_cents", 0))
    log.info("Running total: %s", summarise())
    return 0


if __name__ == "__main__":
    sys.exit(main())
