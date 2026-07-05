"""Reconstruct the real Kalshi trade history since a start date, straight
from the account's own fills and settlements — the authoritative record.

Writes:
  - trade_history.csv : one row per fill, joined to its settlement result
  - HISTORY.md        : the same, human-readable, grouped by date

READ-ONLY: pulls account data, places nothing. Runs once and exits.

    python backfill_history.py            # since 2026-07-01 (default)
    python backfill_history.py 2026-06-15 # since a custom date
"""

import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("backfill_history")

ROOT = Path(__file__).resolve().parent
CSV_OUT = ROOT / "trade_history.csv"
MD_OUT = ROOT / "HISTORY.md"
DEFAULT_START = "2026-07-01"


def _money(d: dict, base: str):
    """Prefer the *_dollars variant; fall back to a cents field / 100."""
    if d.get(base + "_dollars") not in (None, ""):
        return float(d[base + "_dollars"])
    if d.get(base) not in (None, ""):
        return float(d[base]) / 100.0
    return None


def _count(fill: dict) -> float:
    for k in ("count", "count_fp"):
        if fill.get(k) not in (None, ""):
            return float(fill[k])
    return 0.0


def _fill_price_usd(fill: dict) -> float:
    """Price paid per contract for the side actually traded."""
    side = (fill.get("side") or "").lower()
    base = "no_price" if side == "no" else "yes_price"
    return _money(fill, base) or 0.0


def settlement_pnl(s: dict):
    """(result, net_pnl_usd) for a settled market."""
    result = s.get("market_result", "")
    revenue = _money(s, "revenue")
    cost = (_money(s, "yes_total_cost") or 0.0) + (_money(s, "no_total_cost") or 0.0)
    fee = _money(s, "fee_cost") or 0.0
    if revenue is None:
        val = _money(s, "value")
        return result, (val - cost - fee if val is not None else None)
    return result, revenue - cost - fee


def build(start_date: str) -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    min_ts = int(start_dt.timestamp())
    log.info("Pulling Kalshi history since %s (%s) ...", start_date, settings.kalshi_env)

    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path, settings.kalshi_env)
    try:
        fills = client.get_fills(min_ts)
        settlements = client.get_settlements(min_ts)
    except Exception as exc:
        log.error("Could not fetch history: %s", exc)
        return 1

    log.info("Fetched %d fills and %d settlements.", len(fills), len(settlements))

    # index settlement result/pnl by ticker
    settled = {}
    for s in settlements:
        result, pnl = settlement_pnl(s)
        settled[s.get("ticker")] = (result, pnl)

    rows = []
    for f in fills:
        ts = f.get("created_time") or f.get("ts") or ""
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        ticker = f.get("ticker") or f.get("market_ticker") or ""
        price = _fill_price_usd(f)
        count = _count(f)
        result, pnl = settled.get(ticker, ("", None))
        rows.append({
            "datetime_utc": str(ts),
            "date": str(ts)[:10],
            "ticker": ticker,
            "action": (f.get("action") or "").upper(),
            "side": (f.get("side") or "").upper(),
            "count": f"{count:g}",
            "price_cents": f"{price * 100:.0f}",
            "cost_usd": f"{price * count:.2f}",
            "fee_usd": f"{_money(f, 'fee_cost') or 0:.2f}",
            "settlement": result,
            "pnl_usd": "" if pnl is None else f"{pnl:.2f}",
        })
    rows.sort(key=lambda r: r["datetime_utc"])

    cols = ["datetime_utc", "date", "ticker", "action", "side", "count",
            "price_cents", "cost_usd", "fee_usd", "settlement", "pnl_usd"]
    with open(CSV_OUT, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    _write_markdown(rows, settlements)
    log.info("Wrote %d fills to %s and %s", len(rows), CSV_OUT.name, MD_OUT.name)
    return 0


def _write_markdown(rows: list, settlements: list) -> None:
    settled_pnl = 0.0
    for s in settlements:
        _, pnl = settlement_pnl(s)
        if pnl is not None:
            settled_pnl += pnl

    by_day = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)

    lines = [
        "# 📜 Kalshi Trade History",
        "",
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC from your "
        f"account's fills & settlements._",
        "",
        f"**{len(rows)} fills. Realized P&L on settled markets: "
        f"${settled_pnl:+.2f}.**",
        "",
    ]
    for day in sorted(by_day, reverse=True):
        day_rows = by_day[day]
        lines += [f"## {day}", "",
                  "| Time | Market | Action | Side | Qty | Price | Cost | Result | P&L |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in day_rows:
            res = r["settlement"] or "—"
            pnl = r["pnl_usd"]
            pnl_cell = "—" if pnl == "" else (f"🟢 +${pnl}" if float(pnl) >= 0
                                              else f"🔴 -${abs(float(pnl)):.2f}")
            lines.append(
                f"| {r['datetime_utc'][11:16]} | {r['ticker']} | {r['action']} "
                f"| {r['side']} | {r['count']} | {r['price_cents']}¢ "
                f"| ${r['cost_usd']} | {res} | {pnl_cell} |")
        lines.append("")
    MD_OUT.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START
    sys.exit(build(start))
