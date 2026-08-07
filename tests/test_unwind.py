"""Tests for the position exit.

This script sells real money out of real positions, so the tests that matter
are the ones pinning what it must never do: open a bet, sell more than is held,
invent a price, or fire when told not to.
"""

import unwind


class FakeClient:
    def __init__(self, positions=None, market=None, resting=None, fills=None):
        self._positions = positions or {"market_positions": []}
        self._market = market or {}
        self._resting = resting or []
        self._fills = fills or []
        self.orders = []
        self.cancelled = []

    def get_positions(self):
        return self._positions

    def get_resting_orders(self):
        return list(self._resting)

    def get_balance_cents(self):
        return 5000

    def get_fills(self, min_ts=None):
        return list(self._fills)

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"ok": True}

    def _request(self, method, path, **kw):
        return {"market": self._market}

    def create_limit_order(self, ticker, side, action, count, price):
        self.orders.append(dict(ticker=ticker, side=side, action=action,
                                count=count, price=price))
        return {"order_id": "o1"}


def _client(qty=30, yes_bid=7, yes_ask=8, ticker="KXWNBA-26-ATL", resting=None,
            fills=None):
    return FakeClient(
        positions={"market_positions": [{"ticker": ticker, "position": qty}]},
        market={"ticker": ticker, "yes_bid": yes_bid, "yes_ask": yes_ask},
        resting=resting, fills=fills)


# --- it must never be able to open a position ------------------------------

def test_module_never_buys():
    src = open(unwind.__file__).read()
    order_path = src.split("def unwind(")[1]
    assert '"buy"' not in order_path and "'buy'" not in order_path, (
        "unwind must not contain a buy path — it is an exit, not a strategy")


def test_order_action_is_always_sell():
    c = _client()
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert [o["action"] for o in c.orders] == ["sell"]


# --- it sells what is held, and only that ----------------------------------

def test_sells_the_full_long_yes_position():
    c = _client(qty=30)
    assert unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False) == 30
    assert c.orders[0] == dict(ticker="KXWNBA-26-ATL", side="yes",
                               action="sell", count=30, price=7)


def test_a_short_position_is_a_long_no_and_sells_no():
    c = _client(qty=-12)
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert c.orders[0]["side"] == "no" and c.orders[0]["count"] == 12


def test_no_position_is_a_clean_no_op():
    c = FakeClient(positions={"market_positions": []})
    assert unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False) == 0
    assert c.orders == []


def test_a_flat_row_is_not_a_position():
    # The exchange keeps listing markets after they settle, at position 0.
    c = _client(qty=0)
    assert unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False) == 0
    assert c.orders == []


def test_a_different_ticker_is_never_touched():
    c = _client(ticker="KXOTHER-1")
    assert unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False) == 0
    assert c.orders == []


def test_count_comes_from_the_exchange_not_the_caller():
    # There is no way to pass a size in; the only source is the position record.
    assert "count" not in unwind.unwind.__code__.co_varnames[:3]
    c = _client(qty=5)
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert c.orders[0]["count"] == 5


# --- pricing ---------------------------------------------------------------

def test_sells_into_the_yes_bid():
    c = _client(qty=30, yes_bid=7, yes_ask=8)
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert c.orders[0]["price"] == 7          # the bid, not the ask


def test_selling_no_prices_off_the_yes_ask():
    # A NO bid is quoted as 100 - yes_ask.
    c = _client(qty=-4, yes_bid=7, yes_ask=8)
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert c.orders[0]["price"] == 92


def test_refuses_when_the_book_has_no_bid():
    c = FakeClient(
        positions={"market_positions": [{"ticker": "T", "position": 30}]},
        market={"ticker": "T", "yes_bid": None, "yes_ask": None})
    assert unwind.unwind(c, "T", dry_run=False) == 0
    assert c.orders == []


def test_min_price_floor_blocks_a_dump():
    c = _client(qty=30, yes_bid=2)
    assert unwind.unwind(c, "KXWNBA-26-ATL", min_price=5, dry_run=False) == 0
    assert c.orders == []


def test_min_price_floor_is_inclusive():
    c = _client(qty=30, yes_bid=5)
    assert unwind.unwind(c, "KXWNBA-26-ATL", min_price=5, dry_run=False) == 30


# --- dry run ---------------------------------------------------------------

def test_dry_run_sends_nothing():
    c = _client()
    assert unwind.unwind(c, "KXWNBA-26-ATL", dry_run=True) == 0
    assert c.orders == []


def test_dry_run_is_the_default():
    c = _client()
    unwind.unwind(c, "KXWNBA-26-ATL")
    assert c.orders == []


# --- position parsing ------------------------------------------------------

def test_position_for_handles_string_and_float_quantities():
    pos = {"market_positions": [{"ticker": "T", "position": "30"}]}
    assert unwind.position_for(pos, "T") == ("yes", 30)
    pos = {"market_positions": [{"ticker": "T", "position": 30.0}]}
    assert unwind.position_for(pos, "T") == ("yes", 30)


def test_position_for_survives_junk():
    pos = {"market_positions": [{"ticker": "T", "position": "n/a"}]}
    assert unwind.position_for(pos, "T") is None
    assert unwind.position_for({}, "T") is None
    assert unwind.position_for({"market_positions": None}, "T") is None


# --- configuration ---------------------------------------------------------

def test_exit_does_not_require_the_opening_trade_caps(monkeypatch, tmp_path):
    # The first live unwind died with "Missing required setting
    # 'MAX_ORDER_SIZE'" before it could sell anything. Those caps bound how
    # much risk may be OPENED; requiring them to CLOSE creates a way to be
    # locked out of exiting — a small MAX_ORDER_SIZE would block the sale of a
    # larger holding.
    import config
    pem = tmp_path / "k.pem"
    pem.write_text("x")
    for var in ("MAX_ORDER_SIZE", "MAX_TOTAL_EXPOSURE", "MARKET_TICKER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("KALSHI_ENV", "prod")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", str(pem))

    # what unwind.main() asks for must succeed with no trading caps set
    settings = config.load_kalshi_settings(require_market=False,
                                           require_trading=False)
    assert settings.kalshi_env == "prod"


def test_main_loads_settings_without_requiring_trading_caps(monkeypatch):
    seen = {}

    def fake_load(**kw):
        seen.update(kw)
        raise unwind.ConfigError("stop here — we only care about the call")

    monkeypatch.setattr(unwind, "load_kalshi_settings", fake_load)
    monkeypatch.setattr(unwind.sys, "argv", ["unwind.py", "T"])
    assert unwind.main() == 1
    assert seen == {"require_market": False, "require_trading": False}


# --- unfilled orders are exposure too --------------------------------------
#
# The first live unwind reported "no open position in KXWNBA-26-ATL" and
# stopped. That was correct by its own logic and useless in fact: the 8c buy
# had never filled, so it was not a position — it was a resting order sitting
# in the book, still able to fill and re-open the trade at any moment.

def test_cancels_a_resting_order_even_with_no_position():
    c = _client(qty=0, resting=[{"ticker": "KXWNBA-26-ATL", "order_id": "o9",
                                 "side": "yes", "count": 30}])
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert c.cancelled == ["o9"]


def test_cancels_every_resting_order_in_the_ticker():
    c = _client(qty=0, resting=[
        {"ticker": "KXWNBA-26-ATL", "order_id": "a"},
        {"ticker": "KXWNBA-26-ATL", "order_id": "b"}])
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert c.cancelled == ["a", "b"]


def test_never_cancels_orders_in_another_market():
    c = _client(qty=0, resting=[{"ticker": "KXOTHER-1", "order_id": "z"}])
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert c.cancelled == []


def test_cancels_before_selling_so_we_do_not_cross_ourselves():
    # Kalshi's self-trade prevention rejects a sale that would cross our own
    # resting bid, so the cancel has to happen first.
    events = []
    c = _client(qty=30, resting=[{"ticker": "KXWNBA-26-ATL", "order_id": "o1"}])
    real_cancel, real_order = c.cancel_order, c.create_limit_order
    c.cancel_order = lambda oid: (events.append("cancel"), real_cancel(oid))[1]
    c.create_limit_order = lambda *a, **k: (events.append("sell"),
                                            real_order(*a, **k))[1]
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False)
    assert events == ["cancel", "sell"]


def test_dry_run_cancels_nothing():
    c = _client(qty=30, resting=[{"ticker": "KXWNBA-26-ATL", "order_id": "o1"}])
    unwind.unwind(c, "KXWNBA-26-ATL", dry_run=True)
    assert c.cancelled == [] and c.orders == []


def test_a_failing_resting_lookup_does_not_stop_the_sale():
    c = _client(qty=30)
    def boom():
        raise RuntimeError("orders endpoint down")
    c.get_resting_orders = boom
    assert unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False) == 30


# --- it must report what the exchange says, not what we assume --------------

def test_describe_account_reports_without_trading():
    c = _client(qty=30, resting=[{"ticker": "T", "order_id": "o"}],
                fills=[{"ticker": "KXWNBA-26-ATL", "count": 30}])
    unwind.describe_account(c, "KXWNBA-26-ATL")
    assert c.orders == [] and c.cancelled == []


def test_describe_account_survives_every_endpoint_failing():
    # A diagnostic that crashes takes the exit down with it. Each source is
    # independent and optional.
    c = _client(qty=30)
    for name in ("get_balance_cents", "get_positions", "get_resting_orders",
                 "get_fills"):
        def boom(*a, **k):
            raise RuntimeError("down")
        setattr(c, name, boom)
    unwind.describe_account(c, "T")        # must not raise


def test_a_broken_diagnostic_does_not_block_the_sale():
    c = _client(qty=30)
    def boom(*a, **k):
        raise RuntimeError("down")
    c.get_fills = boom
    assert unwind.unwind(c, "KXWNBA-26-ATL", dry_run=False) == 30
