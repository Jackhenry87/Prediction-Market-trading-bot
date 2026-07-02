"""Kalshi Phase 2: place ONE manually-triggered order, then exit. No loop.

Edit the ORDER PARAMETERS block, then run:

    python kalshi_place_order.py

Same gauntlet as always: kill switch, price/size sanity, MAX_ORDER_SIZE,
MAX_TOTAL_EXPOSURE — any violation is rejected and logged, nothing sent.
DRY_RUN=true logs what would be sent and stops. Start in KALSHI_ENV=demo
(fake money) before even thinking about prod.
"""

import sys

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import ExposureError, current_exposure_usd
from safety import check_order, order_notional_usdc
from trade_logger import get_logger, setup_logging

# ================= ORDER PARAMETERS — EDIT THESE =================
SIDE = "yes"          # "yes" or "no" — which outcome you're trading
ACTION = "buy"        # "buy" or "sell"
PRICE_CENTS = 10      # cents per contract, 1-99
COUNT = 10            # number of contracts
#                       max cost for a buy = PRICE_CENTS x COUNT
#                       (here: 10c x 10 = $1.00)
TICKER = ""           # leave empty to use MARKET_TICKER from .env
# =================================================================

log = get_logger("kalshi_place_order")


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=not TICKER)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    ticker = TICKER or settings.market_ticker
    price_usd = PRICE_CENTS / 100
    notional = order_notional_usdc(price_usd, COUNT)
    # safety.check_order expects BUY/SELL semantics for the exposure rule
    gate_side = "BUY" if ACTION == "buy" else "SELL"

    log.info(
        "Run mode: env=%s DRY_RUN=%s KILL_SWITCH=%s MAX_ORDER_SIZE=%.2f "
        "MAX_TOTAL_EXPOSURE=%.2f",
        settings.kalshi_env, settings.dry_run, settings.kill_switch,
        settings.max_order_size, settings.max_total_exposure,
    )
    if settings.kalshi_env == "prod" and not settings.dry_run:
        log.warning("PRODUCTION + LIVE: this will place a REAL-MONEY order.")
    log.info(
        "ORDER ATTEMPT: %s %s %d x %s @ %d¢ (max cost $%.2f) ticker=%s",
        ACTION.upper(), SIDE.upper(), COUNT, "contracts", PRICE_CENTS,
        notional, ticker,
    )

    if SIDE not in ("yes", "no") or ACTION not in ("buy", "sell"):
        log.error("ORDER REJECTED: SIDE must be yes/no, ACTION must be buy/sell")
        return 1
    if not isinstance(PRICE_CENTS, int) or not 1 <= PRICE_CENTS <= 99:
        log.error("ORDER REJECTED: PRICE_CENTS must be a whole number 1-99")
        return 1

    try:
        client = KalshiClient(
            settings.kalshi_api_key_id,
            settings.kalshi_private_key_path,
            settings.kalshi_env,
        )
        balance = client.get_balance_cents()
        log.info("Authenticated to Kalshi (%s). Balance: $%.2f",
                 settings.kalshi_env, balance / 100)
    except Exception as exc:
        log.error("Could not authenticate to Kalshi: %s", exc)
        return 1

    # Fail closed: no exposure number, no order — even in dry-run.
    try:
        exposure = current_exposure_usd(client)
    except ExposureError as exc:
        log.error("ORDER REJECTED: %s (failing closed)", exc)
        return 1

    if check_order(settings, gate_side, price_usd, COUNT, exposure):
        log.error("Order rejected by safety checks. Nothing was sent.")
        return 1

    if ACTION == "buy" and notional * 100 > balance:
        log.error("ORDER REJECTED: costs $%.2f but balance is only $%.2f",
                  notional, balance / 100)
        return 1

    if settings.dry_run:
        log.info(
            "DRY_RUN: order passed all checks and WOULD have been sent: "
            "%s %s %d contracts @ %d¢ on %s. No order was placed. "
            "Set DRY_RUN=false in .env to send it for real.",
            ACTION, SIDE, COUNT, PRICE_CENTS, ticker,
        )
        return 0

    log.info("LIVE MODE: posting order ...")
    try:
        resp = client.create_limit_order(ticker, SIDE, ACTION, COUNT, PRICE_CENTS)
    except Exception as exc:
        log.error("ORDER FAILED: %s", exc)
        return 1

    order = resp.get("order", resp)
    log.info("ORDER RESULT: id=%s status=%s", order.get("order_id"),
             order.get("status"))
    log.info("Full response: %s", resp)
    log.info("Done. Exiting (one order max per run).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
