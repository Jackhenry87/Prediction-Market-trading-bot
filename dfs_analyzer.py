"""DFS pick'em (+EV) analyzer for PrizePicks / Underdog style apps.

These apps have NO betting API and their ToS forbid automation, so this
does NOT place anything and never touches them. It does the one thing that
is legal and actually valuable: the math. You paste the lines you see in
the app plus the sharp sportsbook line/odds (free to look up), and it tells
you which picks are +EV versus the sharp market and by how much.

Why this works: DFS pick'em lines are often softer (slower, less sharp)
than a real sportsbook's player props. When the sharp market implies a
higher true probability than the DFS payout requires to break even, the
pick is +EV. You still place the bets manually.

Input: dfs_picks.csv (see dfs_picks.example.csv). Output: DFS_ANALYSIS.md.

    python dfs_analyzer.py
"""

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

from trade_logger import get_logger, setup_logging

log = get_logger("dfs_analyzer")

ROOT = Path(__file__).resolve().parent
PICKS = ROOT / "dfs_picks.csv"
OUT = ROOT / "DFS_ANALYSIS.md"
# require a little cushion above breakeven so vig noise / stale lines don't
# masquerade as edge
MIN_EDGE = 0.02


def implied_prob(decimal_odds: float) -> float:
    return 1.0 / decimal_odds


def devig_two_way(over_odds: float, under_odds: float) -> float:
    """Fair P(over) from two-way decimal odds, vig removed."""
    po, pu = 1.0 / over_odds, 1.0 / under_odds
    return po / (po + pu)


def breakeven_prob(payout_mult: float, num_legs: int) -> float:
    """Per-leg win probability an entry needs just to break even, for a flat
    payout multiplier over N all-must-hit legs: (1/M)^(1/N)."""
    if payout_mult <= 1 or num_legs < 1:
        return 1.0
    return (1.0 / payout_mult) ** (1.0 / num_legs)


def analyze_pick(row: dict) -> dict:
    """Compute fair probability and edge for one DFS pick."""
    side = (row.get("dfs_side") or "").strip().lower()
    over_odds = _f(row.get("sharp_over_odds"))
    under_odds = _f(row.get("sharp_under_odds"))
    fair_prob = _f(row.get("fair_prob"))

    if fair_prob is None and over_odds and under_odds:
        p_over = devig_two_way(over_odds, under_odds)
        fair_prob = p_over if side == "over" else 1.0 - p_over

    result = dict(row)
    result["side"] = side
    if fair_prob is None:
        result["status"] = "no sharp reference"
        return result

    mult = _f(row.get("payout_mult")) or 0
    legs = int(_f(row.get("num_legs")) or 1)
    be = breakeven_prob(mult, legs)
    edge = fair_prob - be

    result.update(fair_prob=fair_prob, breakeven=be, edge=edge)
    # line discrepancy note (the most common real edge)
    dfs_line, sharp_line = _f(row.get("dfs_line")), _f(row.get("sharp_line"))
    if dfs_line is not None and sharp_line is not None and dfs_line != sharp_line:
        favorable = (side == "over" and dfs_line < sharp_line) or \
                    (side == "under" and dfs_line > sharp_line)
        result["line_note"] = (
            f"DFS line {dfs_line} vs sharp {sharp_line} — "
            + ("in your favor (edge is understated/conservative)"
               if favorable else "against you (edge overstated — be cautious)"))
    result["status"] = ("+EV" if edge >= MIN_EDGE
                        else "-EV" if edge < 0 else "marginal")
    return result


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(picks_path: Path = PICKS, out_path: Path = OUT) -> int:
    if not picks_path.exists():
        log.error("No %s found. Copy dfs_picks.example.csv to dfs_picks.csv "
                  "and fill in the lines you see in the app.", picks_path.name)
        return 1
    with open(picks_path, newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r.get("player")]

    analyzed = [analyze_pick(r) for r in rows]
    scored = [a for a in analyzed if "edge" in a]
    scored.sort(key=lambda a: -a["edge"])
    plus_ev = [a for a in scored if a["status"] == "+EV"]

    lines = [
        "# 🎯 DFS +EV Analysis",
        "",
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. "
        f"Manual play only — this tool places nothing._",
        "",
        f"**{len(plus_ev)} of {len(scored)} picks are +EV** (edge ≥ "
        f"{MIN_EDGE:.0%} over breakeven).",
        "",
        "| Player | Market | Pick | Fair % | Breakeven % | Edge | Verdict |",
        "|---|---|---|---|---|---|---|",
    ]
    for a in scored:
        verdict = {"+EV": "🟢 +EV", "-EV": "🔴 -EV"}.get(a["status"], "⚪ marginal")
        lines.append(
            f"| {a.get('player','')} | {a.get('market','')} "
            f"| {a['side'].upper()} {a.get('dfs_line','')} "
            f"| {a['fair_prob']*100:.0f}% | {a['breakeven']*100:.0f}% "
            f"| {a['edge']*100:+.1f}pts | {verdict} |")
    unscored = [a for a in analyzed if "edge" not in a]
    if unscored:
        lines += ["", "### ⚠️ Missing a sharp reference (add odds or fair_prob)",
                  ""]
        for a in unscored:
            lines.append(f"- {a.get('player','')} — {a.get('market','')}")
    notes = [a for a in scored if a.get("line_note")]
    if notes:
        lines += ["", "### Line-discrepancy notes", ""]
        for a in notes:
            lines.append(f"- **{a.get('player','')}**: {a['line_note']}")
    lines += ["", "---",
              "_Edge = fair win probability − breakeven probability. Only bet "
              "+EV picks, and remember DFS entries are parlays: even +EV legs "
              "lose often; size small and expect variance._", ""]
    out_path.write_text("\n".join(lines) + "\n")
    log.info("Analyzed %d picks (%d +EV). Wrote %s",
             len(scored), len(plus_ev), out_path.name)
    return 0


def main() -> int:
    setup_logging()
    return build()


if __name__ == "__main__":
    sys.exit(main())
