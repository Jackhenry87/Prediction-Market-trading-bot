"""Exposure parsing must keep up with Kalshi's field-name vintages. When it
can't parse an order it fails CLOSED (returns None -> caller refuses to trade),
so a new field vocabulary silently blocks ALL real-money placement. This locks
in the current vintage (remaining_count_fp + *_price_dollars)."""

from kalshi_exposure import _order_cost_cents


def test_current_vintage_fp_and_dollars():
    # the exact resting order Kalshi returned that blocked trading:
    order = {
        "action": "buy", "book_side": "bid", "side": "yes", "status": "resting",
        "remaining_count_fp": "1.00", "initial_count_fp": "1.00",
        "yes_price_dollars": "0.6500", "no_price_dollars": "0.3500",
        "ticker": "KXMLBTOTAL-26JUL102005HOUTEX-7",
    }
    # buy YES at $0.65 x 1 contract = 65 cents reserved
    assert _order_cost_cents(order) == 65.0


def test_buy_no_uses_no_price_dollars():
    order = {"action": "buy", "side": "no", "remaining_count_fp": "2.00",
             "yes_price_dollars": "0.6000", "no_price_dollars": "0.4000"}
    assert _order_cost_cents(order) == 80.0        # 40c x 2


def test_legacy_cents_vintage_still_parses():
    order = {"action": "buy", "side": "yes", "remaining_count": 3, "yes_price": 30}
    assert _order_cost_cents(order) == 90.0


def test_non_buying_order_is_zero():
    assert _order_cost_cents({"action": "sell", "side": "yes",
                              "remaining_count_fp": "5.00"}) == 0


def test_truly_unparseable_fails_closed():
    # no recognisable count field at all -> None (caller fails closed)
    assert _order_cost_cents({"action": "buy", "side": "yes",
                              "yes_price_dollars": "0.50"}) is None
