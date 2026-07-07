"""Tests for the copy-only scoreboard."""

import csv

import copy_scoreboard
from ledger import EXEC_COLUMNS


def _write(path, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(EXEC_COLUMNS)
        w.writerows(rows)


def test_copy_scoreboard_totals(tmp_path):
    log = tmp_path / "copy_trades.csv"
    out = tmp_path / "COPY_SCOREBOARD.md"
    _write(log, [
        # a 4-lot winner, a 1-lot loser, and one still open
        ["2026-07-07T13:32:00Z", "smartmoney", "K-GAU", "yes", "4", "26",
         "1.04", "o1", "win (+74c)"],
        ["2026-07-07T13:20:00Z", "smartmoney", "K-PEG", "yes", "1", "78",
         "0.78", "o2", "loss (-78c)"],
        ["2026-07-07T17:06:00Z", "smartmoney", "K-AUG", "yes", "1", "44",
         "0.44", "o3", ""],
    ])
    copy_scoreboard.build(out=out, path=log)
    md = out.read_text()
    assert "3 copies · $2.26 deployed · 1 open" in md
    # 1 win / 1 loss on $1.82 spent, net +2.96 - 0.78 = +$2.18
    assert "1 W — 1 L" in md and "$1.82 spent" in md and "+$2.18" in md
    assert "🟢 win (+$2.96)" in md and "🔴 loss (-$0.78)" in md
    assert "⏳ open" in md


def test_copy_scoreboard_empty(tmp_path):
    out = tmp_path / "COPY_SCOREBOARD.md"
    copy_scoreboard.build(out=out, path=tmp_path / "missing.csv")
    assert "No copy trades recorded yet" in out.read_text()
