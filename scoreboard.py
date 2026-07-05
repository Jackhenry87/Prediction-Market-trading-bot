"""Generates SCOREBOARD.md — a phone-friendly, color-coded view of the
paper-trade ledgers (green wins, red losses). GitHub renders it directly.

    python scoreboard.py     # rebuild by hand; auto_trade also rebuilds it
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "SCOREBOARD.md"
SOURCES = [
    ("🌡️ Weather model", ROOT / "paper_trades.csv"),
    ("₿ Crypto model", ROOT / "paper_trades_crypto.csv"),
    ("⚾ Sports model", ROOT / "paper_trades_sports.csv"),
    ("🛢️ Commodities model", ROOT / "paper_trades_commodities.csv"),
]
MAX_ROWS = 20


def _read(path: Path):
    if not path.exists():
        return None, []
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return None, []
    return rows[0], rows[1:]


def _pnl_cents(outcome: str) -> float:
    if "(" not in outcome:
        return 0.0
    return float(outcome.split("(")[1].rstrip("c)").replace("+", ""))


def build(out: Path = OUT) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# 📊 Trading Scoreboard",
        "",
        f"_Updated {now} — auto-generated every run; do not edit._",
        "",
        "Signals are scored against official settlement whether or not a "
        "real order was placed. P&L shown is per 1-contract stakes.",
        "",
    ]
    for name, path in SOURCES:
        header, body = _read(path)
        if not header:
            lines += [f"## {name}", "", "_No signals recorded yet._", ""]
            continue
        idx = {h: i for i, h in enumerate(header)}
        outcomes = [r[idx["outcome"]] for r in body]
        wins = sum(1 for o in outcomes if o.startswith("win"))
        losses = sum(1 for o in outcomes if o.startswith("loss"))
        pending = len(body) - wins - losses
        pnl = sum(_pnl_cents(o) for o in outcomes)

        lines += [
            f"## {name}",
            "",
            f"### 🟢 {wins} W — 🔴 {losses} L — ⏳ {pending} pending "
            f"— net **{pnl:+.0f}¢**",
            "",
            "| Scanned (UTC) | Market | Side | Price | Model | Result |",
            "|---|---|---|---|---|---|",
        ]
        for r in reversed(body[-MAX_ROWS:]):
            outcome = r[idx["outcome"]]
            if outcome.startswith("win"):
                result = f"🟢 **{outcome}**"
            elif outcome.startswith("loss"):
                result = f"🔴 {outcome}"
            else:
                result = "⏳ pending"
            when = r[idx["scanned_at_utc"]][5:16].replace("T", " ")
            prob = float(r[idx["model_prob"]]) * 100
            lines.append(
                f"| {when} | {r[idx['ticker']]} | {r[idx['side']].upper()} "
                f"| {float(r[idx['price_cents']]):.0f}¢ | {prob:.0f}% | {result} |"
            )
        if len(body) > MAX_ROWS:
            lines.append(f"| … | _{len(body) - MAX_ROWS} older rows in the "
                         f"CSV_ | | | | |")
        lines.append("")
    out.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    build()
    print(f"Wrote {OUT}")
