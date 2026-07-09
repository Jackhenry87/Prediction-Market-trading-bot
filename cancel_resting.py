"""Cancel EVERY resting order on the configured Kalshi account.

Used by the demo runner to clear stale resting orders before each pass so
taker orders fill against the real book instead of self-crossing our own
resting orders (which self-trade-prevention would just block).

Demo-guarded: refuses to run against prod unless ALLOW_PROD_CANCEL=true, so it
can never wipe the real account's resting orders by accident.

    KALSHI_ENV=demo python cancel_resting.py
"""

import os
import sys

from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("cancel_resting")


def main() -> int:
    setup_logging()
    env = os.getenv("KALSHI_ENV", "demo")
    if env != "demo" and os.getenv("ALLOW_PROD_CANCEL", "").lower() != "true":
        log.error("Refusing to cancel resting orders on env=%s (set "
                  "ALLOW_PROD_CANCEL=true to override).", env)
        return 1
    client = KalshiClient(os.getenv("KALSHI_API_KEY_ID"),
                          os.getenv("KALSHI_PRIVATE_KEY_PATH"), env)
    try:
        orders = client.get_resting_orders()
    except Exception as exc:
        log.error("Could not list resting orders: %s", exc)
        return 1
    log.info("Cancelling %d resting order(s) on %s...", len(orders), env)
    cancelled = 0
    for o in orders:
        oid = o.get("order_id")
        if not oid:
            continue
        try:
            client.cancel_order(str(oid))
            cancelled += 1
        except Exception as exc:
            log.warning("cancel failed for %s: %s", oid, exc)
    log.info("Cancelled %d/%d resting order(s).", cancelled, len(orders))
    return 0


if __name__ == "__main__":
    sys.exit(main())
