"""Records live win-expectancy vs Kalshi price. Places NOTHING, ever.

    python live_scan.py

WHY THIS IS A RECORDER AND NOT A RUNNER
---------------------------------------
Same reason arb_scan.py is. The in-play market turned out to be the tightest on
the exchange — 1c spread on 2.4M contracts, against 7,290 on the same teams'
game tomorrow — and the first comparison we ever computed disagreed with it by
1.2c, which is less than the taker fee. That is one observation, and one
observation is not a distribution.

So this logs every comparison, tradeable or not, and answers with data:

  * How often does |model - market| exceed the fee at all?
  * When it does, is it on a real dislocation (a run just scored) or on stale
    state we misparsed?
  * Does our edge, when we claim one, predict the price MOVING our way?

The last question is the one that matters, and it is why every row carries the
market price at the time. A follow-up pass can score whether the price moved
toward the model — the in-play analogue of closing line value — without a
single dollar at risk.

FLIPPING IT LIVE
----------------
There is deliberately no order path in this file. When the recorded
distribution justifies trading, the runner is a separate change with its own
review, not a flag flipped here.
"""

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import strategy_live
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("live_scan")

SCAN_LOG = Path(__file__).resolve().parent / "live_scan.csv"

COLUMNS = ["scanned_at_utc", "game", "detail", "inning", "half", "score",
           "ticker", "backing", "model_prob", "p_home", "price_cents",
           "edge_cents", "tradeable"]


def record(rows: list, path: Path = None, now: str = None) -> int:
    """Append one row per comparison. Returns rows written."""
    path = path or SCAN_LOG
    now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not rows:
        return 0
    new_file = not path.exists()
    with open(path, "a", newline="") as fh:
        w = csv.writer(fh)
        if new_file:
            w.writerow(COLUMNS)
        for r in rows:
            w.writerow([
                now, r.get("game", ""), r.get("detail", ""),
                r.get("inning", ""), r.get("half", ""), r.get("score", ""),
                r.get("ticker", ""), r.get("backing", ""),
                f"{float(r.get('model_prob') or 0):.4f}",
                f"{float(r.get('p_home') or 0):.4f}",
                f"{float(r.get('price_cents') or 0):.0f}",
                f"{float(r.get('ev_cents') or 0):.1f}",
                "yes" if r.get("tradeable") else "no",
            ])
    return len(rows)


def summarise(path: Path = None) -> str:
    """The line that decides whether a live runner is worth building."""
    path = path or SCAN_LOG
    if not path.exists():
        return "no live comparisons recorded yet"
    edges, tradeable = [], 0
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                edges.append(float(r["edge_cents"]))
            except (KeyError, TypeError, ValueError):
                continue
            if r.get("tradeable") == "yes":
                tradeable += 1
    if not edges:
        return "no live comparisons recorded yet"
    n = len(edges)
    beat_fee = sum(1 for e in edges if e > 0)
    best = max(edges)
    mean = sum(edges) / n
    return (f"{n} comparisons; {beat_fee} ({100.0 * beat_fee / n:.1f}%) showed "
            f"positive edge after fees, {tradeable} cleared the "
            f"{strategy_live.LIVE_MIN_EDGE_CENTS:.0f}c trade gate; "
            f"mean {mean:+.2f}c, best {best:+.1f}c")


def main() -> int:
    setup_logging()
    # No credentials: this must not be able to place even by accident.
    client = KalshiClient(env=os.getenv("KALSHI_ENV", "prod"))
    try:
        rows = strategy_live.scan(client)
    except Exception as exc:
        log.error("Live scan failed: %s", exc)
        return 1
    if not rows:
        log.info("Live scan complete: nothing in progress to compare.")
        return 0
    n = record(rows)
    log.info("Live scan complete: %d comparison(s) recorded.", n)
    for r in sorted(rows, key=lambda x: -x["ev_cents"])[:10]:
        log.info("  %-14s %-16s %s | model %.1fc vs %.0fc -> %+.1fc%s",
                 r["game"], r["detail"], r["score"],
                 100 * r["model_prob"], r["price_cents"], r["ev_cents"],
                 "  TRADEABLE" if r["tradeable"] else "")
    log.info("Running total: %s", summarise())
    return 0


if __name__ == "__main__":
    sys.exit(main())
