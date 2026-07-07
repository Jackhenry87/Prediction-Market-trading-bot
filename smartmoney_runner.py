"""Always-on smart-money COPY runner — its own thing, NOT the hourly bot.

The hourly pipeline scans many models on a schedule; this process does one
job continuously: watch Polymarket's proven-profitable wallets and, the
moment >= SM_MIN_WALLETS of them have piled onto a pick we can reach on
Kalshi, place the copy IMMEDIATELY, sized by conviction at
SM_COPY_MIN_PCT..SM_COPY_MAX_PCT of bankroll (owner spec: 4-8%).

  sizing   base SM_COPY_MIN_PCT% of bankroll (cash + positions), +1% per
           sharp beyond the minimum, capped at SM_COPY_MAX_PCT%.
  rails    every order still passes the hard-rules gate (KILL_SWITCH,
           DRY_RUN, MAX_ORDER_SIZE, MAX_TOTAL_EXPOSURE) — raise the
           MAX_* repo Variables if they start clamping the % sizing.
  dedupe   never re-copies a market already held/resting/copied this
           session, and — critically — NEVER both sides of one event.
           The one-bet-per-event rule is enforced two ways so it can't
           slip through a poll gap: (1) a live snapshot of held/resting
           positions each pass, and (2) a durable set of every event this
           SESSION has already touched. (2) matters because a maker order
           placed one pass may not show up in the live snapshot on the
           next pass (in-flight / fill lag) — without durable memory the
           copier could buy the OTHER side of the same match 10 minutes
           later. It has done exactly that; this is the guard.
  records  orders are logged locally AND recovered by the hourly runs'
           fills reconciliation, so the scoreboard stays true even
           though this process never commits to git.

    python smartmoney_runner.py            # loop for SM_RUN_MINUTES
    python smartmoney_runner.py --once     # single scan+copy pass
"""

import os
import sys
import time

import strategy_smartmoney
from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import ExposureError, current_exposure_usd
from ledger import log_execution, log_signals
from safety import check_order
from trade_logger import get_logger, setup_logging

log = get_logger("smartmoney_runner")

POLL_SECONDS = int(os.getenv("SM_POLL_SECONDS", "600"))
RUN_MINUTES = float(os.getenv("SM_RUN_MINUTES", "110"))
COPY_MIN_PCT = float(os.getenv("SM_COPY_MIN_PCT", "4"))
COPY_MAX_PCT = float(os.getenv("SM_COPY_MAX_PCT", "8"))


def copy_pct(wallets: int) -> float:
    """Conviction sizing: 4% of bankroll at the minimum consensus, +1% per
    extra sharp, capped at 8% (owner spec)."""
    extra = max(0, wallets - strategy_smartmoney.MIN_WALLETS)
    return min(COPY_MAX_PCT, COPY_MIN_PCT + extra)


def contracts_for(budget_usd: float, price_cents: float) -> int:
    """Whole contracts the budget buys at this price (>=1 if affordable)."""
    if price_cents <= 0:
        return 0
    return int(budget_usd * 100 // price_cents)


def held_and_events(client) -> tuple:
    """(held tickers, events already bet) from live positions + resting."""
    from auto_trade import event_of, held_tickers
    positions = client.get_positions()
    resting = client.get_resting_orders()
    held = held_tickers(positions, resting)
    return held, {event_of(t) for t in held}


def signal_event(sig: dict) -> str:
    """The authoritative event key for a signal: the event_ticker the
    mapper carried from Kalshi's own metadata, falling back to the
    ticker-derived prefix. Two markets on one match share this key, so
    it is what we dedupe on to guarantee we never hold both sides."""
    from auto_trade import event_of
    return sig.get("event_ticker") or event_of(sig.get("ticker", ""))


def copy_pass(client, settings, session_seen: set,
              session_events: set) -> int:
    """One scan-and-copy pass. Returns orders placed.

    ``session_events`` persists ACROSS passes for the whole session; every
    event we place into is recorded there so a later pass can never take
    the opposite side, even if the first order hasn't surfaced in the live
    position snapshot yet."""
    try:
        exited = strategy_smartmoney.check_exits(client, settings)
        if exited:
            log.info("Cashed out %d copy(ies) the sharps abandoned.", exited)
    except Exception as exc:
        log.warning("Exit check failed: %s", exc)
    results = strategy_smartmoney.scan()
    if not results:
        return 0
    try:
        log_signals(results, strategy_smartmoney.PAPER_LOG)
    except Exception as exc:
        log.warning("Signal ledger write failed: %s", exc)

    try:
        balance_usd = client.get_balance_cents() / 100.0
        exposure = current_exposure_usd(client)
        held, held_events = held_and_events(client)
    except ExposureError as exc:
        log.error("REFUSING TO COPY: %s (failing closed)", exc)
        return 0
    bankroll = balance_usd + exposure
    # hard caps GROW with the account (owner spec): floor at the static
    # env values, scale by bankroll, absolute ceilings as the backstop
    from dataclasses import replace

    from safety import scaled_exposure_cap, scaled_order_cap
    settings = replace(settings,
                       max_order_size=scaled_order_cap(bankroll, settings),
                       max_total_exposure=scaled_exposure_cap(bankroll,
                                                              settings))
    placed = 0

    flat = [(r, s) for r in results for s in r["signals"]]
    # place the highest-CONVICTION copies first so, if a cap binds, the
    # sharpest-sized pile gets funded ahead of a whale's routine bet
    flat.sort(key=lambda rs: -rs[1].get("conviction",
                                        rs[1].get("stake", 0.0)))
    for r, s in flat:
        from auto_trade import event_of
        ticker, price = s["ticker"], s["price_cents"]
        event = signal_event(s)
        if ticker in held or ticker in session_seen:
            continue
        # one bet per event, ever — never the other side of a match we've
        # already touched. Check the live snapshot AND this session's
        # durable memory (the snapshot can miss an in-flight maker order).
        if event in held_events or event in session_events \
                or event_of(ticker) in held_events \
                or event_of(ticker) in session_events:
            log.info("SKIP %s: already hold/placed a side of event %s",
                     ticker, event)
            continue
        pct = copy_pct(s.get("wallets", strategy_smartmoney.MIN_WALLETS))
        # flywheel: tilt size by the backers' proven record (bounded; the
        # scaled hard caps below are still the final clamp)
        budget = bankroll * pct / 100.0 * s.get("quality_mult", 1.0)
        count = contracts_for(budget, price)
        if count < 1:
            log.info("SKIP %s: %.0f%% of bankroll ($%.2f) buys no contract "
                     "at %.0fc", ticker, pct, budget, price)
            continue
        notional = count * price / 100.0
        problems = check_order(settings, "BUY", price / 100.0, count,
                               exposure)
        if problems:
            for p in problems:
                log.warning("BLOCKED %s: %s", ticker, p)
            continue
        log.info("COPY: buy %s %d x %s @ %.0fc ($%.2f, %.0f%% conviction) "
                 "| %s", s["side"], count, ticker, price, notional, pct,
                 s["subtitle"])
        try:
            strategy_smartmoney.log_copy_wallets(
                ticker, s["side"], price, s.get("wallet_ids", []))
        except Exception as exc:
            log.warning("Wallet attribution log failed: %s", exc)
        if settings.dry_run:
            log.info("DRY_RUN: copy not sent.")
            session_seen.add(ticker)
            session_events.add(event)
            session_events.add(event_of(ticker))
            continue
        from auto_trade import maker_price
        placed_price = maker_price(price, "smartmoney")
        try:
            order = client.create_limit_order(ticker, s["side"], "buy",
                                              count, placed_price)
        except Exception as exc:
            log.error("Copy order failed for %s: %s", ticker, exc)
            continue
        try:
            oid = str(order.get("order_id", ""))
            log_execution("smartmoney", ticker, s["side"], count,
                          placed_price, oid)
            # copy-only ledger too, WITH the confidence behind the pick
            from ledger import log_copy_execution
            log_copy_execution(ticker, s["side"], count, placed_price, oid,
                               s.get("model_prob", 0.0),
                               s.get("wallets", 0),
                               s.get("conviction", 0.0))
        except Exception as exc:
            log.warning("Execution-log write failed: %s", exc)
        session_seen.add(ticker)
        held_events.add(event_of(ticker))
        session_events.add(event)
        session_events.add(event_of(ticker))
        try:                              # remember the poly-link for exits
            strategy_smartmoney.record_open_copy(
                ticker, s.get("poly_slug", ""), s.get("poly_outcome", ""),
                s.get("wallet_ids", []), s["side"], count)
        except Exception as exc:
            log.warning("Open-copy record failed for %s: %s", ticker, exc)
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
    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path,
                          settings.kalshi_env)
    log.info("SMART-MONEY COPY RUNNER: env=%s DRY_RUN=%s sizing %."
             "0f-%.0f%% of bankroll, poll %ds",
             settings.kalshi_env, settings.dry_run, COPY_MIN_PCT,
             COPY_MAX_PCT, POLL_SECONDS)
    if not settings.dry_run:
        log.warning("LIVE: real-money copies this session.")

    session_seen = set()
    session_events = set()
    once = "--once" in sys.argv
    deadline = time.time() + RUN_MINUTES * 60
    total = 0
    while True:
        try:
            total += copy_pass(client, settings, session_seen,
                               session_events)
        except Exception as exc:
            log.error("Copy pass failed: %s — retrying next poll", exc)
        if once or time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    log.info("Session done: %d cop%s placed.", total,
             "y" if total == 1 else "ies")
    # the copier owns the copy scoreboard end-to-end: score settled copies
    # and rebuild COPY_SCOREBOARD.md (single writer -> no workflow conflict)
    try:
        from ledger import COPY_LOG
        from strategy_weather import score_pending_paper_trades
        score_pending_paper_trades(COPY_LOG)
        import copy_scoreboard
        copy_scoreboard.build()
        log.info("COPY_SCOREBOARD.md refreshed.")
    except Exception as exc:
        log.warning("Copy scoreboard refresh failed: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
