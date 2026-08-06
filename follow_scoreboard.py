"""The gate between paper and live money for the leaderboard copier.

    python follow_scoreboard.py        # writes FOLLOW_SCOREBOARD.md

Reads follow_trades.csv — every decision the copier made, placed or skipped —
and answers the three questions that decide whether FOLLOW_DRY_RUN should
ever be turned off:

  1. HOW OFTEN WOULD IT EVEN FIRE?  Roughly two thirds of his trades are
     soccer, golf, tennis, UFC, spreads and combos that no model here prices.
     A copier that fires four times a month is not a strategy, however good
     those four are. The skip-reason histogram makes that visible.

  2. IS IT PROFITABLE AFTER COSTS?  Settled copies only, net of the entry
     price actually paid. An unsettled position is not a result.

  3. ARE WE BUYING HIS IMPACT?  The gap between his entry and the price we
     could get is the tax he charges us for arriving late. If it eats the
     edge, the answer to the whole project is no — and that shows up here,
     not in a backtest.

Nothing in this file trades. It only measures.
"""

import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAPER_LOG = ROOT / "follow_trades.csv"
OUT = ROOT / "FOLLOW_SCOREBOARD.md"


def load_rows(path: Path = PAPER_LOG) -> list:
    try:
        with open(path, newline="") as fh:
            return list(csv.DictReader(fh))
    except FileNotFoundError:
        return []


def _f(row: dict, key: str):
    try:
        return float(row.get(key) or "")
    except ValueError:
        return None


def settled_pnl(rows: list) -> dict:
    """Realized cents on copies that actually settled.

    `outcome` is filled by the same settlement reconciliation the other
    ledgers use; rows without it are still open and are excluded rather than
    counted as flat, which would flatter a losing book.
    """
    wins = losses = 0
    net_cents = 0.0
    for r in rows:
        outcome = (r.get("outcome") or "").lower()
        contracts = _f(r, "contracts") or 0
        price = _f(r, "market_price_cents") or 0
        if not contracts or not price:
            continue
        if outcome.startswith("win"):
            wins += 1
            net_cents += contracts * (100 - price)
        elif outcome.startswith("loss"):
            losses += 1
            net_cents -= contracts * price
    return dict(wins=wins, losses=losses, net_usd=net_cents / 100.0)


def slippage_stats(rows: list) -> dict:
    """How far the line had moved past his entry by the time we saw it."""
    gaps = []
    for r in rows:
        his, ours = _f(r, "his_price_cents"), _f(r, "market_price_cents")
        if his and ours:
            gaps.append(ours - his)
    if not gaps:
        return dict(n=0, mean=0.0, worst=0.0)
    return dict(n=len(gaps), mean=sum(gaps) / len(gaps), worst=max(gaps))


def build(rows: list) -> str:
    decisions = Counter((r.get("decision") or "?") for r in rows)
    traded = [r for r in rows if r.get("decision") in ("placed", "paper")]
    pnl = settled_pnl(traded)
    slip = slippage_stats(rows)
    skips = Counter((r.get("reason") or "?")[:60]
                    for r in rows if r.get("decision") == "skip")
    models = Counter(r.get("model") or "-" for r in traded)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fired = len(traded)
    total = len(rows)
    rate = f"{fired / total:.0%}" if total else "—"

    lines = [
        "# 📋 Leaderboard-copier scoreboard", "",
        f"_Updated {now} — every notification the copier saw._", "",
        f"**{total} notification(s) · {fired} would-trade ({rate}) · "
        f"{decisions.get('placed', 0)} live · "
        f"{decisions.get('paper', 0)} paper**", "",
    ]

    settled = pnl["wins"] + pnl["losses"]
    if settled:
        emoji = "🟢" if pnl["net_usd"] >= 0 else "🔴"
        lines += [f"Settled: **{pnl['wins']}W — {pnl['losses']}L** → "
                  f"**${pnl['net_usd']:+.2f}** {emoji}", ""]
    else:
        lines += ["_No copies have settled yet — nothing to judge._", ""]

    if slip["n"]:
        lines += [
            "## Slippage vs his entry", "",
            f"On {slip['n']} signal(s) carrying his price, the market was "
            f"**{slip['mean']:+.1f}¢** away on average "
            f"(worst {slip['worst']:+.1f}¢).", "",
            "_Positive means the line had already moved against us — the "
            "cost of arriving after a six-figure order._", "",
        ]

    if models:
        lines += ["## Which model supplied `p`", ""]
        lines += [f"- `{m}` — {n}" for m, n in models.most_common()] + [""]

    if skips:
        lines += ["## Why signals were skipped", ""]
        lines += [f"- {r} — {n}" for r, n in skips.most_common(10)] + [""]

    lines += [
        "## Gate to live", "",
        "Flip `FOLLOW_DRY_RUN=false` only when this scoreboard shows a "
        "positive settled net across a real sample. His public record is "
        "+$448,676, but $586,308 of it is five trades — excluding those he "
        "is -$137,632, and 53% over 115 bets is 0.7σ from a coin flip. "
        "The number that matters is the one above, not his.", "",
    ]
    return "\n".join(lines)


def main() -> int:
    rows = load_rows()
    OUT.write_text(build(rows))
    print(f"Wrote {OUT.name} ({len(rows)} row(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
