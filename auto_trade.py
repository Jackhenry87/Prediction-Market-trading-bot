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

import math
import sys

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from kalshi_exposure import (ExposureError, _position_exposure_cents,
                             current_exposure_usd)
from ledger import apply_price_band, log_execution, log_signals
from safety import check_order
import strategy_commodities
import strategy_crypto
import strategy_macro
import strategy_smartmoney
import strategy_sports
import strategy_weather
from strategy_weather import scan, score_pending_paper_trades, SIGMA_F
from trade_logger import get_logger, setup_logging

log = get_logger("auto_trade")


def pick_best_per_event(results: list) -> list:
    """One signal per event: same-event signals are the same underlying bet."""
    chosen = []
    for r in results:
        if r["signals"]:
            best = max(r["signals"], key=lambda s: s["ev_cents"])
            chosen.append(dict(best, date=r["date"], mu=r["mu"],
                               model=r.get("model", "")))
    return chosen


def held_tickers(positions: dict, resting_orders: list) -> set:
    """Markets we already have a position or resting order in — scheduled
    runs must not stack a second bet on the same market."""
    held = {p.get("ticker") for p in positions.get("market_positions", [])
            if float(p.get("position", 0) or 0) != 0}
    held |= {o.get("ticker") for o in resting_orders or []}
    held.discard(None)
    return held


def event_of(ticker: str) -> str:
    """KXHIGHNY-26JUL02-B99.5 -> KXHIGHNY-26JUL02. Two markets in one event
    are the same underlying bet and must not be held simultaneously."""
    if ticker and ticker.count("-") >= 2:
        return ticker.rsplit("-", 1)[0]
    return ticker or ""


# Ticker prefix -> theme, so we can cap correlated bets (e.g. all the weather
# markets on a heat-wave day are really ONE bet). Order matters: longest /
# most specific first.
_THEME_PREFIXES = [
    ("KXHIGH", "weather"), ("KXLOW", "weather"),
    ("KXBTC", "crypto"), ("KXETH", "crypto"),
    ("KXWTI", "commodities"), ("KXNGAS", "commodities"), ("KXGOLD", "commodities"),
    ("KXMLB", "sports"), ("KXNBA", "sports"), ("KXNFL", "sports"),
    ("KXNHL", "sports"), ("KXWNBA", "sports"),
]


def theme_of(ticker: str) -> str:
    """Which correlated theme a market belongs to (weather/crypto/sports/
    commodities), inferred from its ticker. 'other' if unrecognized."""
    t = (ticker or "").upper()
    for prefix, theme in _THEME_PREFIXES:
        if t.startswith(prefix):
            return theme
    return "other"


def theme_exposure(positions: dict) -> dict:
    """Current USD committed per theme, from open positions."""
    out = {}
    for p in positions.get("market_positions", []):
        if float(p.get("position", 0) or 0) == 0:
            continue
        cents = _position_exposure_cents(p) or 0
        out[theme_of(p.get("ticker"))] = out.get(
            theme_of(p.get("ticker")), 0.0) + cents / 100.0
    return out


def dynamic_order_caps(balance_cents: float, exposure_usd: float, settings):
    """(max_usd, min_usd) per order: MAX_ORDER_PCT / MIN_ORDER_PCT of the
    bankroll (cash + committed positions), never above the absolute
    MAX_ORDER_SIZE ceiling. Scales automatically as the account changes."""
    bankroll = balance_cents / 100.0 + exposure_usd
    max_usd = min(settings.max_order_size,
                  bankroll * settings.max_order_pct / 100.0)
    min_usd = bankroll * settings.min_order_pct / 100.0
    return max_usd, min_usd


def size_order(price_cents: float, exposure_usd: float, settings,
               max_usd: float = None) -> int:
    """Contracts purchasable within the caps. 0 = no room."""
    per_order = settings.max_order_size if max_usd is None else max_usd
    budget = min(per_order, settings.max_total_exposure - exposure_usd)
    if budget <= 0 or price_cents <= 0:
        return 0
    return int(budget * 100 // price_cents)


def manage_exits(client, settings, positions: dict, resting_orders: list) -> None:
    """Place a take-profit sell (GTC limit) on every position that doesn't
    already have a resting order: target = entry cost +TAKE_PROFIT_PCT%,
    capped at 99c. Winners get recycled into new trades instead of waiting
    for settlement."""
    if settings.kill_switch:
        log.info("KILL_SWITCH on — not placing take-profit sells.")
        return
    resting_tickers = {o.get("ticker") for o in resting_orders or []}
    for p in positions.get("market_positions", []):
        pos = float(p.get("position", 0) or 0)
        ticker = p.get("ticker")
        if pos == 0 or not ticker or ticker in resting_tickers:
            continue
        count = int(abs(pos))
        cost_cents = _position_exposure_cents(p)
        if not cost_cents:
            continue
        avg = cost_cents / count
        target = min(int(math.ceil(avg * (1 + settings.take_profit_pct / 100.0))), 99)
        if target <= avg:
            continue
        side = "yes" if pos > 0 else "no"
        log.info("TAKE-PROFIT: sell %s %d x %s @ %d¢ (entry avg %.0f¢, +%.0f%%)",
                 side, count, ticker, target, avg, settings.take_profit_pct)
        if settings.dry_run:
            log.info("DRY_RUN: sell not sent.")
            continue
        try:
            resp = client.create_limit_order(ticker, side, "sell", count, target)
            log.info("SELL PLACED: %s", resp.get("order", resp))
        except Exception as exc:
            log.error("Take-profit sell failed for %s: %s — continuing", ticker, exc)


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

    enabled = settings.enabled_models
    log.info("Enabled models: %s", ", ".join(enabled) or "(none)")

    # scan fn + ledger path, per model; sports needs the odds key
    model_defs = {
        "weather": (scan, strategy_weather.PAPER_LOG),
        "crypto": (strategy_crypto.scan, strategy_crypto.PAPER_LOG),
        "commodities": (strategy_commodities.scan,
                        strategy_commodities.PAPER_LOG),
        "sports": (lambda: strategy_sports.scan(settings.odds_api_key)
                   if settings.odds_api_key else [], strategy_sports.PAPER_LOG),
        "macro": (lambda: strategy_macro.scan(settings.fred_api_key)
                  if settings.fred_api_key else [], strategy_macro.PAPER_LOG),
        # Polymarket smart-money consensus, executed on Kalshi (Polymarket
        # geoblocks US orders). Public APIs — no key needed.
        "smartmoney": (strategy_smartmoney.scan,
                       strategy_smartmoney.PAPER_LOG),
    }

    for name, (_, path) in model_defs.items():
        if name not in enabled:
            continue
        try:
            score_pending_paper_trades(path)
        except Exception as exc:
            log.warning("%s scoring skipped (%s)", name, exc)

    # Scan each enabled model separately so signals log to the right ledger.
    per_model = []  # (results, ledger_path, model_name)
    for name, (scan_fn, path) in model_defs.items():
        if name not in enabled:
            log.info("Model '%s' is OFF (not in ENABLED_MODELS).", name)
            continue
        try:
            res = scan_fn()
            for r in res:
                r["model"] = name
            per_model.append((res, path, name))
        except Exception as exc:
            log.error("%s scan failed: %s — continuing without it", name, exc)

    # Price-band filter (your 60–90% rule): trade only mid-priced contracts.
    log.info("Trading only contracts priced %.0f–%.0f¢ (skips near-locks and "
             "longshots).", settings.trade_min_price, settings.trade_max_price)
    results = []
    for res, path, name in per_model:
        if name == "macro":
            # Known-outcome (resolution-lag) trades: the correct side is
            # ~certain to pay 100c, so buying it at 92-98c is exactly the
            # edge. Do NOT apply the 60-90c band that's meant for uncertain
            # bets — it would discard the best lag captures.
            banded = res
        else:
            banded = apply_price_band(res, settings.trade_min_price,
                                      settings.trade_max_price)
        # Record EVERY in-band signal for scoring, traded or not — this is
        # the measurement that was missing.
        try:
            n = log_signals(banded, path)
            if n:
                log.info("Logged %d new signal(s) to %s", n, path.name)
        except Exception as exc:
            log.warning("Ledger write to %s failed: %s", path.name, exc)
        results += banded

    if not results:
        log.info("No in-band signals this run. Exiting.")
        refresh_records(settings)
        return 0

    picks = pick_best_per_event(results)
    if not picks:
        log.info("No signals today — nothing to trade. Exiting.")
        refresh_records(settings)
        return 0

    try:
        client = KalshiClient(
            settings.kalshi_api_key_id,
            settings.kalshi_private_key_path,
            settings.kalshi_env,
        )
        balance = client.get_balance_cents()
        log.info("Available balance: $%.2f", balance / 100)
        exposure = current_exposure_usd(client)
        positions = client.get_positions()
        resting = client.get_resting_orders()
        already_held = held_tickers(positions, resting)
    except ExposureError as exc:
        log.error("REFUSING TO TRADE: %s (failing closed)", exc)
        return 1
    except Exception as exc:
        log.error("Could not authenticate or read positions: %s", exc)
        return 1

    manage_exits(client, settings, positions, resting)

    bankroll = balance / 100 + exposure
    max_usd, min_usd = dynamic_order_caps(balance, exposure, settings)
    log.info("Dynamic sizing: bankroll $%.2f -> per-order max $%.2f (%.0f%%), "
             "min $%.2f (%.0f%%)", bankroll, max_usd,
             settings.max_order_pct, min_usd, settings.min_order_pct)
    held_events = {event_of(t) for t in already_held}
    theme_used = theme_exposure(positions)
    theme_cap = bankroll * settings.max_theme_pct / 100.0
    log.info("Per-theme cap: $%.2f (%.0f%% of bankroll). Current: %s",
             theme_cap, settings.max_theme_pct,
             {k: round(v, 2) for k, v in theme_used.items()} or "none")

    placed = 0
    for signal in picks:
        if signal["ticker"] in already_held:
            log.info("SKIP %s: already holding a position/order there",
                     signal["ticker"])
            continue
        if event_of(signal["ticker"]) in held_events:
            log.info("SKIP %s: already holding a bet in the same event",
                     signal["ticker"])
            continue
        price = int(round(signal["price_cents"]))
        count = size_order(price, exposure, settings, max_usd)
        if count < 1:
            log.info("SKIP %s: no room under caps (exposure $%.2f)",
                     signal["ticker"], exposure)
            continue
        notional = price * count / 100.0
        if notional < min_usd:
            log.info("SKIP %s: $%.2f is below the %.0f%% bankroll minimum "
                     "($%.2f)", signal["ticker"], notional,
                     settings.min_order_pct, min_usd)
            continue
        theme = theme_of(signal["ticker"])
        if theme_used.get(theme, 0.0) + notional > theme_cap:
            log.info("SKIP %s: theme '%s' at $%.2f, +$%.2f would exceed the "
                     "$%.2f cap (%.0f%% of bankroll)", signal["ticker"], theme,
                     theme_used.get(theme, 0.0), notional, theme_cap,
                     settings.max_theme_pct)
            continue

        log.info(
            "ORDER ATTEMPT: buy %s %d x %s @ %d¢ ($%.2f) | model %.0f%% | "
            "EV +%.1fc",
            signal["side"], count, signal["ticker"], price, notional,
            100 * signal["model_prob"], signal["ev_cents"],
        )
        if notional * 100 > balance:
            log.info("SKIP %s: costs $%.2f but only $%.2f available",
                     signal["ticker"], notional, balance / 100)
            continue

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
        # Guaranteed audit trail: record every real fill immediately.
        try:
            log_execution(signal.get("model", ""), signal["ticker"],
                          signal["side"], count, price,
                          str(order.get("order_id", "")))
        except Exception as exc:
            log.warning("Execution-log write failed: %s", exc)
        exposure += notional
        balance -= notional * 100
        theme_used[theme] = theme_used.get(theme, 0.0) + notional
        held_events.add(event_of(signal["ticker"]))
        placed += 1

    log.info("Run complete: %d order(s) placed. Exiting (no loop).", placed)
    refresh_records(settings, client)
    return 0


def refresh_records(settings, client=None) -> None:
    """Make the record TRUE before rebuilding the scoreboard: pull any real
    orders the ledger missed from Kalshi's own fills (source of truth —
    CI workspaces are ephemeral), score settled executions, then rebuild.
    Runs on EVERY exit path, including no-signal runs."""
    from ledger import EXEC_LOG, reconcile_fills
    if client is None:
        try:
            client = KalshiClient(settings.kalshi_api_key_id,
                                  settings.kalshi_private_key_path,
                                  settings.kalshi_env)
        except Exception as exc:
            log.warning("Records: no authenticated client (%s)", exc)
            client = None
    if client is not None:
        try:
            added = reconcile_fills(client)
            if added:
                log.info("Reconciled %d order(s) from Kalshi fills that the "
                         "ledger had missed.", added)
        except Exception as exc:
            log.warning("Fill reconciliation failed: %s", exc)
        try:
            score_pending_paper_trades(EXEC_LOG)
        except Exception as exc:
            log.warning("Executed-trade scoring failed: %s", exc)
    try:
        import scoreboard
        scoreboard.build()
        log.info("SCOREBOARD.md refreshed.")
    except Exception as exc:
        log.warning("Scoreboard generation failed: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
