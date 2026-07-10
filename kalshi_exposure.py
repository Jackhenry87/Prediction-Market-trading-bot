"""Current exposure on Kalshi in USD: cost basis of open positions plus
cash reserved by resting buy orders.

Raises ExposureError when it cannot be determined — callers fail closed.
"""

from trade_logger import get_logger

log = get_logger("kalshi_exposure")


class ExposureError(Exception):
    pass


def _position_exposure_cents(p: dict):
    """Cost basis of one open position across API field vintages.
    Returns None when no known field is present (caller fails closed)."""
    if p.get("market_exposure") not in (None, ""):
        return abs(float(p["market_exposure"]))
    if p.get("market_exposure_dollars") not in (None, ""):
        return abs(float(p["market_exposure_dollars"])) * 100.0
    if p.get("total_traded") not in (None, ""):
        return abs(float(p["total_traded"]))
    if p.get("total_traded_dollars") not in (None, ""):
        return abs(float(p["total_traded_dollars"])) * 100.0
    return None


def _order_cost_cents(order: dict):
    """USD cents reserved by one resting order, across V1 (action buy/sell,
    side yes/no, <side>_price cents) and V2 (side bid/ask, price in dollar
    strings) field vocabularies. Returns None if unparseable (fail closed);
    0 for non-buying orders."""
    action = order.get("action")
    side = order.get("side")
    book_side = order.get("book_side")
    buying = (action == "buy" if action
              else (book_side or side) == "bid")
    if not buying:
        return 0

    # count across vintages: remaining_count / count (V1) and the fixed-point
    # *_fp variants Kalshi now returns (remaining_count_fp / count_fp / initial).
    remaining = None
    for f in ("remaining_count", "count", "remaining_count_fp", "count_fp",
              "initial_count_fp"):
        if order.get(f) not in (None, ""):
            remaining = order.get(f)
            break
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
    # the outcome side we're buying (V2 orders carry side='yes'/'no' plus a
    # book_side='bid'/'ask'); price is in cents (<side>_price) or dollar strings
    # (<side>_price_dollars, the current vintage).
    outcome = side if side in ("yes", "no") else None
    for base in ([outcome] if outcome else []) + ["yes", "no"]:
        c = order.get(f"{base}_price")
        if c not in (None, ""):
            return float(c) * remaining                 # already cents
        d = order.get(f"{base}_price_dollars")
        if d not in (None, ""):
            return float(d) * 100.0 * remaining         # dollars -> cents
    return None


def current_exposure_usd(client) -> float:
    try:
        positions = client.get_positions()
        orders = client.get_resting_orders()
    except Exception as exc:
        raise ExposureError(f"could not determine current exposure: {exc}") from exc

    position_cents = 0.0
    for p in positions.get("market_positions", []):
        if float(p.get("position", 0) or 0) == 0:
            continue
        cents = _position_exposure_cents(p)
        if cents is None:
            raise ExposureError(
                f"could not parse position (unknown fields): "
                f"{ {k: v for k, v in p.items() if v not in (None, '', 0)} }"
            )
        position_cents += cents

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
