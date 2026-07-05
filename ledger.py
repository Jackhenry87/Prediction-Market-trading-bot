"""Shared paper-trade ledger: records every signal a model produces so the
scoreboard can score it against real settlement — independent of whether we
had capital to trade it. This is the measurement the executor was missing.

One row per market (deduped by ticker), so re-scanning the same edge every
hour does not inflate the record.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

LEDGER_COLUMNS = ["scanned_at_utc", "event", "ticker", "side", "price_cents",
                  "model_prob", "ev_cents", "outcome"]


def apply_price_band(results: list, min_cents: float, max_cents: float) -> list:
    """Drop signals whose buy price is outside [min, max] cents. Avoids
    near-locks (bad risk/reward) and longshots (lottery tickets), and the
    price zone where our models are least calibrated."""
    out = []
    for r in results:
        kept = [s for s in r["signals"]
                if min_cents <= s["price_cents"] <= max_cents]
        if kept:
            out.append(dict(r, signals=kept))
    return out


def _existing_tickers(path: Path) -> set:
    if not path.exists():
        return set()
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return set()
    idx = rows[0].index("ticker") if "ticker" in rows[0] else 2
    return {r[idx] for r in rows[1:] if len(r) > idx}


def log_signals(results: list, path: Path) -> int:
    """Append new signals (unseen tickers) to the model's ledger.
    Returns how many rows were written."""
    seen = _existing_tickers(path)
    new_file = not path.exists()
    written = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(LEDGER_COLUMNS)
        for r in results:
            for s in r["signals"]:
                if s["ticker"] in seen:
                    continue
                seen.add(s["ticker"])
                writer.writerow([now, r.get("date", ""), s["ticker"],
                                 s["side"], f"{s['price_cents']:.0f}",
                                 f"{s['model_prob']:.3f}",
                                 f"{s['ev_cents']:.1f}", ""])
                written += 1
    return written
