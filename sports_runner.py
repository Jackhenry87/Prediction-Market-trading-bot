"""Always-on SPORTS runner — the selective sharp-line tracker, 24/7.

Separate from the hourly pipeline (like the smart-money copier): this
process polls the sharp sportsbook lines continuously and places only the
few best plays a day. strategy_sports.scan already applies the gates
(real steam move + confidence floor + edge) and the SPORTS_MAX_PER_DAY
budget; this runner just sizes, risk-checks, and places the survivors,
telling scan how many it has placed THIS session so the daily cap holds
across polls.

    python sports_runner.py            # loop for SPORTS_RUN_MINUTES
    python sports_runner.py --once     # single scan+place pass
"""

import os
import sys
import time

import strategy_sports
from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import ExposureError, current_exposure_usd
from ledger import log_execution
from safety import check_order, scaled_exposure_cap, scaled_order_cap
from trade_logger import get_logger, setup_logging

log = get_logger("sports_runner")

POLL_SECONDS = int(os.getenv("SPORTS_POLL_SECONDS", "900"))    # 15 min
RUN_MINUTES = float(os.getenv("SPORTS_RUN_MINUTES", "110"))
ORDER_PCT = float(os.getenv("SPORTS_ORDER_PCT", "3"))          # % bankroll/pick


def contracts_for(budget_usd: float, price_cents: float) -> int:
    if price_cents <= 0:
        return 0
    return int(budget_usd * 100 // price_cents)


def sports_pass(client, settings, session: dict) -> int:
    """One scan-and-place pass. Returns orders placed."""
    from dataclasses import replace

    from auto_trade import event_of, maker_price
    try:
        results = strategy_sports.scan(settings.odds_api_key)
    except Exception as exc:
        log.error("Sports scan failed: %s", exc)
        return 0
    if not results:
        return 0
    try:
        strategy_sports.append_paper_trades(
            [s for r in results for s in r["signals"]], results[0]["date"])
    except Exception as exc:
        log.warning("Sports signal ledger write failed: %s", exc)

    try:
        balance = client.get_balance_cents() / 100.0
        exposure = current_exposure_usd(client)
        positions = client.get_positions()
        resting = client.get_resting_orders()
    except ExposureError as exc:
        log.error("REFUSING TO PLACE: %s (failing closed)", exc)
        return 0
    from auto_trade import held_tickers
    held = held_tickers(positions, resting)
    bankroll = balance + exposure
    settings = replace(settings,
                       max_order_size=scaled_order_cap(bankroll, settings),
                       max_total_exposure=scaled_exposure_cap(bankroll,
                                                              settings))
    placed = 0
    flat = [(r, s) for r in results for s in r["signals"]]
    flat.sort(key=lambda rs: -rs[1].get("ev_cents", 0.0))
    for r, s in flat:
        ticker, price = s["ticker"], s["price_cents"]
        event = event_of(ticker)
        if (ticker in held or ticker in session["placed"]
                or event in session["events"]):
            continue
        budget = bankroll * ORDER_PCT / 100.0
        count = contracts_for(budget, price)
        if count < 1:
            continue
        notional = count * price / 100.0
        problems = check_order(settings, "BUY", price / 100.0, count, exposure)
        if problems:
            for p in problems:
                log.warning("BLOCKED %s: %s", ticker, p)
            continue
        log.info("SHARP PLAY: buy %s %d x %s @ %.0fc ($%.2f, %.1fc edge) | %s",
                 s["side"], count, ticker, price, notional,
                 s.get("ev_cents", 0), s.get("subtitle", ""))
        if settings.dry_run:
            log.info("DRY_RUN: not sent.")
            session["placed"].add(ticker)
            session["events"].add(event)
            continue
        placed_price = maker_price(price, "sports")
        try:
            order = client.create_limit_order(ticker, s["side"], "buy",
                                              count, placed_price)
        except Exception as exc:
            log.error("Order failed for %s: %s", ticker, exc)
            continue
        try:
            log_execution("sports", ticker, s["side"], count, placed_price,
                          str(order.get("order_id", "")))
        except Exception as exc:
            log.warning("Execution-log write failed: %s", exc)
        session["placed"].add(ticker)
        session["events"].add(event)
        exposure += notional
        placed += 1
    return placed


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1
    if not settings.odds_api_key:
        log.error("ODDS_API_KEY not set — sports runner needs sportsbook odds.")
        return 1
    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path, settings.kalshi_env)
    log.info("SPORTS RUNNER: env=%s DRY_RUN=%s, <=%d ML + %d O/U picks/day, "
             "poll %ds", settings.kalshi_env, settings.dry_run,
             strategy_sports.SPORTS_MAX_ML_PER_DAY,
             strategy_sports.SPORTS_MAX_TOTALS_PER_DAY, POLL_SECONDS)
    if not settings.dry_run:
        log.warning("LIVE: real-money sharp plays this session.")

    session = dict(placed=set(), events=set())   # dedup across polls
    once = "--once" in sys.argv
    deadline = time.time() + RUN_MINUTES * 60
    total = 0
    while True:
        try:
            total += sports_pass(client, settings, session)
        except Exception as exc:
            log.error("Sports pass failed: %s — retrying next poll", exc)
        if once or time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    log.info("Session done: %d sharp play(s) placed.", total)
    # the runner owns its ledgers (sole writer -> no cross-workflow conflict):
    # score settled picks so the scoreboard is current.
    try:
        from strategy_weather import score_pending_paper_trades
        score_pending_paper_trades(strategy_sports.PAPER_LOG)
    except Exception as exc:
        log.warning("Sports scoring failed: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
