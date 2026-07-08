"""DEMO-only probe: capture the RAW shape of Kalshi order responses so we can
confirm arb_runner._filled_count reads real fills correctly before trusting the
partial-fill guard on real money.

Demo order books are empty (no external liquidity to take), so this does two
things:
  Phase 1 — place a resting order that can't fill, dump the raw create response
            AND the canonical Order object from get_resting_orders. This pins
            down the exact field names + nesting (order_id, status,
            remaining_count, ...).
  Phase 2 — deliberately SELF-CROSS (a resting YES ask via a NO buy, then a
            marketable YES buy that hits it, with self-trade-prevention omitted)
            to produce a genuinely EXECUTED order, then dump that response and
            show what _order_obj / _filled_count extract from a real fill.

Cleans up after itself (cancel resting + report any residual set). Refuses to
run anywhere but demo. Play money only.

    KALSHI_ENV=demo python arb_fill_probe.py
"""

import json
import os
import sys

import arb_runner
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("arb_fill_probe")

X = 50            # self-cross price in cents (both legs; a $1 set, ~break-even)


def _dump(label, obj):
    try:
        log.info("%s:\n%s", label, json.dumps(obj, indent=2, default=str))
    except Exception:
        log.info("%s (unserialisable): %r", label, obj)


def _report_parse(resp, requested):
    order = arb_runner._order_obj(resp)
    _dump("_order_obj(resp)", order)
    filled = arb_runner._filled_count(order, requested)
    log.info(">>> _filled_count reads: %r (requested %d) <<<", filled, requested)
    return order, filled


def _find_market(client) -> str:
    """First open, tradeable market ticker (self-cross needs no liquidity)."""
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
        log.error("Refusing to run outside demo — it places live orders.")
        return 1
    client = KalshiClient(os.getenv("KALSHI_API_KEY_ID"),
                          os.getenv("KALSHI_PRIVATE_KEY_PATH"), "demo")
    log.info("demo balance $%.2f", client.get_balance_cents() / 100.0)

    ticker = _find_market(client)
    if not ticker:
        log.error("No open demo market found.")
        return 1
    log.info("using market %s", ticker)

    to_cancel = []
    try:
        # ---- Phase 1: resting-order schema (can't fill at 1c) --------------
        log.info("=== PHASE 1: resting order (schema) ===")
        resp = client.create_limit_order(ticker, "yes", "buy", 1, 1)
        _dump("RAW create response (resting)", resp)
        order, _ = _report_parse(resp, 1)
        oid = order.get("order_id")
        if oid:
            to_cancel.append(oid)
        resting = client.get_resting_orders()
        _dump("get_resting_orders() [canonical Order objects, up to 3]",
              resting[:3])

        # ---- Phase 2: force a real EXECUTED fill via self-cross ------------
        log.info("=== PHASE 2: self-cross to force a real fill ===")
        # maker: buy NO @ (100-X) rests as a YES ask at X
        maker = client.create_limit_order(ticker, "no", "buy", 1, 100 - X)
        moid = arb_runner._order_obj(maker).get("order_id")
        if moid:
            to_cancel.append(moid)
        log.info("resting maker placed (YES ask @ %dc), order %s", X, moid)
        # taker: marketable buy YES @ X, self-trade-prevention OMITTED so it
        # actually matches our own resting ask instead of being cancelled
        taker = client.create_limit_order(ticker, "yes", "buy", 1, X,
                                           self_trade_prevention_type=None)
        _dump("RAW create response (taker — should be FILLED)", taker)
        torder, filled = _report_parse(taker, 1)
        toid = torder.get("order_id")
        if toid:
            to_cancel.append(toid)

        if filled == 1:
            log.info("✅ CONFIRMED: _filled_count reads a REAL fill correctly "
                     "(1/1). The partial-fill guard will work on real money.")
        elif filled is None:
            log.error("❌ _filled_count could NOT parse the fill — see the RAW "
                      "taker dump above and extend the parser to those fields.")
        else:
            log.warning("taker filled=%r (self-cross may have been prevented, "
                        "or fields differ) — inspect the RAW dump above.", filled)

        try:
            _dump("get_fills() [most recent 3]", client.get_fills()[-3:])
        except Exception as exc:
            log.warning("get_fills failed: %s", exc)
    finally:
        for oid in to_cancel:
            try:
                client.cancel_order(str(oid))
            except Exception:
                pass
        try:
            pos = {p.get("ticker"): int(float(p.get("position", 0) or 0))
                   for p in client.get_positions().get("market_positions", [])}
            if pos.get(ticker):
                log.info("residual demo position on %s: %d (a self-hedged set; "
                         "settles to $1, harmless play money).", ticker,
                         pos[ticker])
        except Exception:
            pass

    log.info("PROBE DONE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
