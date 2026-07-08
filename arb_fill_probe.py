"""DEMO-only probe: capture the RAW shape of a Kalshi create-order response for
a MARKETABLE order that actually fills, so we can confirm arb_runner._filled_count
reads real fills correctly before trusting the fill check on real money.

The arb runner's partial-fill guard depends on parsing fill_count / remaining_count
/ status out of whatever POST /portfolio/events/orders returns. The existing smoke
test only ever places a RESTING order, so it never exercises the filled path. This
probe deliberately places a small marketable buy (1 contract) into a market that
has a hittable ask, dumps the entire JSON response, shows what _order_obj /
_filled_count extract from it, then cleans up (flatten + cancel).

Refuses to run anywhere but demo. Play money only.

    KALSHI_ENV=demo python arb_fill_probe.py
"""

import json
import os
import sys

import arb_runner
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("arb_fill_probe")

PROBE_COUNT = 1            # one contract — cents of play money


def _dump(label, obj):
    try:
        log.info("%s:\n%s", label, json.dumps(obj, indent=2, default=str))
    except Exception:
        log.info("%s (unserialisable): %r", label, obj)


def _find_hittable(client):
    """A market with resting YES asks we can actually take, cheapest first so
    the probe spends almost nothing. Returns (ticker, yes_ask_cents) or None."""
    try:
        data = client._request("GET", "/markets",
                               params={"status": "open", "limit": 200})
    except Exception as exc:
        log.error("markets list failed: %s", exc)
        return None
    markets = data.get("markets", [])
    # highest-volume first — most likely to have a live two-sided book on demo
    markets.sort(key=lambda m: float(m.get("volume") or 0), reverse=True)
    checked = 0
    for m in markets:
        ticker = m.get("ticker")
        if not ticker or m.get("status") not in (None, "active", "open"):
            continue
        try:
            book = client.get_orderbook(ticker)
        except Exception:
            continue
        checked += 1
        # buying YES matches resting NO orders (yes_ask = 100 - no_price)
        lad = arb_runner.kalshi_arb._ladder_for_buy(book, "yes")
        if lad:
            price, qty = lad[0]
            if 1 <= price <= 98 and qty >= PROBE_COUNT:
                log.info("Hittable market %s: %d YES available @ %dc",
                         ticker, int(qty), int(round(price)))
                return ticker, int(round(price))
        if checked >= 60:
            break
    return None


def main() -> int:
    setup_logging()
    env = os.getenv("KALSHI_ENV", "demo")
    if env != "demo":
        log.error("Refusing to run outside demo (KALSHI_ENV=%s) — it places a "
                  "live marketable order.", env)
        return 1
    client = KalshiClient(os.getenv("KALSHI_API_KEY_ID"),
                          os.getenv("KALSHI_PRIVATE_KEY_PATH"), env)

    log.info("demo balance $%.2f", client.get_balance_cents() / 100.0)

    found = _find_hittable(client)
    if not found:
        log.warning("No hittable ask found on demo right now — cannot force a "
                    "fill. (Demo books are often empty.) Try again later.")
        return 0
    ticker, ask = found

    order_id = None
    try:
        # marketable: limit AT the ask so the resting size fills immediately
        resp = client.create_limit_order(ticker, "yes", "buy", PROBE_COUNT, ask)
        _dump("RAW create_limit_order response", resp)

        order = arb_runner._order_obj(resp)
        _dump("_order_obj(resp)", order)
        filled = arb_runner._filled_count(order, PROBE_COUNT)
        log.info(">>> _filled_count reads: %r (requested %d) <<<",
                 filled, PROBE_COUNT)
        if filled == PROBE_COUNT:
            log.info("✅ parser sees a FULL fill — the partial-fill guard is "
                     "reading real fills correctly.")
        elif filled == 0:
            log.warning("parser sees 0 filled — order likely RESTED (no real "
                        "depth). Envelope still captured above.")
        elif filled is None:
            log.error("❌ parser could NOT determine fill from this response — "
                      "_filled_count needs the fields shown in the RAW dump.")
        else:
            log.info("parser sees PARTIAL fill %d/%d.", filled, PROBE_COUNT)

        order_id = order.get("order_id")

        # cross-check against the authoritative endpoints + capture their shapes
        try:
            _dump("get_fills() [most recent 3]", client.get_fills()[-3:])
        except Exception as exc:
            log.warning("get_fills failed: %s", exc)
        try:
            _dump("get_resting_orders() [up to 3]",
                  client.get_resting_orders()[:3])
        except Exception as exc:
            log.warning("get_resting_orders failed: %s", exc)
    finally:
        # cleanup: cancel any resting remainder, then flatten any position taken
        if order_id:
            try:
                client.cancel_order(str(order_id))
                log.info("cleanup: cancelled order %s", order_id)
            except Exception as exc:
                log.info("cleanup: nothing to cancel for %s (%s)", order_id, exc)
        try:
            pos = {p.get("ticker"): p for p in
                   client.get_positions().get("market_positions", [])}
            held = int(float(pos.get(ticker, {}).get("position", 0) or 0))
            if held:
                # sell what we bought back at 1c (marketable down) to go flat
                side = "yes" if held > 0 else "no"
                client.create_limit_order(ticker, side, "sell", abs(held), 1)
                log.info("cleanup: flattened %d %s on %s", abs(held),
                         side.upper(), ticker)
        except Exception as exc:
            log.warning("cleanup: could not flatten %s (%s) — check demo "
                        "manually (play money).", ticker, exc)

    log.info("PROBE DONE — read the RAW dump above to confirm the fill fields.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
