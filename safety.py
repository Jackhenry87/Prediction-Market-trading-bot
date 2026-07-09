"""The HARD-RULES gate: every order, in every phase, passes through
check_order() before anything is signed or sent. Returns the list of
violations (empty list = order allowed); each violation is also logged.
"""

import os

from trade_logger import get_logger

log = get_logger("safety")

VALID_SIDES = ("BUY", "SELL")

# Longshot floor: refuse to BUY any directional contract the market prices below
# this (a sub-N% outcome — a lottery ticket / the "implausibly large edge means
# your model is wrong" trap that put 98 NO on a 1-8c bucket). Risk-free arb legs
# opt out (they pass min_price_cents=0) because a cheap HEDGED leg isn't a bet on
# a longshot. Raise MIN_PRICE_CENTS to ban a wider band of near-longshots.
MIN_PRICE_CENTS = float(os.getenv("MIN_PRICE_CENTS", "5"))


def order_notional_usdc(price: float, size_shares: float) -> float:
    return price * size_shares


def scaled_order_cap(bankroll_usd: float, settings) -> float:
    """Per-order hard cap that GROWS with the account (owner spec): the
    larger of the static MAX_ORDER_SIZE (the floor, so a small account can
    still trade) and MAX_ORDER_BANKROLL_PCT% of bankroll — but never above
    the MAX_ORDER_ABS ceiling, the ultimate backstop no bug can exceed."""
    pct = float(os.getenv("MAX_ORDER_BANKROLL_PCT", "10"))
    ceiling = float(os.getenv("MAX_ORDER_ABS", "50"))
    return min(max(settings.max_order_size, bankroll_usd * pct / 100.0),
               ceiling)


def scaled_exposure_cap(bankroll_usd: float, settings) -> float:
    """Total-exposure hard cap that grows with the account: the larger of
    the static MAX_TOTAL_EXPOSURE floor and MAX_EXPOSURE_BANKROLL_PCT% of
    bankroll, never above the MAX_EXPOSURE_ABS ceiling."""
    pct = float(os.getenv("MAX_EXPOSURE_BANKROLL_PCT", "80"))
    ceiling = float(os.getenv("MAX_EXPOSURE_ABS", "250"))
    return min(max(settings.max_total_exposure, bankroll_usd * pct / 100.0),
               ceiling)


def check_order(settings, side: str, price: float, size_shares: float,
                current_exposure_usdc: float, min_price_cents: float = None) -> list:
    """Apply kill switch, sanity checks, the longshot floor, and both USDC limits.

    current_exposure_usdc: USDC already committed (open BUY orders + value of
    held positions). Computed by exposure.py; the caller must fail closed if
    it cannot be determined.

    min_price_cents: the longshot floor for BUYs (defaults to MIN_PRICE_CENTS).
    Risk-free arb legs pass 0 to opt out — a cheap hedged leg is not a longshot.
    """
    problems = []
    floor_cents = MIN_PRICE_CENTS if min_price_cents is None else min_price_cents

    if settings.kill_switch:
        problems.append("KILL_SWITCH is on — bot refuses to place any order")

    if side not in VALID_SIDES:
        problems.append(f"side must be BUY or SELL, got {side!r}")

    if not 0 < price < 1:
        problems.append(f"price must be between 0 and 1 (exclusive), got {price}")

    if side == "BUY" and 0 < price < 1 and price * 100 < floor_cents:
        problems.append(
            f"price {price * 100:.0f}c is below the longshot floor "
            f"{floor_cents:.0f}c — refusing a bet on a sub-{floor_cents:.0f}% "
            f"outcome (an implausibly large edge means the model is wrong)"
        )

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
