"""The HARD-RULES gate: every order, in every phase, passes through
check_order() before anything is signed or sent. Returns the list of
violations (empty list = order allowed); each violation is also logged.
"""

from trade_logger import get_logger

log = get_logger("safety")

VALID_SIDES = ("BUY", "SELL")


def order_notional_usdc(price: float, size_shares: float) -> float:
    return price * size_shares


def check_order(settings, side: str, price: float, size_shares: float,
                current_exposure_usdc: float) -> list:
    """Apply kill switch, sanity checks, and both USDC limits.

    current_exposure_usdc: USDC already committed (open BUY orders + value of
    held positions). Computed by exposure.py; the caller must fail closed if
    it cannot be determined.
    """
    problems = []

    if settings.kill_switch:
        problems.append("KILL_SWITCH is on — bot refuses to place any order")

    if side not in VALID_SIDES:
        problems.append(f"side must be BUY or SELL, got {side!r}")

    if not 0 < price < 1:
        problems.append(f"price must be between 0 and 1 (exclusive), got {price}")

    if size_shares <= 0:
        problems.append(f"size must be > 0 shares, got {size_shares}")

    notional = order_notional_usdc(price, size_shares)

    if notional > settings.max_order_size:
        problems.append(
            f"order notional {notional:.2f} USDC exceeds "
            f"MAX_ORDER_SIZE {settings.max_order_size:.2f}"
        )

    if side == "BUY" and current_exposure_usdc + notional > settings.max_total_exposure:
        problems.append(
            f"current exposure {current_exposure_usdc:.2f} + order "
            f"{notional:.2f} = {current_exposure_usdc + notional:.2f} USDC "
            f"would exceed MAX_TOTAL_EXPOSURE {settings.max_total_exposure:.2f}"
        )

    for problem in problems:
        log.warning("ORDER REJECTED: %s", problem)
    return problems
