"""Shared paper-trade ledger: records every signal a model produces so the
scoreboard can score it against real settlement — independent of whether we
had capital to trade it. This is the measurement the executor was missing.

One row per market (deduped by ticker), so re-scanning the same edge every
hour does not inflate the record.
"""

import csv
import time
from datetime import datetime, timezone
from pathlib import Path

LEDGER_COLUMNS = ["scanned_at_utc", "event", "ticker", "side", "price_cents",
                  "model_prob", "ev_cents", "outcome"]
EXEC_COLUMNS = ["placed_at_utc", "model", "ticker", "side", "count",
                "price_cents", "cost_usd", "order_id", "outcome"]
EXEC_LOG = Path(__file__).resolve().parent / "executed_trades.csv"


def log_execution(model: str, ticker: str, side: str, count: int,
                  price_cents: int, order_id: str, path: Path = EXEC_LOG) -> None:
    """Append one row for every REAL order the moment it's placed — a
    guaranteed audit trail of actual money moved, independent of the
    signal ledger. The outcome column is filled by settlement scoring."""
    upgrade_exec_columns(path)
    new_file = not path.exists()
    with open(path, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(EXEC_COLUMNS)
        writer.writerow([
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model, ticker, side, count, price_cents,
            f"{price_cents * count / 100:.2f}", order_id, "",
        ])


def upgrade_exec_columns(path: Path = EXEC_LOG) -> None:
    """Migrate a pre-outcome executed ledger in place (adds the column)."""
    if not path.exists():
        return
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows or "outcome" in rows[0]:
        return
    rows[0].append("outcome")
    for r in rows[1:]:
        r.append("")
    with open(path, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)


def _fill_count(fill: dict) -> float:
    """Contracts in this fill. Live API uses count_fp ('3.00', a fixed-point
    STRING); keep plain count as a fallback for older payloads."""
    for key in ("count_fp", "count"):
        v = fill.get(key)
        if v not in (None, ""):
            return float(v)
    return 0.0


def _fill_price_cents(fill: dict) -> float:
    """Price paid per contract for the side actually bought, in cents.
    Live API gives yes/no_price_dollars as dollar STRINGS ('0.8200');
    cents-int yes_price/no_price kept as fallback."""
    side = (fill.get("side") or "").lower()
    dollars = fill.get("yes_price_dollars" if side == "yes"
                       else "no_price_dollars")
    if dollars not in (None, ""):
        return float(dollars) * 100.0
    if side == "yes":
        raw = fill.get("yes_price")
    else:
        raw = fill.get("no_price")
        if raw in (None, ""):
            yes = fill.get("yes_price")
            raw = None if yes in (None, "") else 100 - float(yes)
    return float(raw) if raw not in (None, "") else 0.0


def reconcile_fills(client, days: float = 7, path: Path = EXEC_LOG) -> int:
    """Recover REAL orders from Kalshi's own fills — the source of truth.
    CI workspaces are ephemeral and a run can die after ordering, so any
    recent BUY fill whose order_id is missing from the ledger is appended
    (partial fills aggregated per order, model='untracked'). Sells are
    exits, not bets — left to Kalshi's own history. Returns rows added."""
    upgrade_exec_columns(path)
    known = set()
    if path.exists():
        with open(path, newline="") as fh:
            rows = list(csv.reader(fh))
        if rows and "order_id" in rows[0]:
            i = rows[0].index("order_id")
            known = {r[i] for r in rows[1:] if len(r) > i and r[i]}

    fills = client.get_fills(int(time.time() - days * 86400))
    agg = {}
    for f in fills:
        if (f.get("action") or "").lower() != "buy":
            continue
        oid = str(f.get("order_id") or f.get("trade_id") or "")
        if not oid or oid in known:
            continue
        count = _fill_count(f)
        ts = f.get("created_time") or ""
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, timezone.utc).isoformat(
                timespec="seconds")
        a = agg.setdefault(oid, dict(
            ticker=f.get("ticker") or f.get("market_ticker") or "",
            side=(f.get("side") or "").lower(), count=0.0, cost_c=0.0,
            ts=str(ts)))
        a["count"] += count
        a["cost_c"] += count * _fill_price_cents(f)
        a["ts"] = min(a["ts"], str(ts)) if a["ts"] else str(ts)

    added = 0
    if agg:
        new_file = not path.exists()
        with open(path, "a", newline="") as fh:
            writer = csv.writer(fh)
            if new_file:
                writer.writerow(EXEC_COLUMNS)
            for oid, a in sorted(agg.items(), key=lambda kv: kv[1]["ts"]):
                if not a["count"]:
                    continue
                price = a["cost_c"] / a["count"]
                writer.writerow([a["ts"], "untracked", a["ticker"],
                                 a["side"], f"{a['count']:g}", f"{price:.0f}",
                                 f"{a['cost_c'] / 100:.2f}", oid, ""])
                added += 1
    return added


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
