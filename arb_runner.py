"""Auto-place within-Kalshi risk-free baskets (see kalshi_arb).

For each complete mutually-exclusive basket trading below $1, buy ARB_CONTRACTS
of every leg with a marketable limit at the ask, locking the guaranteed margin.
All the usual rails apply (DRY_RUN, KILL_SWITCH, MAX_ORDER_SIZE,
MAX_TOTAL_EXPOSURE); each leg is risk-checked before it is sent.

DISCLOSED RISK: Kalshi has no atomic multi-leg order. Legs are placed one at a
time; if a later leg fails to fill, the basket is PARTIAL and no longer
risk-free. That is logged at CRITICAL and the ticker is skipped thereafter.

    python arb_runner.py --once
"""

import os
import sys
import time

import kalshi_arb
from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import ExposureError, current_exposure_usd
from ledger import log_execution
from safety import check_order, scaled_exposure_cap, scaled_order_cap
from trade_logger import get_logger, setup_logging

log = get_logger("arb_runner")

POLL_SECONDS = int(os.getenv("ARB_POLL_SECONDS", "300"))
RUN_MINUTES = float(os.getenv("ARB_RUN_MINUTES", "0"))     # 0 = single pass


def place_basket(client, settings, arb: dict, exposure: float) -> bool:
    """Buy `arb['count']` of every leg on its side (yes/no) at a marketable
    limit so it fills now (arb needs the fill, not a better maker price).
    Returns True only if ALL legs were placed (or dry-run). A mid-basket
    failure is logged CRITICAL and returns False — the guarantee is void once a
    leg is missing."""
    count = arb["count"]
    placed = 0
    for ticker, price_c, side in arb["legs"]:
        problems = check_order(settings, "BUY", price_c / 100.0, count, exposure)
        if problems:
            for p in problems:
                log.warning("BLOCKED leg %s: %s", ticker, p)
            if placed:
                log.critical("PARTIAL BASKET %s: %d/%d legs placed then blocked "
                             "— NOT risk-free, resolve manually.",
                             arb["event_ticker"], placed, arb["n"])
            return False
        if settings.dry_run:
            placed += 1
            continue
        price = int(round(price_c))         # marketable limit at the ask
        try:
            order = client.create_limit_order(ticker, side, "buy", count, price)
        except Exception as exc:
            log.critical("Leg %s FAILED (%s). %d/%d legs already placed — "
                         "PARTIAL BASKET %s, resolve manually.", ticker, exc,
                         placed, arb["n"], arb["event_ticker"])
            return False
        try:
            log_execution("arb", ticker, side, count, price,
                          str(order.get("order_id", "")))
        except Exception as exc:
            log.warning("Execution-log write failed for %s: %s", ticker, exc)
        exposure += count * price / 100.0
        placed += 1
    log.info("Basket %s: %d x %d-leg %s (guaranteed +%.1fc/contract = "
             "$%.2f locked).", arb["event_ticker"], count, arb["n"],
             arb["side"].upper(), arb["profit_cents"],
             arb["profit_cents"] * count / 100.0)
    return placed == arb["n"]


def arb_pass(client, settings, done: set) -> int:
    try:
        arbs = kalshi_arb.scan(client)
    except Exception as exc:
        log.error("Arb scan failed: %s", exc)
        return 0
    if not arbs:
        return 0
    try:
        exposure = current_exposure_usd(client)
    except ExposureError as exc:
        log.error("REFUSING TO PLACE: %s (failing closed)", exc)
        return 0
    balance_cents = client.get_balance_cents()
    bankroll = balance_cents / 100.0 + exposure
    from dataclasses import replace
    settings = replace(settings,
                       max_order_size=scaled_order_cap(bankroll, settings),
                       max_total_exposure=scaled_exposure_cap(bankroll, settings))
    placed = 0
    remaining = float(balance_cents)        # budget shrinks as baskets take it
    for a in arbs:
        if a["event_ticker"] in done:
            continue
        done.add(a["event_ticker"])
        # depth-aware size: as big as the book + your caps allow, no bigger
        sized = kalshi_arb.size_basket(client, a, remaining)
        if not sized or sized["count"] < 1:
            log.info("ARB %s: no size clears the buffer within the book/budget "
                     "— skipping.", a["event_ticker"])
            continue
        log.info("ARB %s (%s): buy %s x%d on %d legs -> guaranteed +%.1fc/ea "
                 "= $%.2f (cost $%.2f)", a["title"], a["event_ticker"],
                 a["side"].upper(), sized["count"], a["n"], sized["profit_cents"],
                 sized["basket_profit_usd"], sized["cost_cents"] / 100.0)
        if place_basket(client, settings, sized, exposure):
            placed += 1
            spent = sized["cost_cents"]
            remaining -= spent
            exposure += spent / 100.0
    return placed


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1
    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path, settings.kalshi_env)
    log.info("ARB RUNNER: env=%s DRY_RUN=%s, depth-sized up to %.0f%% of "
             "balance (reserve $%.2f), min +%.1fc/contract",
             settings.kalshi_env, settings.dry_run, kalshi_arb.ARB_MAX_BALANCE_PCT,
             kalshi_arb.ARB_RESERVE_USD, kalshi_arb.ARB_MIN_PROFIT_CENTS)
    if not settings.dry_run:
        log.warning("LIVE: will place real risk-free baskets this session.")
    done, once = set(), ("--once" in sys.argv or RUN_MINUTES <= 0)
    deadline = time.time() + RUN_MINUTES * 60
    total = 0
    while True:
        try:
            total += arb_pass(client, settings, done)
        except Exception as exc:
            log.error("Arb pass failed: %s", exc)
        if once or time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    log.info("Session done: %d basket(s) placed.", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
