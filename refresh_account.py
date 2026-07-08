"""Refresh the scoreboard's Account section straight from Kalshi's REST API —
the authoritative source (no manual CSV uploads).

Every request is signed with KALSHI-ACCESS-KEY / -TIMESTAMP / -SIGNATURE
(RSA-PSS) by KalshiClient. This pulls the granular portfolio — balance, every
open position (paged across the cursor), and settlements — marks the book to
current market prices, writes account_snapshot.json, and rebuilds SCOREBOARD.md.

    python refresh_account.py       # one authoritative refresh

Equity = cash balance + live mark-to-market value of open positions. Net P&L =
equity - KALSHI_DEPOSITS_USD. An empty positions fetch now retries and, if it
still comes back empty while open orders remain on record, reuses the last
known value instead of zeroing the book.
"""

import sys

from auto_trade import write_account_snapshot
from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("refresh_account")


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1
    try:
        client = KalshiClient(settings.kalshi_api_key_id,
                              settings.kalshi_private_key_path,
                              settings.kalshi_env)
    except Exception as exc:
        log.error("Could not build Kalshi client (need API key + PEM): %s", exc)
        return 1

    try:
        write_account_snapshot(client)
    except Exception as exc:
        log.error("Account refresh failed: %s", exc)
        return 1

    try:
        import scoreboard
        scoreboard.build()
        log.info("Scoreboard rebuilt from live Kalshi portfolio.")
    except Exception as exc:
        log.warning("Snapshot written but scoreboard rebuild failed: %s", exc)

    # the spreadsheets: real book (open + settled since RECORD_SINCE) and the
    # separate paper-trades sheet. GitHub renders both as sortable tables.
    import os
    since = os.getenv("RECORD_SINCE", "2026-07-02")
    try:
        import kalshi_report
        kalshi_report.build(since, client=client)
    except Exception as exc:
        log.warning("kalshi_report build failed: %s", exc)
    try:
        import paper_report
        paper_report.build()
    except Exception as exc:
        log.warning("paper_report build failed: %s", exc)
    # fill-by-fill history (trade_history.csv): every fill joined to its
    # settlement, the granular audit trail beneath the position-level report.
    try:
        import backfill_history
        backfill_history.build(since)
    except Exception as exc:
        log.warning("trade_history build failed: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
