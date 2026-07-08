"""Portfolio spreadsheet — the Kalshi Advanced Portfolio report as a base,
enriched with the columns you actually want to slice by (model, city,
open/settled status), covering OPEN positions AND every SETTLED market since a
start date. Authoritative: built from the account's own positions + settlements
via the signed REST API. GitHub renders the CSV as a sortable table; it opens
in Excel/Sheets.

Money columns are numeric (dollars / cents, no symbols) so you can sum, sort
and pivot — e.g. filter city='New York' to see that city's whole history.

    python kalshi_report.py             # since 2026-07-02 (default)
    python kalshi_report.py 2026-06-15  # custom start

READ-ONLY: pulls account data, places nothing.
"""

import csv
import re
import sys
from pathlib import Path

from backfill_history import settlement_pnl, _money, _count
from trade_logger import get_logger

log = get_logger("kalshi_report")

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "kalshi_report.csv"
DEFAULT_START = "2026-07-02"

COLUMNS = ["market", "model", "copyTrade", "city", "status", "side", "count",
           "avgPrice_cents", "exposure_usd", "lastPrice_cents",
           "unrealized_usd", "realized_usd", "total_usd",
           "categoryTotal_usd", "cityTotal_usd", "result", "closeDate",
           "ticker"]

CITY = {"NY": "New York", "CHI": "Chicago", "MIA": "Miami", "DEN": "Denver",
        "LAX": "Los Angeles", "AUS": "Austin", "PHIL": "Philadelphia",
        "HOU": "Houston", "PHX": "Phoenix"}


def _title(event: str, market_title: str, ticker: str) -> str:
    """Human-readable market name (never the raw ticker if we can help it)."""
    event, market_title = (event or "").strip(), (market_title or "").strip()
    if event and market_title:
        return f"{event} · {market_title}"
    return event or market_title or ticker


def _num(s) -> float:
    try:
        return float(str(s).replace("+", "").replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def ticker_city(ticker: str) -> str:
    m = re.match(r"KX(?:HIGH|LOW)([A-Z]+?)-", ticker or "")
    return CITY.get(m.group(1), "") if m else ""


def ticker_model(ticker: str, exec_map: dict = None) -> str:
    """Which strategy this market belongs to. Prefer the real order record;
    otherwise infer from the ticker family."""
    m = (exec_map or {}).get(ticker, "")
    if m and m != "untracked":          # trust a specific model label...
        return m
    if ticker.startswith(("KXHIGH", "KXLOW")):   # ...else infer from the family
        return "weather"
    if re.match(r"KX(MLB|NBA|NFL|NHL|WNBA)", ticker or ""):
        return "sports"
    if ticker.startswith(("KXATP", "KXWTA")):
        return "tennis"
    return "other"


def _load_copy_trades() -> dict:
    """ticker -> followed wallet(s), from the copy-trade ledger. Any ticker in
    here was a smart-money COPY (a wallet tail), which the row flags explicitly.
    Value is the wallet tag if recorded, else 'yes' so it's still marked."""
    path = ROOT / "copy_trades.csv"
    out = {}
    if not path.exists():
        return out
    try:
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                t = r.get("ticker")
                if t:
                    out[t] = (r.get("wallets") or "").strip() or "yes"
    except OSError:
        pass
    return out


def _load_exec_models() -> dict:
    """ticker -> model, from our own executed-orders ledger (most reliable)."""
    path = ROOT / "executed_trades.csv"
    out = {}
    if not path.exists():
        return out
    try:
        with open(path, newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("ticker") and r.get("model"):
                    out[r["ticker"]] = r["model"]
    except OSError:
        pass
    return out


def open_row(mp: dict, market: dict, exec_map: dict, copy_map: dict = None) -> dict:
    """One OPEN position, marked to the market's last price."""
    pos = float(mp.get("position", 0) or 0)
    side = "yes" if pos > 0 else "no"
    count = abs(pos)
    exposure = (_money(mp, "market_exposure") or 0.0)          # $ cost basis
    avg_c = (exposure / count * 100.0) if count else 0.0
    last_c = None
    if market:
        last_c = market.get("last_price")
        if last_c is None and market.get("yes_bid") is not None:
            last_c = market["yes_bid"]
    held_c = (last_c if side == "yes"
              else (100 - last_c if last_c is not None else None))
    value = (count * held_c / 100.0) if held_c is not None else exposure
    unreal = value - exposure
    ticker = mp.get("ticker", "")
    m = market or {}
    return {
        "market": _title(m.get("title"),
                         m.get("yes_sub_title") or m.get("subtitle"), ticker),
        "model": ticker_model(ticker, exec_map),
        "copyTrade": (copy_map or {}).get(ticker, ""),
        "city": ticker_city(ticker),
        "status": "open",
        "side": side, "count": f"{count:g}",
        "avgPrice_cents": f"{avg_c:.0f}",
        "exposure_usd": f"{exposure:.2f}",
        "lastPrice_cents": "" if held_c is None else f"{held_c:.0f}",
        "unrealized_usd": f"{unreal:+.2f}",
        "realized_usd": "0.00",
        "total_usd": f"{unreal:+.2f}",
        "categoryTotal_usd": "", "cityTotal_usd": "",
        "result": "",
        "closeDate": m.get("close_time", ""),
        "ticker": ticker,
    }


def settled_row(s: dict, market: dict, exec_map: dict, copy_map: dict = None) -> dict:
    """One SETTLED market, realized P&L from the settlement record."""
    ticker = s.get("ticker") or s.get("market_ticker") or ""
    result, pnl = settlement_pnl(s)
    pnl = pnl or 0.0
    yc = _count({"count": s.get("yes_count")}) if s.get("yes_count") else 0.0
    nc = _count({"count": s.get("no_count")}) if s.get("no_count") else 0.0
    side, count = ("yes", yc) if yc >= nc else ("no", nc)
    cost = ((_money(s, "yes_total_cost") or 0.0)
            + (_money(s, "no_total_cost") or 0.0))
    avg_c = (cost / count * 100.0) if count else 0.0
    m = market or {}
    return {
        "market": _title(m.get("title"),
                         m.get("yes_sub_title") or m.get("subtitle"), ticker),
        "model": ticker_model(ticker, exec_map),
        "copyTrade": (copy_map or {}).get(ticker, ""),
        "city": ticker_city(ticker),
        "status": "settled",
        "side": side, "count": f"{count:g}",
        "avgPrice_cents": f"{avg_c:.0f}",
        "exposure_usd": f"{cost:.2f}",
        "lastPrice_cents": "100" if (result == side) else "0",
        "unrealized_usd": "0.00",
        "realized_usd": f"{pnl:+.2f}",
        "total_usd": f"{pnl:+.2f}",
        "categoryTotal_usd": "", "cityTotal_usd": "",
        "result": result,
        "closeDate": m.get("close_time", ""),
        "ticker": ticker,
    }


def build_rows(positions: list, settlements: list, market_lookup,
               exec_map: dict, copy_map: dict = None) -> list:
    """All rows: open positions first, then settled (newest markets last).
    market_lookup(ticker) -> market dict (or None) for titles/prices."""
    rows = []
    open_tickers = set()
    for mp in positions:
        if float(mp.get("position", 0) or 0) == 0:
            continue
        t = mp.get("ticker", "")
        open_tickers.add(t)
        rows.append(open_row(mp, market_lookup(t), exec_map, copy_map))
    for s in settlements:
        t = s.get("ticker") or s.get("market_ticker") or ""
        if t in open_tickers:            # still holding some -> shown as open
            continue
        rows.append(settled_row(s, market_lookup(t), exec_map, copy_map))
    return finalize(rows)


def finalize(rows: list) -> list:
    """Fill the per-row categoryTotal_usd / cityTotal_usd columns and append
    summary rows: total P&L per category (model), per weather city, and a
    grand total across every tracked position. (This is the SUM of each
    position's P&L; the equity-based net is in the Account section.)"""
    from collections import defaultdict
    cat, city, grand = defaultdict(float), defaultdict(float), 0.0
    for r in rows:
        v = _num(r.get("total_usd"))
        grand += v
        if r.get("model"):
            cat[r["model"]] += v
        if r.get("city"):
            city[r["city"]] += v
    for r in rows:
        r["categoryTotal_usd"] = (f"{cat[r['model']]:+.2f}"
                                  if r.get("model") else "")
        r["cityTotal_usd"] = f"{city[r['city']]:+.2f}" if r.get("city") else ""

    def srow(market, **kw):
        base = {c: "" for c in COLUMNS}
        base.update(market=market, status="TOTAL", **kw)
        return base

    summary = [srow("═══ GRAND TOTAL (sum of positions) ═══",
                    total_usd=f"{grand:+.2f}")]
    for m, v in sorted(cat.items(), key=lambda x: -x[1]):
        summary.append(srow(f"TOTAL · category: {m}", model=m,
                            total_usd=f"{v:+.2f}", categoryTotal_usd=f"{v:+.2f}"))
    for c, v in sorted(city.items(), key=lambda x: -x[1]):
        summary.append(srow(f"TOTAL · weather city: {c}", city=c,
                            total_usd=f"{v:+.2f}", cityTotal_usd=f"{v:+.2f}"))
    return rows + summary


def write_csv(rows: list, out: Path = OUT) -> None:
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def build(start_date: str = DEFAULT_START, client=None) -> int:
    from datetime import datetime, timezone
    if client is None:
        from config import ConfigError, load_kalshi_settings
        from kalshi_client import KalshiClient
        try:
            settings = load_kalshi_settings(require_market=False)
        except ConfigError as exc:
            log.error("Configuration error: %s", exc)
            return 1
        client = KalshiClient(settings.kalshi_api_key_id,
                              settings.kalshi_private_key_path,
                              settings.kalshi_env)
    min_ts = int(datetime.strptime(start_date, "%Y-%m-%d")
                 .replace(tzinfo=timezone.utc).timestamp())
    positions = client.get_positions().get("market_positions", [])
    settlements = client.get_settlements(min_ts)
    exec_map = _load_exec_models()
    copy_map = _load_copy_trades()

    cache = {}

    def market_lookup(ticker):
        if ticker not in cache:
            try:
                cache[ticker] = client.get_market(ticker)
            except Exception:
                cache[ticker] = None
        return cache[ticker]

    rows = build_rows(positions, settlements, market_lookup, exec_map, copy_map)
    write_csv(rows)
    log.info("Wrote %d rows (%d open, %d settled) to %s",
             len(rows), sum(1 for r in rows if r["status"] == "open"),
             sum(1 for r in rows if r["status"] == "settled"), OUT.name)
    return 0


if __name__ == "__main__":
    from trade_logger import setup_logging
    setup_logging()
    sys.exit(build(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_START))
