"""Current exposure on Kalshi in USD: cost basis of open positions plus
cash reserved by resting buy orders.

Raises ExposureError when it cannot be determined — callers fail closed.
"""

from trade_logger import get_logger

log = get_logger("kalshi_exposure")


class ExposureError(Exception):
    pass


def current_exposure_usd(client) -> float:
    try:
        positions = client.get_positions()
        orders = client.get_resting_orders()
    except Exception as exc:
        raise ExposureError(f"could not determine current exposure: {exc}") from exc

    position_cents = sum(
        abs(int(p.get("market_exposure", 0) or 0))
        for p in positions.get("market_positions", [])
    )

    order_cents = 0
    for order in orders:
        if order.get("action") != "buy":
            continue
        side = order.get("side", "yes")
        price = int(order.get(f"{side}_price", 0) or 0)
        remaining = int(order.get("remaining_count", 0) or 0)
        order_cents += price * remaining

    total = (position_cents + order_cents) / 100
    log.info(
        "Current exposure: $%.2f (positions $%.2f + resting buy orders $%.2f)",
        total, position_cents / 100, order_cents / 100,
    )
    return total
