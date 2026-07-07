"""Generates COPY_SCOREBOARD.md — a scoreboard for ONLY the smart-money
copy trades: what the copier placed, what it cost, and how each one
settled. Nothing else lives here; the copier is the sole writer.

    python copy_scoreboard.py     # rebuild by hand; the copier also rebuilds
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

from ledger import COPY_LOG

OUT = Path(__file__).resolve().parent / "COPY_SCOREBOARD.md"
MAX_ROWS = 40


def _rows(path: Path):
    if not path.exists():
        return None, []
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return (rows[0] if rows else None), []
    return rows[0], rows[1:]


def _realized_usd(r, idx):
    """Signed $ P&L of a settled copy (outcome '(+42c)' is per contract)."""
    try:
        cents = float(r[idx["outcome"]].split("(")[1].rstrip("c)"))
        return cents * float(r[idx["count"]]) / 100.0
    except (IndexError, ValueError, KeyError):
        return None


def build(out: Path = OUT, path: Path = COPY_LOG) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# 🦈 Copy-Trade Scoreboard",
        "",
        f"_Updated {now} — the smart-money copier's real orders only._",
        "",
    ]
    header, body = _rows(path)
    if not header or not body:
        lines += ["_No copy trades recorded yet._", ""]
        out.write_text("\n".join(lines) + "\n")
        return
    idx = {h: i for i, h in enumerate(header)}

    spent = sum(float(r[idx["cost_usd"]]) for r in body
                if len(r) > idx["cost_usd"] and r[idx["cost_usd"]])
    settled = [r for r in body if len(r) > idx["outcome"] and r[idx["outcome"]]]
    wins = sum(1 for r in settled if r[idx["outcome"]].startswith("win"))
    realized = sum(_realized_usd(r, idx) or 0.0 for r in settled)
    spent_settled = sum(float(r[idx["cost_usd"]]) for r in settled
                        if r[idx["cost_usd"]])

    sign = "+" if realized >= 0 else "-"
    lines += [
        f"**{len(body)} copies · ${spent:.2f} deployed · "
        f"{len(body) - len(settled)} open**",
        "",
        f"Settled: **{wins} W — {len(settled) - wins} L** on "
        f"${spent_settled:.2f} spent → "
        f"**{sign}${abs(realized):.2f}** {'🟢' if realized >= 0 else '🔴'}",
        "",
        "| Placed (UTC) | Market | Side | Qty | Price | Confidence "
        "| Cost | Result |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def confidence(r):
        prob = r[idx["model_prob"]] if "model_prob" in idx \
            and len(r) > idx["model_prob"] else ""
        if not prob:
            return "—"
        conf = f"{float(prob) * 100:.0f}%"
        w = r[idx["wallets"]] if "wallets" in idx and len(r) > idx["wallets"] \
            else ""
        return f"{conf} · {w} sharps" if w else conf

    for r in reversed(body[-MAX_ROWS:]):
        when = r[idx["placed_at_utc"]][5:16].replace("T", " ")
        out_txt = r[idx["outcome"]] if len(r) > idx["outcome"] else ""
        if not out_txt:
            res = "⏳ open"
        else:
            usd = _realized_usd(r, idx) or 0.0
            dot = "🟢" if out_txt.startswith("win") else "🔴"
            s = "+" if usd >= 0 else "-"
            res = f"{dot} {out_txt.split(' ')[0]} ({s}${abs(usd):.2f})"
        lines.append(
            f"| {when} | {r[idx['ticker']]} | {r[idx['side']].upper()} "
            f"| {r[idx['count']]} | {r[idx['price_cents']]}¢ "
            f"| {confidence(r)} | ${r[idx['cost_usd']]} | {res} |")
    lines.append("")
    out.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    build()
