"""Closing-Line Value (CLV) tracker — the honest proof of edge.

CLV is THE metric sharp bettors use to know they have an edge, and it shows up
long before realized P&L (which is buried in variance on small samples). For
every bet we snapshot the market's price for the side we took while the market
is still OPEN; the last pre-settlement snapshot is the CLOSING line.

    CLV_cents = closing_price_of_our_side - entry_price

Positive CLV means the market moved TOWARD our side after we bet — we got a
better price than the closing line, the signature of a genuine edge.

WHAT WENT WRONG BEFORE, AND WHAT THIS FIXES
-------------------------------------------
The first version of this tracker produced ZERO usable samples in a month. Two
causes, both now fixed:

  1. It only enrolled bets from executed_trades.csv — real money fills. With
     the book paper-only there is nothing to enroll, so it tracked nothing.
     It now enrols PAPER SIGNALS from the strategy ledger as well.

  2. When it first saw a market that had already closed, it set status=closed
     with last_price still blank, computed no CLV, and the scoreboard then
     filtered those rows out of the count. Four bets became "0 settled bets
     scored" with nothing anywhere saying they had been silently dropped.
     Rows that never got an open-market snapshot are now marked `unscored`
     and REPORTED, so a broken capture is visible instead of invisible.

FILL REALISM
------------
The favourite-bias model rests maker orders; a resting bid is not a fill. A
paper row starts `resting` and only becomes `filled` when the market's ask for
our side actually drops to our limit price — i.e. someone would have crossed
into us. Rows still resting when the market closes are `unfilled` and are NOT
scored. Counting unfilled paper orders as wins is the classic way a backtest
lies, so the accounting is explicit and reported.

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
PAPER_LEDGER = ROOT / "paper_trades_favorite.csv"
CLV_CSV = ROOT / "clv_sports.csv"
SCOREBOARD = ROOT / "CLV_SCOREBOARD.md"
SPORTS_MODELS = {"sports", "tennis", "nrfi"}
OPEN_STATUS = (None, "", "active", "open", "initialized")

# How many scored samples before the result means anything. The old value was
# 30; the owner's call is 100+ before any real money returns to the book.
SAMPLE_TARGET = int(os.getenv("CLV_SAMPLE_TARGET", "100"))

CLV_FIELDS = ["order_id", "ticker", "model", "side", "entry_price", "entry_ts",
              "last_price", "last_ts", "closing_price", "clv_cents",
              "status", "result", "fill_status", "fill_ts", "pnl_cents"]


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


def side_ask_cents(market: dict, side: str):
    """The best ASK for the side we want to buy, in that side's own prices.
    A resting bid at price P fills when this drops to <= P. Kalshi quotes one
    book in YES terms, so the NO ask is the mirror of the YES bid."""
    yb, ya = price_cents(market, "yes_bid"), price_cents(market, "yes_ask")
    if side == "yes":
        return ya if ya and 0 < ya < 100 else None
    return 100.0 - yb if yb and 0 < yb < 100 else None


def load_clv_rows() -> dict:
    if not CLV_CSV.exists():
        return {}
    with open(CLV_CSV, newline="") as fh:
        return {r["order_id"]: r for r in csv.DictReader(fh)
                if r.get("order_id")}


def load_sports_fills() -> list:
    """Real-money fills worth tracking, from the executed ledger."""
    if not EXECUTED.exists():
        return []
    out = []
    with open(EXECUTED, newline="") as fh:
        for r in csv.DictReader(fh):
            if (r.get("model") in SPORTS_MODELS and r.get("order_id")
                    and r.get("ticker")):
                out.append(r)
    return out


def load_paper_signals(path: Path = None) -> list:
    """Paper signals from the strategy ledger, shaped like fills so they can
    be enrolled by the same code path. The ledger has no order_id, so the
    (ticker, timestamp) pair is the key — stable across re-runs, which keeps
    re-scanning the same market from enrolling it twice."""
    path = path or PAPER_LEDGER
    if not path.exists():
        return []
    out = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            ticker, ts = r.get("ticker"), r.get("scanned_at_utc")
            if not ticker or not ts:
                continue
            out.append(dict(order_id=f"paper:{ticker}:{ts}", ticker=ticker,
                            model="favorite", side=r.get("side", ""),
                            price_cents=r.get("price_cents", ""),
                            placed_at_utc=ts, fill_status="resting"))
    return out


def _enroll(clv_rows: dict, bets: list) -> dict:
    """Add any bet we're not already tracking. Real fills are filled by
    definition; paper maker signals start resting until the book proves the
    order would have been hit."""
    for f in bets:
        oid = f["order_id"]
        if oid in clv_rows:
            continue
        clv_rows[oid] = dict(
            order_id=oid, ticker=f["ticker"], model=f.get("model", ""),
            side=f.get("side", ""), entry_price=f.get("price_cents", ""),
            entry_ts=f.get("placed_at_utc", ""), last_price="", last_ts="",
            closing_price="", clv_cents="", status="open", result="",
            fill_status=f.get("fill_status", "filled"), fill_ts="",
            pnl_cents="")
    return clv_rows


def settlement_pnl_cents(side: str, entry: float, result: str):
    """Per-contract P&L at settlement, in cents. A binary contract pays 100
    if your side is the result and 0 otherwise, so a fill at `entry` makes
    (100 - entry) on a win and loses `entry` on a loss.

    This needs NO order to have been placed — Kalshi publishes the result of
    every market, so a paper signal gets a real, auditable win/loss record.
    What it cannot tell you is whether a resting maker bid would actually
    have been hit; that is why only rows the book demonstrably reached
    (fill_status == 'filled') are counted here.
    """
    if result not in ("yes", "no") or entry is None:
        return None
    return (100.0 - entry) if result == side else -entry


def update(clv_rows: dict, fills: list, client, now: str = None) -> dict:
    """Enrol new bets, snapshot each still-open bet's price, resolve maker
    fills, and freeze the closing line when the market settles. Pure w.r.t.
    the client so it's unit-testable. Returns the updated {order_id: row}."""
    now = now or datetime.now(timezone.utc).isoformat()
    _enroll(clv_rows, fills)

    for row in clv_rows.values():
        if row.get("status") in ("closed", "unscored"):
            continue
        try:
            market = client.get_market(row["ticker"])
        except Exception as exc:
            log.warning("no market for %s (%s) — will retry", row["ticker"], exc)
            continue
        status = market.get("status")
        entry = _num(row.get("entry_price"))

        if status in OPEN_STATUS:
            # A resting maker bid becomes a fill the moment someone is willing
            # to sell to us at our price.
            if row.get("fill_status") == "resting" and entry is not None:
                ask = side_ask_cents(market, row["side"])
                if ask is not None and ask <= entry:
                    row["fill_status"], row["fill_ts"] = "filled", now
                    log.info("FILLED (paper): %s %s @ %.0fc",
                             row["side"], row["ticker"], entry)
            val = side_value_cents(market, row["side"])
            if val is not None:                       # snapshot latest open price
                row["last_price"], row["last_ts"] = f"{val:.1f}", now
            continue

        # --- market closed ---
        if row.get("fill_status") == "resting":
            # Never got hit. Not a bet, must not be scored either way.
            row["fill_status"] = "unfilled"
            row["status"] = "closed"
            row["result"] = market.get("result") or ""
            continue

        closing = _num(row.get("last_price"))
        if closing is None or entry is None:
            # We never saw this market while it was open, so there is no
            # closing line to compare against. Say so instead of emitting a
            # blank row that the scoreboard silently drops.
            row["status"] = "unscored"
            row["result"] = market.get("result") or row.get("result") or ""
            log.warning("UNSCORED %s: closed before any open-market snapshot",
                        row["ticker"])
            continue

        row["closing_price"] = f"{closing:.1f}"
        row["clv_cents"] = f"{closing - entry:.1f}"
        row["status"] = "closed"
        row["result"] = market.get("result") or row.get("result") or ""
        pnl = settlement_pnl_cents(row["side"], entry, row["result"])
        if pnl is not None:
            row["pnl_cents"] = f"{pnl:+.0f}"
    return clv_rows


def write_clv(clv_rows: dict) -> None:
    with open(CLV_CSV, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CLV_FIELDS)
        w.writeheader()
        for row in clv_rows.values():
            w.writerow({k: row.get(k, "") for k in CLV_FIELDS})


def _is_scored(r: dict) -> bool:
    return (r.get("status") == "closed"
            and r.get("fill_status") != "unfilled"
            and _num(r.get("clv_cents")) is not None)


def scoreboard(clv_rows: dict) -> str:
    rows = list(clv_rows.values())
    scored = [r for r in rows if _is_scored(r)]
    n = len(scored)
    if n:
        clvs = [_num(r["clv_cents"]) for r in scored]
        mean = sum(clvs) / n
        beat = 100.0 * sum(1 for c in clvs if c > 0) / n
    else:
        mean = beat = 0.0

    resting = sum(1 for r in rows if r.get("fill_status") == "resting"
                  and r.get("status") not in ("closed", "unscored"))
    open_n = sum(1 for r in rows
                 if r.get("status") not in ("closed", "unscored")) - resting
    unfilled = sum(1 for r in rows if r.get("fill_status") == "unfilled")
    unscored = sum(1 for r in rows if r.get("status") == "unscored")

    if n < SAMPLE_TARGET:
        verdict = (f"⏳ {n} of {SAMPLE_TARGET} scored samples — not enough to "
                   f"read yet. No real money until this reaches "
                   f"{SAMPLE_TARGET}+ and the mean is positive.")
    elif mean > 0:
        verdict = (f"✅ Mean CLV +{mean:.1f}c over {n} bets — evidence of a "
                   f"genuine edge. This is where real size is justified.")
    else:
        verdict = (f"❌ Mean CLV {mean:.1f}c over {n} bets — no edge vs the "
                   f"closing line. Do NOT put real money here.")

    # Settlement record. Kalshi publishes every market's result, so paper
    # signals get a real W/L and P&L without an order being placed. Only
    # rows the book actually reached are counted — an unfilled resting bid
    # is not a bet and must not appear as a win.
    settled = [r for r in rows if _num(r.get("pnl_cents")) is not None
               and r.get("fill_status") != "unfilled"]
    wins = sum(1 for r in settled if _num(r["pnl_cents"]) > 0)
    net = sum(_num(r["pnl_cents"]) for r in settled)
    staked = sum(_num(r.get("entry_price")) or 0 for r in settled)
    roi = (100.0 * net / staked) if staked else 0.0

    lines = [
        "# CLV scoreboard — favourite-bias maker model",
        "",
        "Closing-Line Value = (market price of our side at close) − (price we "
        "paid). Positive = we beat the close = edge. The honest test.",
        "",
        f"- **Scored samples:** {n} / {SAMPLE_TARGET}",
        f"- **Mean CLV:** {mean:+.1f}c per bet",
        f"- **Beat the close:** {beat:.0f}% of bets",
        "",
        "### Settlement record (scored against Kalshi's official results)",
        "",
        f"- **{wins}W – {len(settled) - wins}L** over {len(settled)} settled "
        f"contracts",
        f"- **Net {net:+.0f}c** on {staked:.0f}c staked (**{roi:+.1f}%**)",
        "",
        "_Real outcomes from public settlement data — no order required. What "
        "this does NOT prove is that a resting maker bid would have been "
        "filled at these prices; only live orders establish queue position._",
        "",
        "### Sample accounting",
        "",
        f"- resting (maker bid not yet hit): {resting}",
        f"- filled and still open: {open_n}",
        f"- expired unfilled (never hit — correctly NOT scored): {unfilled}",
        f"- unscored (closed before any open snapshot — capture gap): {unscored}",
        "",
        f"**Verdict:** {verdict}",
    ]
    if unscored:
        lines += ["", f"> ⚠️ {unscored} row(s) unscored. That is a measurement "
                      "failure, not a result — the tracker saw them only after "
                      "their market had closed."]
    return "\n".join(lines) + "\n"


def main() -> int:
    setup_logging()
    client = KalshiClient(os.getenv("KALSHI_API_KEY_ID"),
                          os.getenv("KALSHI_PRIVATE_KEY_PATH"),
                          os.getenv("KALSHI_ENV", "prod"))
    bets = load_sports_fills() + load_paper_signals()
    rows = update(load_clv_rows(), bets, client)
    write_clv(rows)
    board = scoreboard(rows)
    SCOREBOARD.write_text(board)
    log.info("CLV updated: %d bets tracked. %s", len(rows),
             board.splitlines()[-1] if board else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
