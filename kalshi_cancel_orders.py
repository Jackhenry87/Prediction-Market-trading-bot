"""Cancel ALL resting Kalshi orders, then exit.

    python kalshi_cancel_orders.py

DRY_RUN=true lists resting orders without cancelling. KILL_SWITCH does NOT
block this — cancelling reduces risk and must always be possible.
"""

import sys

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("kalshi_cancel")


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    try:
        client = KalshiClient(
            settings.kalshi_api_key_id,
            settings.kalshi_private_key_path,
            settings.kalshi_env,
        )
        orders = client.get_resting_orders()
    except Exception as exc:
        log.error("Could not fetch resting orders: %s", exc)
        return 1

    if not orders:
        log.info("No resting orders. Nothing to cancel.")
        return 0

    log.info("Resting orders (%d):", len(orders))
    for order in orders:
        side = order.get("side", "?")
        log.info(
            "  id=%s %s %s %sx @ %s¢ on %s (remaining %s)",
            order.get("order_id"), order.get("action"), side,
            order.get("count"), order.get(f"{side}_price"),
            order.get("ticker"), order.get("remaining_count"),
        )

    if settings.dry_run:
        log.info("DRY_RUN: would cancel all %d order(s). Nothing cancelled.",
                 len(orders))
        return 0

    failures = 0
    for order in orders:
        order_id = order.get("order_id")
        try:
            client.cancel_order(order_id)
            log.info("Cancelled %s", order_id)
        except Exception as exc:
            failures += 1
            log.error("Cancel failed for %s: %s", order_id, exc)

    if failures:
        log.error("%d cancel(s) failed — re-run to retry.", failures)
        return 1
    log.info("All orders cancelled. Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
