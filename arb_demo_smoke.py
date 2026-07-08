"""Demo smoke test for the arb plumbing against Kalshi DEMO (play money).

Proves the whole chain end-to-end without risking a fill:
  1. auth + signed balance read
  2. positions read (portfolio auth)
  3. arb scan runs against demo markets
  4. order lifecycle: place ONE resting limit far below the market (so it can
     NOT fill), confirm an order id came back, then cancel it.

This is the mechanics check demo exists for — demo has no real arbs, so what we
validate is that orders place, leg, and cancel correctly before any real money.

    KALSHI_ENV=demo python arb_demo_smoke.py
"""

import os
import sys

import kalshi_arb
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("arb_demo_smoke")

REST_PRICE_CENTS = 2      # a 2c YES bid rests harmlessly below any real market


def _find_market(client) -> str:
    """First open market ticker across the arb series (for the order test)."""
    for s in kalshi_arb.ARB_SERIES:
        try:
            data = client._request(
                "GET", "/events",
                params={"series_ticker": s, "status": "open",
                        "with_nested_markets": "true", "limit": 5})
        except Exception:
            continue
        for event in data.get("events", []):
            for m in event.get("markets") or []:
                if m.get("ticker") and m.get("status") in (None, "active", "open"):
                    return m["ticker"]
    return ""


def main() -> int:
    setup_logging()
    env = os.getenv("KALSHI_ENV", "demo")
    if env != "demo":
        log.error("Refusing to run the smoke test outside demo (KALSHI_ENV=%s). "
                  "It places a live test order.", env)
        return 1
    client = KalshiClient(os.getenv("KALSHI_API_KEY_ID"),
                          os.getenv("KALSHI_PRIVATE_KEY_PATH"), env)

    # 1. auth + balance
    try:
        bal = client.get_balance_cents()
    except Exception as exc:
        log.error("AUTH/BALANCE FAILED: %s", exc)
        return 1
    log.info("✓ auth OK — demo balance $%.2f", bal / 100.0)

    # 2. positions
    try:
        pos = client.get_positions().get("market_positions", [])
        log.info("✓ positions read OK (%d open)", len(pos))
    except Exception as exc:
        log.error("POSITIONS READ FAILED: %s", exc)
        return 1

    # 3. scan
    try:
        arbs = kalshi_arb.scan(client)
        log.info("✓ arb scan OK — %d basket(s) on demo (0 is expected here)",
                 len(arbs))
    except Exception as exc:
        log.error("SCAN FAILED: %s", exc)
        return 1

    # 4. order place -> cancel
    ticker = _find_market(client)
    if not ticker:
        log.warning("No demo market found to test order placement — auth/scan "
                    "validated, order lifecycle SKIPPED.")
        return 0
    try:
        order = client.create_limit_order(ticker, "yes", "buy", 1,
                                          REST_PRICE_CENTS)
    except Exception as exc:
        log.error("ORDER PLACE FAILED on %s: %s", ticker, exc)
        return 1
    oid = (order.get("order_id")
           or (order.get("order") or {}).get("order_id"))
    log.info("✓ placed resting test order %s @ %dc on %s", oid,
             REST_PRICE_CENTS, ticker)
    if not oid:
        log.error("No order id returned (response: %s) — cannot cancel.",
                  str(order)[:200])
        return 1
    try:
        client.cancel_order(oid)
        log.info("✓ cancelled test order OK")
    except Exception as exc:
        log.error("CANCEL FAILED for %s: %s — CANCEL IT MANUALLY on demo.",
                  oid, exc)
        return 1

    log.info("ALL DEMO CHECKS PASSED ✅  auth, positions, scan, place, cancel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
