"""Phase 1: authenticate to the Polymarket CLOB and print the live order
book for the single market token in .env (MARKET_TOKEN_ID).

Read-only. Places NO orders. Runs once and exits.

Usage:
    python fetch_orderbook.py
"""

import sys

from py_clob_client.client import ClobClient

from config import ConfigError, load_settings
from trade_logger import get_logger, setup_logging

log = get_logger("fetch_orderbook")


def build_client(settings) -> ClobClient:
    """Create an authenticated CLOB client (L1 wallet auth + derived L2 API creds)."""
    kwargs = {
        "key": settings.private_key,
        "chain_id": settings.chain_id,
    }
    if settings.signature_type in (1, 2):
        kwargs["signature_type"] = settings.signature_type
        kwargs["funder"] = settings.funder_address

    client = ClobClient(settings.clob_api_url, **kwargs)

    # Derives (or creates on first run) L2 API credentials by signing with the
    # wallet key. Proves our authentication works end to end.
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)
    log.info(
        "Authenticated to CLOB at %s as wallet %s (signature_type=%s)",
        settings.clob_api_url,
        client.get_address(),
        settings.signature_type,
    )
    return client


def print_order_book(client: ClobClient, token_id: str, depth: int = 10) -> None:
    log.info("Fetching order book for token_id=%s", token_id)
    book = client.get_order_book(token_id)

    # API returns price/size as strings; sort best-first for display.
    bids = sorted(book.bids, key=lambda o: float(o.price), reverse=True)
    asks = sorted(book.asks, key=lambda o: float(o.price))

    best_bid = float(bids[0].price) if bids else None
    best_ask = float(asks[0].price) if asks else None
    log.info(
        "Order book received: %d bid levels, %d ask levels | best bid=%s best ask=%s spread=%s",
        len(bids),
        len(asks),
        best_bid,
        best_ask,
        round(best_ask - best_bid, 4) if bids and asks else "n/a",
    )

    print(f"\n=== Order book for token {token_id} ===")
    print(f"{'BID size':>12} {'BID price':>10} | {'ASK price':<10} {'ASK size':<12}")
    print("-" * 50)
    for i in range(max(min(depth, max(len(bids), len(asks))), 1)):
        bid = bids[i] if i < len(bids) else None
        ask = asks[i] if i < len(asks) else None
        bid_str = f"{float(bid.size):>12,.2f} {float(bid.price):>10.3f}" if bid else " " * 23
        ask_str = f"{float(ask.price):<10.3f} {float(ask.size):<12,.2f}" if ask else ""
        print(f"{bid_str} | {ask_str}")
    if best_bid is not None and best_ask is not None:
        mid = (best_bid + best_ask) / 2
        print("-" * 50)
        print(f"best bid {best_bid:.3f} | best ask {best_ask:.3f} | mid {mid:.3f}")
    print()


def main() -> int:
    setup_logging()
    try:
        settings = load_settings(require_market=True)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 1

    log.info(
        "Run mode: DRY_RUN=%s KILL_SWITCH=%s MAX_ORDER_SIZE=%s MAX_TOTAL_EXPOSURE=%s "
        "(Phase 1 is read-only; no order path exists yet)",
        settings.dry_run,
        settings.kill_switch,
        settings.max_order_size,
        settings.max_total_exposure,
    )

    try:
        client = build_client(settings)
        print_order_book(client, settings.market_token_id)
    except Exception as exc:  # surface API/auth failures in the log, exit nonzero
        log.error("Failed to fetch order book: %s", exc)
        return 1

    log.info("Done. Exiting (no loop, no orders).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
