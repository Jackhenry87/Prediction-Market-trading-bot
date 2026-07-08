"""Paper-trades spreadsheet — every paper (not-real-money) signal across all
models, consolidated into one sortable CSV. Kept SEPARATE from kalshi_report.csv
so the real-money book is never tangled with paper tracking.

Columns are uniform across models (weather/sports/props/…): model, when,
market, side, price, model prob, city, outcome. GitHub renders it as a table;
opens in Excel/Sheets.

    python paper_report.py
"""

import csv
import sys
from pathlib import Path

from kalshi_report import ticker_city

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "paper_report.csv"

# ledger file -> model label
SOURCES = {
    "paper_trades.csv": "weather",
    "paper_trades_crypto.csv": "crypto",
    "paper_trades_sports.csv": "sports",
    "paper_trades_commodities.csv": "commodities",
    "paper_trades_macro.csv": "macro",
    "paper_trades_smartmoney.csv": "smartmoney",
    "paper_trades_nowcast.csv": "nowcast",
    "paper_trades_props.csv": "props",
}
COLUMNS = ["model", "when_utc", "market", "side", "price", "model_prob",
           "city", "outcome"]


def _row(model: str, r: dict) -> dict:
    """Normalize one ledger row (schemas differ by model) into COLUMNS."""
    if model == "props":                       # DFS props schema
        market = f"{r.get('player','')} {r.get('stat','')} {r.get('line','')}".strip()
        return dict(model=model, when_utc=r.get("scanned_at_utc", ""),
                    market=market, side=r.get("side", ""),
                    price=r.get("dfs_decimal", ""),
                    model_prob=r.get("sharp_prob", ""), city="",
                    outcome=r.get("outcome", ""))
    ticker = r.get("ticker", "")
    return dict(model=model, when_utc=r.get("scanned_at_utc", ""),
                market=ticker, side=r.get("side", ""),
                price=r.get("price_cents", ""),
                model_prob=r.get("model_prob", ""),
                city=ticker_city(ticker), outcome=r.get("outcome", ""))


def build(out: Path = OUT) -> int:
    rows = []
    for fname, model in SOURCES.items():
        path = ROOT / fname
        if not path.exists():
            continue
        try:
            with open(path, newline="") as fh:
                for r in csv.DictReader(fh):
                    rows.append(_row(model, r))
        except OSError:
            continue
    rows.sort(key=lambda r: r["when_utc"])
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    return len(rows)


if __name__ == "__main__":
    n = build()
    print(f"Wrote {n} paper rows to {OUT}")
    sys.exit(0)
