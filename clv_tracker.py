"""Closing-Line Value (CLV) tracker for the sports bets — the honest proof of edge.

CLV is THE metric sharp bettors use to know they have an edge, and it shows up
long before realized P&L (which is buried in variance on small samples). For
every real sports fill we snapshot the market's price for the side we took while
the market is still OPEN; the last pre-settlement snapshot is the CLOSING line.

    CLV_cents = closing_price_of_our_side - entry_price

Positive CLV means the market moved TOWARD our side after we bet — we got a
better price than the closing line, the signature of a genuine edge. A positive
mean CLV over ~30+ bets is real evidence; realized W/L on <30 bets is noise.

Reads real fills from executed_trades.csv (model in sports/tennis), maintains
clv_sports.csv (one row per bet, keyed by order_id), writes CLV_SCOREBOARD.md.
Run it on the sports cadence so it captures a price close to each game's start.

    KALSHI_ENV=prod python clv_tracker.py
"""

import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from strategy_weather import price_cents
from trade_logger import get_logger, setup_logging

log = get_logger("clv_tracker")

ROOT = Path(__file__).resolve().parent
EXECUTED = ROOT / "executed_trades.csv"
CLV_CSV = ROOT / "clv_sports.csv"
SCOREBOARD = ROOT / "CLV_SCOREBOARD.md"
SPORTS_MODELS = {"sports", "tennis", "nrfi"}
OPEN_STATUS = (None, "", "active", "open", "initialized")
CLV_FIELDS = ["order_id", "ticker", "model", "side", "entry_price", "entry_ts",
              "last_price", "last_ts", "closing_price", "clv_cents",
              "status", "result"]


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def side_value_cents(market: dict, side: str):
    """Current cents value of the side we HOLD: yes -> the yes mid (or last),
    no -> 100 - that. None if the market has no usable quote. Uses price_cents
    so it reads BOTH the cents fields and Kalshi's newer *_dollars vintage —
    reading the raw yes_bid keys alone returned None and silently killed CLV."""
    yb, ya = price_cents(market, "yes_bid"), price_cents(market, "yes_ask")
    if yb and ya and 0 < yb and ya < 100:
        yes = (yb + ya) / 2.0
    else:
        last = price_cents(market, "last_price")
        if not last or not 0 < last < 100:
            return None
        yes = last
    return yes if side == "yes" else 100.0 - yes


def load_clv_rows() -> dict:
    if not CLV_CSV.exists():
        return {}
    with open(CLV_CSV, newline="") as fh:
        return {r["order_id"]: r for r in csv.DictReader(fh)
                if r.get("order_id")}


def load_sports_fills() -> list:
    if not EXECUTED.exists():
        return []
    out = []
    with open(EXECUTED, newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("model") in SPORTS_MODELS and r.get("order_id")
                    and r.get("ticker")):
                out.append(r)
    return out


def update(clv_rows: dict, fills: list, client) -> dict:
    """Add any new sports fills, then snapshot each still-open bet's price and
    freeze the closing line when its market settles. Pure w.r.t. the client so
    it's unit-testable. Returns the updated {order_id: row}."""
    now = datetime.now(timezone.utc).isoformat()
    for f in fills:
        oid = f["order_id"]
        if oid not in clv_rows:
            clv_rows[oid] = dict(
                order_id=oid, ticker=f["ticker"], model=f.get("model", ""),
                side=f.get("side", ""), entry_price=f.get("price_cents", ""),
                entry_ts=f.get("placed_at_utc", ""), last_price="", last_ts="",
                closing_price="", clv_cents="", status="open", result="")

    for row in clv_rows.values():
        if row.get("status") == "closed":
            continue
        try:
            market = client.get_market(row["ticker"])
        except Exception as exc:
            log.warning("no market for %s (%s) — will retry", row["ticker"], exc)
            continue
        status = market.get("status")
        val = side_value_cents(market, row["side"])
        if status in OPEN_STATUS:
            if val is not None:                       # snapshot latest open price
                row["last_price"], row["last_ts"] = f"{val:.1f}", now
        else:
            # market closed/settled: freeze the CLOSING line = last OPEN snapshot
            closing = _num(row.get("last_price"))
            entry = _num(row.get("entry_price"))
            if closing is not None and entry is not None:
                row["closing_price"] = f"{closing:.1f}"
                row["clv_cents"] = f"{closing - entry:.1f}"
            row["status"] = "closed"
            row["result"] = market.get("result") or row.get("result") or ""
    return clv_rows


def write_clv(clv_rows: dict) -> None:
    with open(CLV_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CLV_FIELDS)
        w.writeheader()
        for row in clv_rows.values():
            w.writerow({k: row.get(k, "") for k in CLV_FIELDS})


def scoreboard(clv_rows: dict) -> str:
    closed = [r for r in clv_rows.values()
              if r.get("status") == "closed" and _num(r.get("clv_cents")) is not None]
    n = len(closed)
    if n:
        clvs = [_num(r["clv_cents"]) for r in closed]
        mean = sum(clvs) / n
        pos = sum(1 for c in clvs if c > 0)
        beat = 100.0 * pos / n
    else:
        mean = beat = 0.0
    open_n = sum(1 for r in clv_rows.values() if r.get("status") != "closed")
    if n < 30:
        verdict = (f"⏳ Only {n} settled bets — need ~30+ for a real read "
                   f"(realized W/L on <30 is noise).")
    elif mean > 0:
        verdict = (f"✅ Mean CLV +{mean:.1f}c over {n} bets — evidence of a "
                   f"genuine edge. This is where real size is justified.")
    else:
        verdict = (f"❌ Mean CLV {mean:.1f}c over {n} bets — no edge vs the "
                   f"closing line. Do NOT scale real money here.")
    lines = [
        "# Sports CLV scoreboard",
        "",
        "Closing-Line Value = (market price of our side at close) − (price we "
        "paid). Positive = we beat the close = edge. The honest test.",
        "",
        f"- **Settled bets scored:** {n}   (open/pending: {open_n})",
        f"- **Mean CLV:** {mean:+.1f}c per bet",
        f"- **Beat the close:** {beat:.0f}% of bets",
        "",
        f"**Verdict:** {verdict}",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    setup_logging()
    client = KalshiClient(os.getenv("KALSHI_API_KEY_ID"),
                          os.getenv("KALSHI_PRIVATE_KEY_PATH"),
                          os.getenv("KALSHI_ENV", "prod"))
    rows = update(load_clv_rows(), load_sports_fills(), client)
    write_clv(rows)
    board = scoreboard(rows)
    SCOREBOARD.write_text(board)
    log.info("CLV updated: %d bets tracked. %s", len(rows),
             board.splitlines()[-1] if board else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
