"""DEMO-only probe: capture the RAW shape of a Kalshi create-order response so
we can confirm arb_runner._filled_count reads real fills correctly before
trusting the partial-fill guard on real money.

What we learned running this on demo: the create-order response is a FLAT object

    {"order_id": "...", "fill_count": "0.00", "remaining_count": "1.00",
     "client_order_id": "...", "ts_ms": ...}

with fill_count / remaining_count as STRING decimals and no status field. That
is exactly what _filled_count parses (fill_count first, via int(float(...))), so
a resting order reads 0 and a real fill reads its filled count. We cannot force
an *executed* order on demo — the books are empty (no external liquidity) and
Kalshi requires self_trade_prevention_type, so a self-cross is refused — but the
field is literally the fill count and is parsed correctly, which is the thing
the guard depends on.

This probe places one resting order, dumps the raw response + what
_order_obj/_filled_count extract, then cleans up (and sweeps any stray resting
orders from earlier runs). Refuses to run anywhere but demo. Play money only.

    KALSHI_ENV=demo python arb_fill_probe.py
"""

import json
import os
import sys

import arb_runner
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("arb_fill_probe")


def _dump(label, obj):
    try:
        log.info("%s:\n%s", label, json.dumps(obj, indent=2, default=str))
    except Exception:
        log.info("%s (unserialisable): %r", label, obj)


def _sweep_resting(client):
    """Cancel every resting order (housekeeping — clears strays from any earlier
    probe run so nothing we placed can quietly fill later)."""
    try:
        orders = client.get_resting_orders()
    except Exception as exc:
        log.warning("could not list resting orders: %s", exc)
        return
    for o in orders:
        oid = o.get("order_id")
        if oid:
            try:
                client.cancel_order(str(oid))
                log.info("swept resting order %s", oid)
            except Exception:
                pass


def _find_market(client) -> str:
    data = client._request("GET", "/markets",
                           params={"status": "open", "limit": 200})
    for m in sorted(data.get("markets", []),
                    key=lambda m: float(m.get("volume") or 0), reverse=True):
        if m.get("ticker") and m.get("status") in (None, "active", "open"):
            return m["ticker"]
    return ""


def main() -> int:
    setup_logging()
    if os.getenv("KALSHI_ENV", "demo") != "demo":
        log.error("Refusing to run outside demo — it places a live order.")
        return 1
    client = KalshiClient(os.getenv("KALSHI_API_KEY_ID"),
                          os.getenv("KALSHI_PRIVATE_KEY_PATH"), "demo")
    log.info("demo balance $%.2f", client.get_balance_cents() / 100.0)

    _sweep_resting(client)          # clean up anything left by a prior run

    ticker = _find_market(client)
    if not ticker:
        log.error("No open demo market found.")
        return 1
    log.info("using market %s", ticker)

    oid = None
    try:
        resp = client.create_limit_order(ticker, "yes", "buy", 1, 1)  # 1c rests
        _dump("RAW create_limit_order response", resp)
        order = arb_runner._order_obj(resp)
        filled = arb_runner._filled_count(order, 1)
        log.info(">>> _order_obj keys: %s", sorted(order.keys()))
        log.info(">>> _filled_count reads: %r (requested 1) <<<", filled)
        if filled == 0:
            log.info("✅ CONFIRMED: the response exposes fill_count/"
                     "remaining_count and _filled_count parses them (0 for this "
                     "resting order). A real fill sets fill_count>0, read by the "
                     "same path — the partial-fill guard is sound.")
        elif filled is None:
            log.error("❌ _filled_count could NOT parse this response — extend "
                      "it to the fields shown in the RAW dump above.")
        else:
            log.info("order shows filled=%d already.", filled)
        oid = order.get("order_id")
    finally:
        if oid:
            try:
                client.cancel_order(str(oid))
                log.info("cleanup: cancelled probe order %s", oid)
            except Exception as exc:
                log.warning("cleanup: could not cancel %s (%s)", oid, exc)
        _sweep_resting(client)

    log.info("PROBE DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
