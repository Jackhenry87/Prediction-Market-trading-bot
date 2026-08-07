"""Live executor for the favourite-bias maker model. Runs ONCE and exits.

    python favorite_runner.py

This is the only code path in the repo that can place a real order. Every
order goes through the shared safety gate, and three properties are enforced
here rather than assumed:

  1. MAKER ONLY. The signal price is bid+1 as of the scan, but the book moves
     between scanning and placing. Before every order the market is re-fetched
     and the price re-checked against the CURRENT ask; if it would now cross,
     the order is re-priced down or skipped. A crossed "maker" order pays the
     taker fee and destroys the edge it was placed to capture.

  2. SIZED, NOT FLAT. Contracts come from sizing.size_position — quarter Kelly
     on the signal's own edge — not from a fixed stake. Signals too thin to
     pay for one contract are skipped, never rounded up.

  3. FAIL CLOSED. If exposure cannot be determined, nothing is placed.

DRY_RUN=true logs the exact orders and sends nothing. KILL_SWITCH=true blocks
everything at the safety gate.
"""

import csv
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import strategy_favorite as fav
from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import (ExposureError, current_exposure_usd,
                             order_price_cents)
from ledger import log_execution, log_signals
from safety import check_order, scaled_exposure_cap, scaled_order_cap
from trade_logger import get_logger, setup_logging

log = get_logger("favorite_runner")

MODEL = "favorite"

# --- daily circuit breakers -------------------------------------------------
# The per-order and total-exposure caps bound how much is at risk at any
# instant, but neither bounds how much can be COMMITTED IN A DAY: a single
# scan can emit 25 signals, and the exposure cap alone would let ~90% of a $50
# bankroll go out in one run. These two caps bound the worst realistic day.
#
# They count orders PLACED today (UTC) from the executed ledger, so they
# survive the runner being invoked many times a day by the scheduler.
MAX_ORDERS_PER_DAY = int(os.getenv("MAX_ORDERS_PER_DAY", "8"))
MAX_DAILY_DEPLOY_USD = float(os.getenv("MAX_DAILY_DEPLOY_USD", "15"))

# Correlated-theme cap ACROSS THE DAY. strategy_favorite caps themes within a
# single scan, but the scheduler runs ~70 scans a day: two inflation bets per
# scan would still fill the entire daily budget with one bet on one CPI print.
# The per-scan cap alone does not achieve the goal; this one does.
MAX_ORDERS_PER_THEME_PER_DAY = int(
    os.getenv("MAX_ORDERS_PER_THEME_PER_DAY", "3"))

EXEC_LOG = Path(__file__).resolve().parent / "executed_trades.csv"


def placed_today(path: Path = None, now: datetime = None) -> tuple:
    """(order_count, usd_deployed, {theme: count}) for this model today, UTC.

    Read from the executed ledger rather than held in memory because the
    scheduler runs this process fresh every 20 minutes — in-process counters
    would reset each time and cap nothing. Themes are derived from the ticker
    so no extra ledger column is needed.
    """
    path = path or EXEC_LOG
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    count, usd, themes = 0, 0.0, {}
    if not path.exists():
        return count, usd, themes
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            if r.get("model") != MODEL:
                continue
            if not str(r.get("placed_at_utc", "")).startswith(today):
                continue
            count += 1
            th = fav.theme_of(r.get("ticker", ""))
            themes[th] = themes.get(th, 0) + 1
            try:
                usd += float(r.get("cost_usd") or 0)
            except ValueError:
                pass
    return count, usd, themes


# How long a maker bid may rest before it is cancelled and re-priced by a
# later scan. A resting limit order with no expiry is a free option written to
# the rest of the market: the only traders who exercise it are the ones who
# know something we do not. Markets here close up to a week out, so without
# this an 86c bid could sit in the book for six days while the market walks
# away from it.
RESTING_TTL_H = float(os.getenv("RESTING_TTL_H", "6"))


def cancel_reason(order: dict, market: dict, now: datetime):
    """Why this resting bid should be pulled, or None to leave it.

    The dangerous case is not age by itself — it is a bid left ABOVE the
    market's own mid after the price walks away from us. That order no longer
    buys below fair value; it offers to overpay, and it will be hit precisely
    when someone informed wants out.
    """
    price = order_price_cents(order)
    side = order.get("side")

    try:
        created = datetime.fromisoformat(
            str(order.get("created_time") or "").replace("Z", "+00:00"))
        age_h = (now - created).total_seconds() / 3600.0
    except ValueError:
        age_h = None

    if price is None or side not in ("yes", "no"):
        # Cannot judge it on price. Fall back to age alone rather than
        # leaving an unreadable order resting forever.
        if age_h is not None and age_h >= RESTING_TTL_H:
            return f"unreadable order resting {age_h:.1f}h"
        return None

    book = fav.favourite_side(market)
    if book is None:
        return None                       # no quote to judge against; leave it
    fav_side, bid, ask = book
    if fav_side != side:
        return f"favourite flipped to {fav_side}, our {side} bid is the longshot"

    mid = (bid + ask) / 2.0
    if price > mid:
        return (f"bid {price:.0f}c is above the mid {mid:.1f}c — the book "
                f"moved away and this now offers to overpay")
    if not fav.FAV_MIN_PRICE <= price <= fav.FAV_MAX_PRICE:
        return f"bid {price:.0f}c drifted outside the trading band"
    if age_h is not None and age_h >= RESTING_TTL_H:
        return f"resting {age_h:.1f}h (TTL {RESTING_TTL_H:.0f}h) — re-price it"
    return None


def cancel_stale_resting(client, resting: list, now: datetime = None) -> int:
    """Pull resting bids that have gone stale. Returns how many were cancelled.

    Runs before the scan so a cancelled ticker becomes eligible again and the
    next pass re-places it at a price that reflects the current book.
    """
    now = now or datetime.now(timezone.utc)
    cancelled = 0
    for o in resting or []:
        action = (o.get("action") or "").lower()
        book_side = (o.get("book_side") or "").lower()
        if action and action != "buy":
            continue
        if not action and book_side and book_side != "bid":
            continue
        ticker = o.get("ticker")
        oid = o.get("order_id")
        if not ticker or not oid:
            continue
        try:
            market = client.get_market(ticker)
        except Exception as exc:
            log.warning("could not re-check %s (%s) — leaving it", ticker, exc)
            continue
        reason = cancel_reason(o, market, now)
        if not reason:
            continue
        try:
            client.cancel_order(str(oid))
            log.info("CANCELLED %s: %s", ticker, reason)
            cancelled += 1
        except Exception as exc:
            log.error("cancel failed for %s: %s", ticker, exc)
    return cancelled


def maker_price_now(market: dict, side: str, wanted: float):
    """Re-check a signal price against the live book.

    Returns the price to actually place, or None if there is no maker price
    left. `wanted` was computed at scan time; if the ask has since fallen to
    or below it, resting there would cross and we would pay the taker fee.
    """
    book = fav.favourite_side(market)
    if not book:
        return None
    now_side, bid, ask = book
    if now_side != side:
        return None                      # the favourite flipped sides on us
    price = min(wanted, ask - 1.0)       # never at or through the ask
    if price < bid:
        return None                      # the book moved past us entirely
    if not fav.FAV_MIN_PRICE <= price <= fav.FAV_MAX_PRICE:
        return None
    return float(int(price))


def place_signals(client, settings, signals: list, bankroll: float,
                  exposure: float, held: set) -> int:
    """Place each sized signal that survives the live re-check and the gate."""
    placed = 0
    events = set()
    day_count, day_usd, day_themes = placed_today()
    log.info("Today so far: %d order(s), $%.2f deployed, themes %s "
             "(caps: %d orders, $%.2f, %d per theme)", day_count, day_usd,
             day_themes or "{}", MAX_ORDERS_PER_DAY, MAX_DAILY_DEPLOY_USD,
             MAX_ORDERS_PER_THEME_PER_DAY)

    for s in sorted(signals, key=lambda x: -x["ev_cents"]):
        ticker = s["ticker"]
        event = fav._event_of(ticker)
        if ticker in held or event in events:
            log.info("SKIP %s: already held or same event this run", ticker)
            continue

        if day_count >= MAX_ORDERS_PER_DAY:
            log.info("DAILY ORDER CAP reached (%d) — stopping for today",
                     MAX_ORDERS_PER_DAY)
            break

        try:
            market = client.get_market(ticker)
        except Exception as exc:
            log.warning("SKIP %s: could not re-fetch market (%s)", ticker, exc)
            continue

        price = maker_price_now(market, s["side"], s["price_cents"])
        if price is None:
            log.info("SKIP %s: no maker price left (book moved)", ticker)
            continue
        if price != s["price_cents"]:
            log.info("RE-PRICED %s: %.0fc -> %.0fc (book moved)",
                     ticker, s["price_cents"], price)

        theme = s.get("theme") or fav.theme_of(ticker)
        if day_themes.get(theme, 0) >= MAX_ORDERS_PER_THEME_PER_DAY:
            log.info("SKIP %s: theme '%s' already at its daily cap (%d) — "
                     "these resolve off the same driver", ticker, theme,
                     MAX_ORDERS_PER_THEME_PER_DAY)
            continue

        count = s["contracts"]
        notional = count * price / 100.0

        if day_usd + notional > MAX_DAILY_DEPLOY_USD:
            log.info("SKIP %s: $%.2f would exceed today's $%.2f deploy cap "
                     "($%.2f used)", ticker, notional, MAX_DAILY_DEPLOY_USD,
                     day_usd)
            continue

        problems = check_order(settings, "BUY", price / 100.0, count, exposure)
        if problems:
            for p in problems:
                log.warning("BLOCKED %s: %s", ticker, p)
            continue

        log.info("MAKER BUY %s %d x %s @ %.0fc ($%.2f) | edge %.2fc | %s",
                 s["side"], count, ticker, price, notional, s["ev_cents"],
                 s["sizing_reason"])

        if settings.dry_run:
            log.info("DRY_RUN: not sent.")
            events.add(event)
            # count it anyway so a dry run exercises the same daily caps a
            # live run would hit, instead of reporting an impossible day
            day_count += 1
            day_usd += notional
            day_themes[theme] = day_themes.get(theme, 0) + 1
            continue

        try:
            order = client.create_limit_order(ticker, s["side"], "buy",
                                              count, int(price))
        except Exception as exc:
            log.error("Order failed for %s: %s — continuing", ticker, exc)
            continue

        order_id = str((order.get("order") or order).get("order_id", ""))
        try:
            log_execution(MODEL, ticker, s["side"], count, int(price), order_id)
        except Exception as exc:
            log.warning("Execution-log write failed for %s: %s", ticker, exc)

        events.add(event)
        exposure += notional
        day_count += 1
        day_usd += notional
        day_themes[theme] = day_themes.get(theme, 0) + 1
        placed += 1
    return placed


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path,
                          settings.kalshi_env)

    try:
        balance = client.get_balance_cents() / 100.0
        exposure = current_exposure_usd(client)
        positions = client.get_positions()
        resting = client.get_resting_orders()
    except ExposureError as exc:
        log.error("REFUSING TO PLACE: %s (failing closed)", exc)
        return 1
    except Exception as exc:
        log.error("REFUSING TO PLACE: account read failed: %s", exc)
        return 1

    # Pull stale bids BEFORE building the held-set, so a cancelled ticker
    # becomes eligible for a fresh, correctly-priced order this same run.
    n = cancel_stale_resting(client, resting)
    if n:
        try:
            resting = client.get_resting_orders()
            exposure = current_exposure_usd(client)
        except Exception as exc:
            log.warning("re-read after cancels failed (%s) — using prior view",
                        exc)

    from auto_trade import held_tickers
    held = held_tickers(positions, resting)
    bankroll = balance + exposure
    log.info("Bankroll $%.2f (cash $%.2f + exposure $%.2f), %d held ticker(s)",
             bankroll, balance, exposure, len(held))

    # Independent second cap on top of the sizing model's own 5% limit.
    settings = replace(
        settings,
        max_order_size=scaled_order_cap(bankroll, settings),
        max_total_exposure=scaled_exposure_cap(bankroll, settings))

    try:
        results = fav.scan(client, bankroll_usd=bankroll)
    except Exception as exc:
        log.error("Scan failed: %s", exc)
        return 1
    if not results:
        log.info("No qualifying signals this run.")
        return 0

    # Record the intent before touching the exchange, so the paper record
    # exists even if placement fails or the run dies mid-way.
    try:
        log_signals(results, fav.PAPER_LEDGER)
    except Exception as exc:
        log.warning("Signal ledger write failed: %s", exc)

    signals = [s for r in results for s in r["signals"]]
    placed = place_signals(client, settings, signals, bankroll, exposure, held)
    log.info("Run complete: %d maker order(s) placed.", placed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
