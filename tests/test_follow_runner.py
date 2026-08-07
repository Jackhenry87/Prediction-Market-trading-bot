"""The copier's decision rules.

These are the guards standing between "he traded" and real money. Each test
pins a refusal, and each refusal has a reason his own record supplies: his
edge is five trades deep, he sizes in round thousands rather than by Kelly,
and he moves six-figure contract counts that make his fill unrepeatable.

The feed hands `handle` a signal dict directly now — the notification-parsing
layer is gone, so there is no prose to misread.
"""

import follow_feed
import follow_runner as fr
from config import Settings


class FakeClient:
    """Enough Kalshi surface for the runner, and a record of what it sent."""

    def __init__(self, yes_ask=50, yes_bid=48, balance=100_00):
        self.yes_ask, self.yes_bid = yes_ask, yes_bid
        self.balance = balance
        self.orders = []

    def get_market(self, ticker):
        return {"ticker": ticker, "yes_ask": self.yes_ask,
                "yes_bid": self.yes_bid, "status": "active", "title": "t"}

    def get_balance_cents(self):
        return self.balance

    def create_limit_order(self, ticker, side, action, count, price):
        self.orders.append((ticker, side, action, count, price))
        return {"order_id": "test-order"}


TICKER = "KXMLBGAME-26AUG06LADCHC-CHC"


def _settings(**kw):
    base = dict(dry_run=False, kill_switch=False, max_order_size=50.0,
                max_total_exposure=500.0, odds_api_key="k")
    base.update(kw)
    return Settings(**base)


def _sig(**kw):
    """A trade of his, as follow_feed normalises it."""
    base = dict(username="blushing.wildebeest7119", ticker=TICKER,
                side="yes", price_cents=42, contracts=250_000,
                action="buy", trade_id="t1", market_title=None,
                outcome_text=None, ts="2026-08-06T19:00:00Z")
    base.update(kw)
    return base


def _wire(monkeypatch, tmp_path, *, our_p=0.70, model="sports"):
    monkeypatch.setattr(fr, "PAPER_LOG", tmp_path / "follow_trades.csv")
    monkeypatch.setattr(fr, "DRY_RUN", False)
    monkeypatch.setattr(fr, "current_exposure_usd", lambda c: 0.0)
    monkeypatch.setattr(fr.follow_prob, "model_prob",
                        lambda *a, **k: (our_p, model))


def _rows(tmp_path):
    import csv
    with open(tmp_path / "follow_trades.csv", newline="") as fh:
        return list(csv.DictReader(fh))


def _state():
    return {"seen": set(), "events": set()}


# --- the central rule ----------------------------------------------------

def test_no_model_view_means_no_order(monkeypatch, tmp_path):
    """His conviction is not our evidence. If no model of ours prices the
    market, nothing is placed — the rule most likely to regress."""
    _wire(monkeypatch, tmp_path, our_p=None, model=None)
    client = FakeClient()
    assert fr.handle(client, _settings(), _sig(), _state()) is False
    assert client.orders == []
    assert _rows(tmp_path)[0]["reason"] == "no model of ours prices this market"


def test_model_edge_places_the_order(monkeypatch, tmp_path):
    """Our model says 70%, the ask is 44c (inside slippage tolerance on his
    42c entry), so it fires."""
    _wire(monkeypatch, tmp_path, our_p=0.70)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _sig(), _state()) is True
    assert len(client.orders) == 1
    ticker, side, action, count, price = client.orders[0]
    assert (ticker, side, action) == (TICKER, "yes", "buy") and count >= 1
    assert price == 44          # we pay today's ask, not his 42c entry


def test_no_edge_no_bet(monkeypatch, tmp_path):
    """Our model agreeing with the market is not a reason to trade.

    p == the ask leaves nothing once Kalshi's taker fee is paid, so the
    correct answer is no bet — his having traded it changes nothing.
    """
    _wire(monkeypatch, tmp_path, our_p=0.44)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _sig(), _state()) is False
    assert client.orders == []


# --- the slippage guard --------------------------------------------------

def test_skips_when_the_line_ran_past_his_entry(monkeypatch, tmp_path):
    """He bought at 42c; the book is 60c because his own 250k-contract order
    moved it. Copying that buys his impact, not his edge."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr, "MAX_SLIP_CENTS", 3.0)
    client = FakeClient(yes_ask=60)
    assert fr.handle(client, _settings(), _sig(), _state()) is False
    assert client.orders == []
    assert "past his 42c entry" in _rows(tmp_path)[0]["reason"]


def test_small_slip_within_tolerance_still_trades(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr, "MAX_SLIP_CENTS", 3.0)
    client = FakeClient(yes_ask=44)          # 2c past his 42c — inside
    assert fr.handle(client, _settings(), _sig(), _state()) is True


def test_missing_price_disables_the_guard_but_not_the_trade(monkeypatch,
                                                            tmp_path):
    """A record without a price is still tradeable; it just cannot be
    slippage-checked."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=60)
    assert fr.handle(client, _settings(), _sig(price_cents=None),
                     _state()) is True


# --- resolution ----------------------------------------------------------

def test_a_ticker_from_the_feed_skips_prose_matching(monkeypatch, tmp_path):
    """The whole point of polling: the API names the market, so there is no
    title to match and no ambiguity to get wrong."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    called = []
    monkeypatch.setattr(fr.follow_resolve, "resolve_ticker",
                        lambda c, s: called.append(1))
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _sig(), _state()) is True
    assert called == []


def test_a_record_without_a_ticker_falls_back_to_prose(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(
        fr.follow_resolve, "resolve_ticker",
        lambda c, s: {"ticker": TICKER, "event_ticker": "E",
                      "market": {"ticker": TICKER, "title": "t"}})
    client = FakeClient(yes_ask=44)
    sig = _sig(ticker=None, market_title="Los Angeles D vs Chicago C",
               outcome_text="Chicago C")
    assert fr.handle(client, _settings(), sig, _state()) is True


def test_unresolvable_market_is_skipped(monkeypatch, tmp_path):
    """A market we cannot pin to a ticker is a miss, never a guess."""
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(fr.follow_resolve, "resolve_ticker", lambda c, s: None)
    client = FakeClient()
    assert fr.handle(client, _settings(), _sig(ticker=None), _state()) is False
    assert client.orders == []


# --- what we refuse to copy ---------------------------------------------

def test_a_sale_is_not_copied_as_a_buy(monkeypatch, tmp_path):
    """Exits are out of scope; mirroring one as a buy would be backwards."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _sig(action="sell"),
                     _state()) is False
    assert client.orders == []


def test_a_record_with_no_readable_side_is_skipped(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _sig(side=None), _state()) is False
    assert client.orders == []


# --- dedupe --------------------------------------------------------------

def test_never_trades_the_same_event_twice(monkeypatch, tmp_path):
    """Two fills on one event must not become both sides of a match."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=44)
    state = _state()
    assert fr.handle(client, _settings(), _sig(), state) is True
    assert fr.handle(client, _settings(), _sig(trade_id="t2"), state) is False
    assert len(client.orders) == 1


# --- the hard rails ------------------------------------------------------

def test_kill_switch_blocks_the_copy(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(kill_switch=True), _sig(),
                     _state()) is False
    assert client.orders == []
    assert _rows(tmp_path)[0]["decision"] == "blocked"


def test_dry_run_places_nothing_but_records_the_decision(monkeypatch,
                                                         tmp_path):
    """The paper phase must capture what it WOULD have done — that ledger is
    the only evidence that decides whether this goes live."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr, "DRY_RUN", True)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _sig(), _state()) is False
    assert client.orders == []
    row = _rows(tmp_path)[0]
    assert row["decision"] == "paper"
    assert int(row["contracts"]) >= 1        # sizing still computed
    assert row["our_p"] == "0.9000"


def test_price_band_rejects_a_longshot(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.99)
    monkeypatch.setattr(fr, "MIN_PRICE_CENTS", 10.0)
    client = FakeClient(yes_ask=4)
    assert fr.handle(client, _settings(), _sig(price_cents=None),
                     _state()) is False
    assert client.orders == []


def test_order_failure_is_logged_not_raised(monkeypatch, tmp_path):
    """A broken order path must not kill the loop."""
    _wire(monkeypatch, tmp_path, our_p=0.90)

    class Boom(FakeClient):
        def create_limit_order(self, *a, **k):
            raise RuntimeError("kalshi 500")

    client = Boom(yes_ask=44)
    assert fr.handle(client, _settings(), _sig(), _state()) is False
    assert _rows(tmp_path)[0]["decision"] == "error"


# --- the poll pass -------------------------------------------------------

def test_run_once_handles_every_new_trade(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr.follow_feed, "poll",
                        lambda c: [_sig(), _sig(trade_id="t2",
                                                ticker="KXMLBGAME-26AUG06NYMCLE-NYM")])
    client = FakeClient(yes_ask=44)
    assert fr.run_once(client, _settings(), _state()) == 2


def test_a_broken_feed_propagates_rather_than_reading_as_quiet(monkeypatch,
                                                               tmp_path):
    """An outage and 'he has not traded' must never look the same."""
    _wire(monkeypatch, tmp_path)

    def boom(_c):
        raise follow_feed.FeedError("endpoint gone")

    monkeypatch.setattr(fr.follow_feed, "poll", boom)
    try:
        fr.run_once(FakeClient(), _settings(), _state())
        assert False, "run_once should not swallow a FeedError"
    except follow_feed.FeedError:
        pass


def test_a_crash_on_one_trade_does_not_stop_the_rest(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr.follow_feed, "poll",
                        lambda c: [_sig(ticker=None), _sig()])
    monkeypatch.setattr(fr.follow_resolve, "resolve_ticker",
                        lambda c, s: (_ for _ in ()).throw(RuntimeError("x")))
    client = FakeClient(yes_ask=44)
    assert fr.run_once(client, _settings(), _state()) == 1
