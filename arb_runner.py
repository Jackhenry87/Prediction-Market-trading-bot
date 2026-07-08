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
ARB_CONTRACTS = int(os.getenv("ARB_CONTRACTS", "1"))


def place_basket(client, settings, arb: dict, exposure: float) -> bool:
    """Buy every leg of one basket on its side (yes/no) at the ask — a
    marketable limit so it fills now (arb needs the fill, not a better maker
    price). Returns True only if ALL legs were placed (or dry-run). A mid-basket
    failure is logged CRITICAL and returns False — the guarantee is void once a
    leg is missing."""
    placed = 0
    for ticker, ask, side in arb["legs"]:
        problems = check_order(settings, "BUY", ask / 100.0, ARB_CONTRACTS,
                               exposure)
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
        price = int(round(ask))             # marketable limit at the ask
        try:
            order = client.create_limit_order(ticker, side, "buy",
                                              ARB_CONTRACTS, price)
        except Exception as exc:
            log.critical("Leg %s FAILED (%s). %d/%d legs already placed — "
                         "PARTIAL BASKET %s, resolve manually.", ticker, exc,
                         placed, arb["n"], arb["event_ticker"])
            return False
        try:
            log_execution("arb", ticker, "yes", ARB_CONTRACTS, price,
                          str(order.get("order_id", "")))
        except Exception as exc:
            log.warning("Execution-log write failed for %s: %s", ticker, exc)
        exposure += ARB_CONTRACTS * price / 100.0
        placed += 1
    log.info("Basket %s: %d/%d legs placed (guaranteed +%.1fc/contract).",
             arb["event_ticker"], placed, arb["n"], arb["profit_cents"])
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
    balance = client.get_balance_cents() / 100.0
    bankroll = balance + exposure
    from dataclasses import replace
    settings = replace(settings,
                       max_order_size=scaled_order_cap(bankroll, settings),
                       max_total_exposure=scaled_exposure_cap(bankroll, settings))
    placed = 0
    for a in arbs:
        if a["event_ticker"] in done:
            continue
        log.info("ARB %s (%s): %d legs, guaranteed +%.1fc/contract",
                 a["title"], a["event_ticker"], a["n"], a["profit_cents"])
        if place_basket(client, settings, a, exposure):
            placed += 1
        done.add(a["event_ticker"])
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
    log.info("ARB RUNNER: env=%s DRY_RUN=%s, %d contracts/leg, min +%.1fc",
             settings.kalshi_env, settings.dry_run, ARB_CONTRACTS,
             kalshi_arb.ARB_MIN_PROFIT_CENTS)
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
