"""Automated executor for the weather strategy. Runs ONCE and exits.

    python auto_trade.py

Each run: scores past paper signals, scans for fresh edges (prod market
data vs NWS forecast), takes AT MOST ONE signal per market day (the highest
EV — same-day signals are correlated), sizes it within the caps, and places
it through the full safety gate. Every hard rule applies:

  - DRY_RUN=true   -> logs the exact orders it would place, sends nothing
  - KILL_SWITCH    -> nothing is ever sent
  - MAX_ORDER_SIZE / MAX_TOTAL_EXPOSURE enforced per order and cumulatively
  - exposure unknown -> fail closed, no orders
  - everything logged to logs/

Environment note: signals are computed from REAL (prod) prices. Executing
against KALSHI_ENV=demo places the orders in the sandbox, whose books
differ — fine as a rehearsal, meaningless as a fill test. Real execution
means KALSHI_ENV=prod with a funded account.
"""

import sys

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import ExposureError, current_exposure_usd
from safety import check_order
from strategy_weather import scan, score_pending_paper_trades, SIGMA_F
from trade_logger import get_logger, setup_logging

log = get_logger("auto_trade")


def pick_best_per_event(results: list) -> list:
    """One signal per market day: same-day signals are the same weather bet."""
    chosen = []
    for r in results:
        if r["signals"]:
            best = max(r["signals"], key=lambda s: s["ev_cents"])
            chosen.append(dict(best, date=r["date"], mu=r["mu"]))
    return chosen


def size_order(price_cents: float, exposure_usd: float, settings) -> int:
    """Contracts purchasable within both caps. 0 = no room."""
    budget = min(settings.max_order_size,
                 settings.max_total_exposure - exposure_usd)
    if budget <= 0 or price_cents <= 0:
        return 0
    return int(budget * 100 // price_cents)


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    log.info(
        "AUTO-TRADE run: env=%s DRY_RUN=%s KILL_SWITCH=%s "
        "MAX_ORDER_SIZE=%.2f MAX_TOTAL_EXPOSURE=%.2f sigma=%.1fF",
        settings.kalshi_env, settings.dry_run, settings.kill_switch,
        settings.max_order_size, settings.max_total_exposure, SIGMA_F,
    )
    if settings.kalshi_env == "prod" and not settings.dry_run:
        log.warning("PRODUCTION + LIVE: real-money orders this run.")
    if settings.kalshi_env == "demo" and not settings.dry_run:
        log.warning("Demo execution: orders are placed against sandbox books, "
                    "which do not reflect the real prices behind the signals.")

    try:
        score_pending_paper_trades()
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)

    try:
        results = scan()
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        return 1

    picks = pick_best_per_event(results)
    if not picks:
        log.info("No signals today — nothing to trade. Exiting.")
        return 0

    try:
        client = KalshiClient(
            settings.kalshi_api_key_id,
            settings.kalshi_private_key_path,
            settings.kalshi_env,
        )
        exposure = current_exposure_usd(client)
    except ExposureError as exc:
        log.error("REFUSING TO TRADE: %s (failing closed)", exc)
        return 1
    except Exception as exc:
        log.error("Could not authenticate: %s", exc)
        return 1

    placed = 0
    for signal in picks:
        price = int(round(signal["price_cents"]))
        count = size_order(price, exposure, settings)
        if count < 1:
            log.info("SKIP %s: no room under caps (exposure $%.2f)",
                     signal["ticker"], exposure)
            continue
        notional = price * count / 100.0

        log.info(
            "ORDER ATTEMPT: buy %s %d x %s @ %d¢ ($%.2f) | model %.0f%% | "
            "EV +%.1fc",
            signal["side"], count, signal["ticker"], price, notional,
            100 * signal["model_prob"], signal["ev_cents"],
        )
        if check_order(settings, "BUY", price / 100.0, count, exposure):
            continue  # violations already logged

        if settings.dry_run:
            log.info("DRY_RUN: order not sent.")
            continue

        try:
            resp = client.create_limit_order(
                signal["ticker"], signal["side"], "buy", count, price
            )
        except Exception as exc:
            log.error("ORDER FAILED for %s: %s — continuing", signal["ticker"], exc)
            continue
        order = resp.get("order", resp)
        log.info("ORDER PLACED: %s", order)
        exposure += notional
        placed += 1

    log.info("Run complete: %d order(s) placed. Exiting (no loop).", placed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
