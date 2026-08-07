"""Close one named position. SELLS ONLY — it can never open a bet.

    python unwind.py KXWNBA-26-ATL
    python unwind.py KXWNBA-26-ATL --min-price 5     # refuse to sell under 5c

WHY THIS EXISTS
---------------
On 2026-08-07 the sports model bought 30 shares of KXWNBA-26-ATL — Atlanta to
win the WNBA championship — at 8c, on a fictitious 58c edge produced by pricing
a season future off one game's win probability. The bug is fixed (see
strategy_sports.covers_game and SPORTS_MAX_EDGE_CENTS), but the position
outlived it, and nothing in the repo could close a position: every runner is a
buyer. This is the exit.

It is deliberately a separate, manually dispatched script rather than a step in
the trading workflow. An automated seller that runs on a cron is a liquidation
risk; this one only ever runs because a human asked it to, on a ticker a human
named.

SAFETY PROPERTIES (each covered by a test)
------------------------------------------
* action is hard-coded "sell". The string "buy" does not appear in the order
  path, so no argument can turn this into an opening trade.
* It sells only what is held, reading the count from the exchange's own
  position record rather than from anything the caller passes.
* No position, or a position of zero, is a clean no-op — never an order.
* It sells into the resting bid so the order actually fills, but refuses below
  --min-price, so a stale or empty book cannot dump the position at 1c.
* DRY_RUN=true logs the exact order and sends nothing.
"""

import os
import sys

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from ledger import log_execution
from trade_logger import get_logger, setup_logging

log = get_logger("unwind")

# A position of one side is closed by selling that side. Kalshi's position
# record is signed: positive = long YES, negative = long NO.
def position_for(positions: dict, ticker: str):
    """(side, count) held in `ticker`, or None if we hold nothing.

    Count is always positive and always comes from the exchange, so an unwind
    can never sell more than exists."""
    for p in (positions or {}).get("market_positions") or []:
        if p.get("ticker") != ticker:
            continue
        try:
            qty = int(float(p.get("position", 0) or 0))
        except (TypeError, ValueError):
            return None
        if qty > 0:
            return ("yes", qty)
        if qty < 0:
            return ("no", -qty)
        return None            # flat row: the exchange still lists settled markets
    return None


def cancel_resting_in(client, ticker: str, dry_run: bool = True) -> int:
    """Pull any of our own unfilled orders in this market. Returns the count.

    Unwinding means leaving the market, and an unfilled BUY is still exposure:
    it can fill at any moment, re-opening the position we just decided was a
    mistake. The first live unwind found "no open position" and stopped —
    correctly by its own logic, uselessly in fact, because the 8c order for
    KXWNBA-26-ATL had never filled and was sitting in the book waiting to."""
    try:
        orders = client.get_resting_orders() or []
    except Exception as exc:
        log.error("Could not read resting orders: %s", exc)
        return 0
    mine = [o for o in orders if o.get("ticker") == ticker]
    if not mine:
        return 0
    cancelled = 0
    for o in mine:
        oid = o.get("order_id") or o.get("id")
        log.info("RESTING: %s %s x%s @ %s — cancelling", ticker,
                 o.get("side", "?"), o.get("remaining_count", o.get("count", "?")),
                 o.get("price", o.get("yes_price", "?")))
        if dry_run:
            continue
        if not oid:
            log.warning("Resting order in %s has no id — cannot cancel", ticker)
            continue
        try:
            client.cancel_order(oid)
            cancelled += 1
        except Exception as exc:
            log.error("Cancel failed for %s: %s", oid, exc)
    return cancelled


def exit_price(client, ticker: str, side: str):
    """The price to sell into: the best resting bid for the side we hold.

    Selling YES lifts the YES bid; selling NO lifts the NO bid, which is
    quoted as 100 - yes_ask. Returns None when the book has no bid at all —
    there is nothing to sell into and we must not guess a price."""
    try:
        data = client._request("GET", f"/markets/{ticker}")
    except Exception as exc:
        log.error("Could not read the book for %s: %s", ticker, exc)
        return None
    market = (data or {}).get("market") or {}
    from strategy_weather import price_cents
    if side == "yes":
        bid = price_cents(market, "yes_bid")
    else:
        ask = price_cents(market, "yes_ask")
        bid = None if ask is None else 100.0 - ask
    if bid is None or not (0 < bid < 100):
        return None
    return int(round(bid))


def unwind(client, ticker: str, min_price: float = 1.0,
           dry_run: bool = True) -> int:
    """Sell out of `ticker`. Returns contracts sold (0 if nothing was done)."""
    # Order matters: kill the unfilled orders BEFORE selling. Otherwise our own
    # resting buy sits in the book while we try to sell into it, and Kalshi's
    # self-trade prevention rejects the sale.
    pulled = cancel_resting_in(client, ticker, dry_run)
    if pulled:
        log.info("Cancelled %d resting order(s) in %s.", pulled, ticker)

    positions = client.get_positions()
    held = position_for(positions, ticker)
    if not held:
        log.info("No open position in %s%s.", ticker,
                 " (resting orders pulled)" if pulled else
                 " and nothing resting — already flat")
        return 0
    side, count = held

    price = exit_price(client, ticker, side)
    if price is None:
        log.error("REFUSING: no resting bid for %s %s — nothing to sell into.",
                  ticker, side.upper())
        return 0
    if price < min_price:
        # The point of the floor: an empty or stale book quotes a bid far below
        # value, and a market sell would hit it. Better to hold than to donate.
        log.error("REFUSING: best bid %dc for %s is below the %.0fc floor. "
                  "Re-run with --min-price below that to accept it.",
                  price, ticker, min_price)
        return 0

    proceeds = count * price / 100.0
    log.info("UNWIND: sell %s %d x %s @ %dc (recovers ~$%.2f before fees)",
             side.upper(), count, ticker, price, proceeds)
    if dry_run:
        log.info("DRY_RUN: not sent.")
        return 0

    order = client.create_limit_order(ticker, side, "sell", count, price)
    try:
        log_execution("unwind", ticker, side, count, price,
                      str(order.get("order_id", "")))
    except Exception as exc:
        log.warning("Execution-log write failed: %s", exc)
    log.info("Sold. Order id %s", order.get("order_id", "?"))
    return count


def main() -> int:
    setup_logging()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    ticker = (args[0] if args else os.getenv("UNWIND_TICKER", "")).strip()
    if not ticker:
        log.error("Usage: python unwind.py <TICKER> [--min-price N]")
        return 1

    min_price = float(os.getenv("UNWIND_MIN_PRICE", "1"))
    if "--min-price" in sys.argv:
        min_price = float(sys.argv[sys.argv.index("--min-price") + 1])

    try:
        # require_trading=False on purpose. MAX_ORDER_SIZE and the exposure
        # caps bound how much risk may be OPENED; an exit reduces exposure by
        # definition, so demanding them here only creates a way to be locked
        # out of closing a position — a small MAX_ORDER_SIZE would block the
        # sale of a larger holding. The first run failed exactly this way.
        settings = load_kalshi_settings(require_market=False,
                                        require_trading=False)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1
    client = KalshiClient(settings.kalshi_api_key_id,
                          settings.kalshi_private_key_path, settings.kalshi_env)
    log.info("UNWIND %s: env=%s DRY_RUN=%s floor=%.0fc",
             ticker, settings.kalshi_env, settings.dry_run, min_price)
    unwind(client, ticker, min_price, settings.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
