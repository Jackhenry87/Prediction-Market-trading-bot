"""Shared test setup.

Two jobs:

1. Put the repo root on sys.path so `import clv_tracker` works under a bare
   `pytest` as well as `python -m pytest` (which only works because module
   invocation happens to prepend the CWD). CI should not depend on that.

2. Stop tests writing to REAL bot state files. `test_ml_daily_budget_caps_scan`
   called the real `strategy_sports.scan()`, which saved to the repo's own
   sports_line_history.json and truncated a month of live line history down to
   its three-line fixture. The steam filter reads that file to decide every
   trade, so a test run silently blinded the model — and the workflow then
   force-added the damage and pushed it to main.

   Redirecting the paths for the whole session means a future test that
   forgets to patch cannot do it again.

   The same trap caught the follow-runner tests, which appended three fake
   `test-order` rows to the real copy_trades.csv and executed_trades.csv —
   the ledgers the scoreboards report from. Those two are redirected here as
   well, so the blast radius of a forgetful test is a temp directory.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_state_files(tmp_path, monkeypatch):
    """Point every module-level state path at a per-test temp file."""
    import strategy_sports
    import strategy_favorite
    import clv_tracker
    import ledger

    # The two shared EXECUTION ledgers. follow_runner passes these paths
    # explicitly at the call site, which is what lets a patch here take
    # effect — the functions' `path=` defaults are bound at import time and
    # cannot be redirected by patching the module constant alone.
    monkeypatch.setattr(ledger, "EXEC_LOG", tmp_path / "executed_trades.csv")
    monkeypatch.setattr(ledger, "COPY_LOG", tmp_path / "copy_trades.csv")
    try:
        import follow_runner
        monkeypatch.setattr(follow_runner, "EXEC_LOG",
                            tmp_path / "executed_trades.csv")
        monkeypatch.setattr(follow_runner, "COPY_LOG",
                            tmp_path / "copy_trades.csv")
        monkeypatch.setattr(follow_runner, "PAPER_LOG",
                            tmp_path / "follow_trades.csv")
        monkeypatch.setattr(follow_runner, "CURSOR_PATH",
                            tmp_path / "follow_cursor.txt")
        monkeypatch.setattr(follow_runner, "UNPARSED_LOG",
                            tmp_path / "follow_unparsed.log")
    except ImportError:
        pass

    monkeypatch.setattr(strategy_sports, "LINE_HISTORY",
                        tmp_path / "sports_line_history.json")
    monkeypatch.setattr(strategy_sports, "PAPER_LOG",
                        tmp_path / "paper_trades_sports.csv")
    monkeypatch.setattr(strategy_favorite, "PAPER_LEDGER",
                        tmp_path / "paper_trades_favorite.csv")
    monkeypatch.setattr(clv_tracker, "CLV_CSV", tmp_path / "clv_sports.csv")
    monkeypatch.setattr(clv_tracker, "SCOREBOARD", tmp_path / "CLV_SCOREBOARD.md")
    monkeypatch.setattr(clv_tracker, "PAPER_LEDGER",
                        tmp_path / "paper_trades_favorite.csv")
    yield
