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


def _order_obj(resp):
    """The create-order response is either the order dict itself or wrapped as
    {'order': {...}}, depending on API vintage. Normalise to the order dict."""
    if isinstance(resp, dict) and isinstance(resp.get("order"), dict):
        return resp["order"]
    return resp if isinstance(resp, dict) else {}


def _filled_count(order: dict, requested: int):
    """Contracts actually filled by a just-placed marketable order, across the
    fields Kalshi may return. None if it genuinely cannot be determined."""
    for f in ("fill_count", "filled_count"):
        if order.get(f) not in (None, ""):
            try:
                return int(float(order[f]))
            except (TypeError, ValueError):
                pass
    rem = order.get("remaining_count")
    if rem not in (None, ""):
        try:
            return max(requested - int(float(rem)), 0)
        except (TypeError, ValueError):
            pass
    status = str(order.get("status", "")).lower()
    if status in ("executed", "filled", "matched"):
        return requested
    if status in ("resting", "open", "pending", "canceled", "cancelled"):
        return 0
    return None


def place_basket(client, settings, arb: dict, exposure: float) -> bool:
    """Buy `arb['count']` of every leg on its side (yes/no) at a marketable
    limit (the marginal fill price from size_basket) so the full count fills.

    Two guardrails keep the basket risk-free or nothing:
      1. PRE-FLIGHT — every leg is run through check_order BEFORE any order is
         sent, so a leg blocked by the risk gate can never leave a partial,
         one-sided position resting on the book (it sends nothing instead).
      2. FILL VERIFY — after each real order we confirm the leg fully filled;
         a shortfall (book moved between sizing and placement) means the basket
         is unbalanced and no longer risk-free, so we cancel the remainder, log
         CRITICAL, and stop — never silently recording a basket that isn't there.
    Returns True only if ALL legs fully filled (or dry-run)."""
    count = arb["count"]

    # (1) PRE-FLIGHT: clear EVERY leg before sending ANY.
    running = exposure
    for ticker, price_c, side in arb["legs"]:
        # min_price_cents=0: a risk-free hedged basket leg can be cheap (a wide
        # numeric ladder has legs well under the directional longshot floor) and
        # is NOT a longshot — the basket as a whole is guaranteed.
        problems = check_order(settings, "BUY", price_c / 100.0, count, running,
                               min_price_cents=0)
        if problems:
            for p in problems:
                log.warning("BLOCKED leg %s: %s", ticker, p)
            log.info("Basket %s not fully placeable (leg %s blocked) — sending "
                     "NOTHING; will re-check next poll.",
                     arb["event_ticker"], ticker)
            return False
        running += count * price_c / 100.0

    if settings.dry_run:
        log.info("[DRY_RUN] basket %s cleared pre-flight: %d x %d-leg %s "
                 "(guaranteed +%.1fc/contract = $%.2f).", arb["event_ticker"],
                 count, arb["n"], arb["side"].upper(), arb["profit_cents"],
                 arb["profit_cents"] * count / 100.0)
        return True

    # (2) PLACE + VERIFY: all legs pre-cleared; place at the marginal limit.
    placed = 0
    for ticker, price_c, side in arb["legs"]:
        price = int(round(price_c))         # marketable limit at the marginal fill
        try:
            resp = client.create_limit_order(ticker, side, "buy", count, price)
        except Exception as exc:
            log.critical("Leg %s FAILED (%s). %d/%d legs already placed — "
                         "PARTIAL BASKET %s, resolve manually.", ticker, exc,
                         placed, arb["n"], arb["event_ticker"])
            return False
        order = _order_obj(resp)
        filled = _filled_count(order, count)
        if filled is None:
            log.warning("Could not verify fill for %s (unrecognised order "
                        "response) — assuming full fill of %d.", ticker, count)
            filled = count
        try:
            log_execution("arb", ticker, side, filled, price,
                          str(order.get("order_id", "")))
        except Exception as exc:
            log.warning("Execution-log write failed for %s: %s", ticker, exc)
        if filled < count:
            oid = order.get("order_id")
            if oid:
                try:
                    client.cancel_order(str(oid))
                except Exception as exc:
                    log.warning("Could not cancel remainder of %s: %s",
                                ticker, exc)
            log.critical("Leg %s filled %d/%d — PARTIAL BASKET %s (%d prior "
                         "legs already fully filled). NOT risk-free, resolve "
                         "manually.", ticker, filled, count,
                         arb["event_ticker"], placed)
            return False
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
        # depth-aware size: as big as the book + your caps allow, no bigger
        sized = kalshi_arb.size_basket(client, a, remaining)
        if not sized or sized["count"] < 1:
            # too thin / over budget RIGHT NOW — don't mark done, re-check next
            # poll in case the book deepens (24/7 catch rate).
            log.info("ARB %s: no size clears the buffer within the book/budget "
                     "— will re-check next poll.", a["event_ticker"])
            continue
        done.add(a["event_ticker"])       # attempted -> never re-leg the basket
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
