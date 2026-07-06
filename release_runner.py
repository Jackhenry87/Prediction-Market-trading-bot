"""Release-time runner for the macro resolution-lag edge (Option 2B).

A long-lived process (meant for a small always-on VPS) that:
  1. finds the next macro release from macro_calendar,
  2. sleeps until shortly before it,
  3. bursts: repeatedly runs the trading pipeline (macro model only) every
     few seconds for a window, so the instant FRED shows the new number the
     known-outcome order fires with a limit,
  4. goes back to sleep until the next release.

Why a VPS and not GitHub cron: the edge window after a print is
minutes/seconds and GitHub cron can lag 5-15 min. This process fires on
time and polls fast. It still won't beat co-located HFT — the realistic
edge is thinner, less-watched prints where the book reprices slowly.

    python release_runner.py             # daemon: wait -> burst -> repeat
    python release_runner.py --once      # one burst now (testing)

Honors DRY_RUN/KILL_SWITCH/all caps via the normal pipeline.
"""

import os
import sys
import time
from datetime import datetime, timezone

from macro_calendar import next_release
from trade_logger import get_logger, setup_logging

log = get_logger("release_runner")

LEAD_SECONDS = 60        # wake this long before the scheduled time
WINDOW_SECONDS = 900     # keep polling this long after (the lag window)
POLL_SECONDS = 10        # how often to re-check within the window


def _run_pipeline_macro_only() -> int:
    """Run one pass of the trading pipeline restricted to the macro model.
    Returns orders placed (0 if none/ dry-run)."""
    os.environ["ENABLED_MODELS"] = "macro"     # focus the burst
    # import lazily so env is set first; reload settings each call
    import importlib
    import auto_trade
    importlib.reload(auto_trade)
    before = _exec_count()
    auto_trade.main()
    return _exec_count() - before


def _exec_count() -> int:
    from ledger import EXEC_LOG
    if not EXEC_LOG.exists():
        return 0
    with open(EXEC_LOG) as fh:
        return max(sum(1 for _ in fh) - 1, 0)


def burst(name: str = "manual") -> None:
    log.info("BURST for %s: polling every %ss for %ss", name, POLL_SECONDS,
             WINDOW_SECONDS)
    deadline = time.time() + WINDOW_SECONDS
    placed_total = 0
    while time.time() < deadline:
        try:
            placed = _run_pipeline_macro_only()
            placed_total += placed
            if placed:
                log.info("Captured %d order(s); continuing to watch for more.",
                         placed)
        except Exception as exc:
            log.error("pipeline pass failed: %s", exc)
        time.sleep(POLL_SECONDS)
    log.info("BURST done for %s: %d order(s) placed.", name, placed_total)


def daemon() -> int:
    setup_logging()
    log.info("Release runner started. Waiting for macro releases ...")
    while True:
        nxt = next_release()
        if not nxt:
            log.warning("No upcoming releases on the calendar (update "
                        "EXTRA_RELEASES). Sleeping 6h.")
            time.sleep(6 * 3600)
            continue
        when, name, series = nxt
        wait = (when - datetime.now(timezone.utc)).total_seconds() - LEAD_SECONDS
        log.info("Next: %s (%s) at %s UTC — sleeping %.0f min",
                 name, series, when.strftime("%Y-%m-%d %H:%M"), max(wait, 0) / 60)
        if wait > 0:
            time.sleep(wait)
        burst(name)
        time.sleep(max(LEAD_SECONDS, 120))  # avoid immediately re-triggering


def main() -> int:
    setup_logging()
    if "--once" in sys.argv:
        burst("once")
        return 0
    return daemon()


if __name__ == "__main__":
    sys.exit(main())
