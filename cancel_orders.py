"""Cancel ALL of this wallet's open orders on the CLOB, then exit.

    python cancel_orders.py

DRY_RUN=true  -> lists open orders, cancels nothing.
DRY_RUN=false -> cancels everything and logs the result.

Note: KILL_SWITCH deliberately does NOT block this script — cancelling
orders reduces risk, and the kill switch must never trap orders in the book.
"""

import sys

from py_clob_client.clob_types import OpenOrderParams

from clob import build_client
from config import ConfigError, load_settings
from trade_logger import get_logger, setup_logging

log = get_logger("cancel_orders")


def main() -> int:
    setup_logging()
    try:
        settings = load_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    try:
        client = build_client(settings)
        orders = client.get_orders(OpenOrderParams()) or []
    except Exception as exc:
        log.error("Could not fetch open orders: %s", exc)
        return 1

    if not orders:
        log.info("No open orders. Nothing to cancel.")
        return 0

    log.info("Open orders (%d):", len(orders))
    for order in orders:
        log.info(
            "  id=%s %s %s @ %s (matched %s) token=%s",
            order.get("id"), order.get("side"), order.get("original_size"),
            order.get("price"), order.get("size_matched"), order.get("asset_id"),
        )

    if settings.dry_run:
        log.info("DRY_RUN: would cancel all %d order(s). Nothing cancelled. "
                 "Set DRY_RUN=false to cancel for real.", len(orders))
        return 0

    try:
        resp = client.cancel_all()
    except Exception as exc:
        log.error("Cancel failed: %s", exc)
        return 1

    log.info("CANCEL RESULT: %s", resp)
    log.info("Done. Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
