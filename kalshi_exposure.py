"""Current exposure on Kalshi in USD: cost basis of open positions plus
cash reserved by resting buy orders.

Raises ExposureError when it cannot be determined — callers fail closed.
"""

from trade_logger import get_logger

log = get_logger("kalshi_exposure")


class ExposureError(Exception):
    pass


def _order_cost_cents(order: dict):
    """USD cents reserved by one resting order, across V1 (action buy/sell,
    side yes/no, <side>_price cents) and V2 (side bid/ask, price in dollar
    strings) field vocabularies. Returns None if unparseable (fail closed);
    0 for non-buying orders."""
    action = order.get("action")
    side = order.get("side")
    buying = action == "buy" if action else side == "bid"
    if not buying:
        return 0

    remaining = order.get("remaining_count")
    if remaining is None:
        remaining = order.get("count")
    try:
        remaining = float(remaining)
    except (TypeError, ValueError):
        return None
    if remaining <= 0:
        return 0

    price = order.get("price")
    if price not in (None, ""):
        value = float(price)
        cents = value * 100 if value <= 1 else value  # dollars vs cents
        return cents * remaining
    if side in ("yes", "no") and order.get(f"{side}_price") not in (None, ""):
        return float(order[f"{side}_price"]) * remaining
    for field in ("yes_price", "no_price"):
        if order.get(field) not in (None, ""):
            return float(order[field]) * remaining
    return None


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
        cost = _order_cost_cents(order)
        if cost is None:
            raise ExposureError(
                f"could not parse resting order (unknown fields): "
                f"{ {k: v for k, v in order.items() if v not in (None, '')} }"
            )
        order_cents += cost

    total = (position_cents + order_cents) / 100
    log.info(
        "Current exposure: $%.2f (positions $%.2f + resting buy orders $%.2f)",
        total, position_cents / 100, order_cents / 100,
    )
    return total
