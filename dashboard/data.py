"""Data shaping for the dashboard: turn trade_history.csv rows and live
Kalshi fills into one uniform trade dict, and aggregate them into the
stats the page renders (P&L series, per-theme scoreboard, headline tiles).

Pure functions, no I/O beyond reading the CSV — keep it that way so the
web layer stays thin and this stays trivially testable.
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY_CSV = ROOT / "trade_history.csv"

# Ticker prefix -> theme. Superset of auto_trade._THEME_PREFIXES (the
# dashboard also labels macro prints and one-off event markets the bot's
# risk cap doesn't need to know about). tests/test_dashboard_data.py
# cross-checks the shared prefixes against auto_trade.theme_of so the
# two lists can't drift apart silently.
THEME_PREFIXES = [
    ("KXHIGH", "weather"), ("KXLOW", "weather"),
    ("KXBTC", "crypto"), ("KXETH", "crypto"),
    ("KXWTI", "commodities"), ("KXNGAS", "commodities"),
    ("KXGOLD", "commodities"),
    ("KXMLB", "sports"), ("KXNBA", "sports"), ("KXNFL", "sports"),
    ("KXNHL", "sports"), ("KXWNBA", "sports"),
    ("KXMENWORLDCUP", "sports"), ("KXWOMENWORLDCUP", "sports"),
    ("KXPAYROLLS", "macro"), ("KXCPI", "macro"), ("KXU3", "macro"),
    ("KXCLAIMS", "macro"), ("KXFED", "macro"),
]

THEME_ORDER = ["weather", "sports", "crypto", "macro", "commodities", "other"]


def theme_of(ticker: str) -> str:
    t = (ticker or "").upper()
    for prefix, theme in THEME_PREFIXES:
        if t.startswith(prefix):
            return theme
    return "other"


def _parse_ts(raw: str) -> float:
    """ISO-8601 (Z-suffixed) -> unix seconds; 0.0 if unparseable."""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def load_history(csv_path: Path = HISTORY_CSV) -> list:
    """trade_history.csv -> uniform trade dicts, oldest first.

    Each dict: ts (unix), datetime_utc, ticker, theme, action (BUY/SELL),
    side (YES/NO), count, price_cents, cost_usd, settlement ('yes'/'no'/''),
    pnl_usd (float or None while unsettled).
    """
    if not csv_path.is_file():
        return []
    trades = []
    with open(csv_path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                trades.append({
                    "ts": _parse_ts(row.get("datetime_utc", "")),
                    "datetime_utc": row.get("datetime_utc", ""),
                    "ticker": row.get("ticker", ""),
                    "theme": theme_of(row.get("ticker", "")),
                    "action": (row.get("action") or "").upper(),
                    "side": (row.get("side") or "").upper(),
                    "count": float(row.get("count") or 0),
                    "price_cents": float(row.get("price_cents") or 0),
                    "cost_usd": float(row.get("cost_usd") or 0),
                    "settlement": (row.get("settlement") or "").lower(),
                    "pnl_usd": (float(row["pnl_usd"])
                                if row.get("pnl_usd") not in (None, "")
                                else None),
                })
            except ValueError:
                continue  # one malformed row must not blank the page
    trades.sort(key=lambda t: t["ts"])
    return trades


def fill_to_trade(fill: dict) -> dict:
    """A live Kalshi fill -> the same uniform trade dict as load_history."""
    side = (fill.get("side") or "yes").lower()
    price_key = "no_price" if side == "no" else "yes_price"
    if fill.get(price_key + "_dollars") not in (None, ""):
        price_usd = float(fill[price_key + "_dollars"])
    elif fill.get(price_key) not in (None, ""):
        price_usd = float(fill[price_key]) / 100.0
    else:
        price_usd = 0.0
    count = 0.0
    for k in ("count", "count_fp"):
        if fill.get(k) not in (None, ""):
            count = float(fill[k])
            break
    created = fill.get("created_time", "")
    return {
        "ts": _parse_ts(created),
        "datetime_utc": created,
        "ticker": fill.get("ticker", ""),
        "theme": theme_of(fill.get("ticker", "")),
        "action": (fill.get("action") or "buy").upper(),
        "side": side.upper(),
        "count": count,
        "price_cents": round(price_usd * 100, 1),
        "cost_usd": round(price_usd * count, 2),
        "settlement": "",
        "pnl_usd": None,
    }


def market_outcomes(trades: list) -> list:
    """Collapse fills to one outcome per MARKET, oldest last-fill first.

    trade_history.csv repeats the market's settlement P&L on every fill
    row of that market, so P&L, wins and losses must be counted once per
    ticker — never per fill — or they multiply. (HISTORY.md's headline
    number is the per-market sum; this must always agree with it.)

    Each outcome: ticker, theme, last_ts, pnl_usd (None while unsettled).
    """
    markets = {}
    for t in trades:
        m = markets.setdefault(t["ticker"], {
            "ticker": t["ticker"], "theme": t["theme"],
            "last_ts": t["ts"], "pnl_usd": None,
        })
        m["last_ts"] = max(m["last_ts"], t["ts"])
        if t["pnl_usd"] is not None:
            m["pnl_usd"] = t["pnl_usd"]
    return sorted(markets.values(), key=lambda m: m["last_ts"])


def compute_stats(trades: list) -> dict:
    """Aggregate the uniform trade list into everything the page shows.
    Costs are per fill; P&L and W/L records are per market (see above)."""
    per_theme = {}
    for t in trades:
        th = per_theme.setdefault(t["theme"], {
            "theme": t["theme"], "trades": 0, "cost_usd": 0.0,
            "pnl_usd": 0.0, "wins": 0, "losses": 0, "pending": 0,
        })
        th["trades"] += 1
        th["cost_usd"] = round(th["cost_usd"] + t["cost_usd"], 2)

    wins = losses = pending = 0
    realized = cum = 0.0
    pnl_series = []          # [last_ts, cumulative realized pnl] per market
    for m in market_outcomes(trades):
        th = per_theme[m["theme"]]
        if m["pnl_usd"] is None:
            pending += 1
            th["pending"] += 1
            continue
        realized += m["pnl_usd"]
        cum = round(cum + m["pnl_usd"], 2)
        pnl_series.append([m["last_ts"], cum])
        th["pnl_usd"] = round(th["pnl_usd"] + m["pnl_usd"], 2)
        if m["pnl_usd"] >= 0:
            wins += 1
            th["wins"] += 1
        else:
            losses += 1
            th["losses"] += 1

    settled = wins + losses
    themes = sorted(
        per_theme.values(),
        key=lambda th: (THEME_ORDER.index(th["theme"])
                        if th["theme"] in THEME_ORDER else len(THEME_ORDER)),
    )
    return {
        "trades": len(trades),
        "deployed_usd": round(sum(t["cost_usd"] for t in trades), 2),
        "realized_pnl_usd": round(realized, 2),
        "wins": wins,
        "losses": losses,
        "pending": pending,
        "win_rate": round(100.0 * wins / settled, 1) if settled else None,
        "themes": themes,
        "pnl_series": pnl_series,
    }


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
