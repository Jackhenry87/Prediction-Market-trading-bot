"""Phase 2: place ONE manually-triggered order, then exit. No loop.

Edit the ORDER PARAMETERS block below, then run:

    python place_order.py

Order of operations, every time:
  1. Load settings; log run mode.
  2. Determine current exposure (fail closed if unknown).
  3. HARD-RULES gate (safety.check_order): kill switch, price/size sanity,
     MAX_ORDER_SIZE, MAX_TOTAL_EXPOSURE. Any violation -> rejected, logged,
     nothing sent.
  4. Check the market's tick size (price must land on a valid tick).
  5. DRY_RUN=true  -> log exactly what would be sent, exit. Nothing placed.
     DRY_RUN=false -> sign and post the order, log the result, exit.
"""

import sys

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from clob import build_client
from config import ConfigError, load_settings
from exposure import ExposureError, current_exposure_usdc
from safety import check_order, order_notional_usdc
from trade_logger import get_logger, setup_logging

# ================= ORDER PARAMETERS — EDIT THESE =================
SIDE = "BUY"          # "BUY" or "SELL"
PRICE = 0.10          # USDC per share, e.g. 0.10 = 10 cents
SIZE_SHARES = 10      # number of outcome shares
#                       cost = PRICE * SIZE_SHARES (here: 1.00 USDC)
#                       NOTE: Polymarket rejects orders worth less than $1
TOKEN_ID = ""         # leave empty to use MARKET_TOKEN_ID from .env
# =================================================================

log = get_logger("place_order")


def validate_tick(client, token_id: str, price: float, dry_run: bool) -> bool:
    """Price must be a multiple of the market's tick size, inside (tick, 1-tick)."""
    try:
        tick = float(client.get_tick_size(token_id))
    except Exception as exc:
        if dry_run:
            log.warning("Could not fetch tick size (%s); continuing dry-run anyway", exc)
            return True
        log.error("ORDER REJECTED: could not fetch tick size (%s); failing closed", exc)
        return False

    off_grid = abs(price / tick - round(price / tick)) > 1e-6
    if off_grid or not tick <= price <= 1 - tick:
        log.error(
            "ORDER REJECTED: price %s invalid for tick size %s "
            "(must be a multiple of %s between %s and %s)",
            price, tick, tick, tick, 1 - tick,
        )
        return False
    return True


def main() -> int:
    setup_logging()
    try:
        settings = load_settings(require_market=not TOKEN_ID)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    token_id = TOKEN_ID or settings.market_token_id
    side = SIDE.strip().upper()
    notional = order_notional_usdc(PRICE, SIZE_SHARES)

    log.info(
        "Run mode: DRY_RUN=%s KILL_SWITCH=%s MAX_ORDER_SIZE=%.2f MAX_TOTAL_EXPOSURE=%.2f",
        settings.dry_run, settings.kill_switch,
        settings.max_order_size, settings.max_total_exposure,
    )
    log.info(
        "ORDER ATTEMPT: %s %s shares @ %.4f (notional %.2f USDC) token_id=%s",
        side, SIZE_SHARES, PRICE, notional, token_id,
    )

    try:
        client = build_client(settings)
    except Exception as exc:
        log.error("Could not authenticate to CLOB: %s", exc)
        return 1

    # Fail closed: no exposure number, no order — even in dry-run.
    try:
        exposure = current_exposure_usdc(client, settings)
    except ExposureError as exc:
        log.error("ORDER REJECTED: %s (failing closed)", exc)
        return 1

    if check_order(settings, side, PRICE, SIZE_SHARES, exposure):
        log.error("Order rejected by safety checks. Nothing was sent.")
        return 1

    if not validate_tick(client, token_id, PRICE, settings.dry_run):
        return 1

    if settings.dry_run:
        log.info(
            "DRY_RUN: order passed all checks and WOULD have been sent as "
            "GTC limit order: %s %s shares @ %.4f on token %s. "
            "No order was placed. Set DRY_RUN=false in .env to trade for real.",
            side, SIZE_SHARES, PRICE, token_id,
        )
        return 0

    log.info("LIVE MODE: signing and posting order ...")
    try:
        order_args = OrderArgs(
            token_id=token_id,
            price=PRICE,
            size=float(SIZE_SHARES),
            side=BUY if side == "BUY" else SELL,
        )
        signed = client.create_order(order_args)
        resp = client.post_order(signed, OrderType.GTC)
    except Exception as exc:
        log.error("ORDER FAILED: %s", exc)
        return 1

    log.info("ORDER RESULT: %s", resp)
    if isinstance(resp, dict) and not resp.get("success", True):
        log.error("Exchange reported failure: %s", resp.get("errorMsg", "unknown"))
        return 1

    log.info("Done. Exiting (one order max per run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
