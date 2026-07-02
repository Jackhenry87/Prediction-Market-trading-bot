"""Kalshi Phase 1: authenticate, show balance, and print the live order
book for the market ticker in .env (MARKET_TICKER).

Read-only. Places NO orders. Runs once and exits.

    python kalshi_fetch_orderbook.py
"""

import sys

from config import ConfigError, load_kalshi_settings
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("kalshi_fetch")


def print_orderbook(book: dict, ticker: str, depth: int = 10) -> None:
    """Kalshi returns resting YES bids and NO bids (in cents). A NO bid at
    price p is equivalent to someone offering YES at 100-p, so we display
    the NO side as the YES ask column."""
    yes_bids = sorted(book.get("yes") or [], key=lambda x: -x[0])
    yes_asks = sorted(
        [[100 - price, count] for price, count in (book.get("no") or [])],
        key=lambda x: x[0],
    )

    best_bid = yes_bids[0][0] if yes_bids else None
    best_ask = yes_asks[0][0] if yes_asks else None
    log.info(
        "Order book for %s: %d bid levels, %d ask levels | best bid=%s¢ "
        "best ask=%s¢ spread=%s¢",
        ticker, len(yes_bids), len(yes_asks), best_bid, best_ask,
        (best_ask - best_bid) if yes_bids and yes_asks else "n/a",
    )

    print(f"\n=== {ticker} (prices in cents per YES contract) ===")
    print(f"{'BID qty':>10} {'BID':>5} | {'ASK':<5} {'ASK qty':<10}")
    print("-" * 38)
    for i in range(max(min(depth, max(len(yes_bids), len(yes_asks))), 1)):
        bid = yes_bids[i] if i < len(yes_bids) else None
        ask = yes_asks[i] if i < len(yes_asks) else None
        bid_str = f"{bid[1]:>10,} {bid[0]:>4}¢" if bid else " " * 16
        ask_str = f"{ask[0]:<4}¢ {ask[1]:<10,}" if ask else ""
        print(f"{bid_str} | {ask_str}")
    if best_bid is not None and best_ask is not None:
        print("-" * 38)
        print(f"best bid {best_bid}¢ | best ask {best_ask}¢ | "
              f"mid {(best_bid + best_ask) / 2:.1f}¢")
    print()


def main() -> int:
    setup_logging()
    try:
        settings = load_kalshi_settings(require_market=True)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    log.info(
        "Run mode: env=%s DRY_RUN=%s KILL_SWITCH=%s (read-only script)",
        settings.kalshi_env, settings.dry_run, settings.kill_switch,
    )
    if settings.kalshi_env == "prod":
        log.warning("Connected to PRODUCTION Kalshi (real money account).")

    try:
        client = KalshiClient(
            settings.kalshi_api_key_id,
            settings.kalshi_private_key_path,
            settings.kalshi_env,
        )
        balance = client.get_balance_cents()
        log.info("Authenticated to Kalshi (%s). Balance: $%.2f",
                 settings.kalshi_env, balance / 100)

        log.info("Looking up market ticker %s ...", settings.market_ticker)
        try:
            market = client.get_market(settings.market_ticker)
        except Exception:
            # Maybe it's an EVENT ticker (groups several markets) — list them.
            event = None
            try:
                event = client.get_event(settings.market_ticker)
            except Exception:
                pass
            markets = (event or {}).get("markets") or \
                (event or {}).get("event", {}).get("markets") or []
            if markets:
                log.warning(
                    "%s is an EVENT ticker. Pick ONE of its markets and put "
                    "that in MARKET_TICKER:", settings.market_ticker,
                )
                for m in markets:
                    print(f"  {m.get('ticker')}  —  "
                          f"{m.get('subtitle') or m.get('yes_sub_title') or m.get('title')}")
                return 1
            log.error(
                "No market or event found for ticker %r. Copy the ticker "
                "exactly as shown on the Kalshi market page.",
                settings.market_ticker,
            )
            return 1

        log.info("Market: %s — status=%s", market.get("title"),
                 market.get("status"))

        book = client.get_orderbook(settings.market_ticker)
        print_orderbook(book, settings.market_ticker)
    except Exception as exc:
        log.error("Failed: %s", exc)
        return 1

    log.info("Done. Exiting (no loop, no orders).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
