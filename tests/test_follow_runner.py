"""The copier's decision rules.

These are the guards that stand between a push notification and real money.
Each test pins a refusal, because every one of them was written for a reason
the trader's own record supplies: his edge is five trades deep, he sizes in
round thousands rather than by Kelly, and he moves six-figure contract counts
that make his own fill unrepeatable.
"""

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
                "yes_bid": self.yes_bid, "status": "active"}

    def get_balance_cents(self):
        return self.balance

    def create_limit_order(self, ticker, side, action, count, price):
        self.orders.append((ticker, side, action, count, price))
        return {"order_id": "test-order"}


def _settings(**kw):
    base = dict(dry_run=False, kill_switch=False, max_order_size=50.0,
                max_total_exposure=500.0, odds_api_key="k")
    base.update(kw)
    return Settings(**base)


def _wire(monkeypatch, tmp_path, *, our_p=0.70, model="sports",
          ticker="KXMLBGAME-26AUG06LADCHC-CHC", resolve=True):
    """Point the runner at fakes and a temp ledger."""
    monkeypatch.setattr(fr, "PAPER_LOG", tmp_path / "follow_trades.csv")
    monkeypatch.setattr(fr, "UNPARSED_LOG", tmp_path / "unparsed.log")
    monkeypatch.setattr(fr, "DRY_RUN", False)
    monkeypatch.setattr(fr, "USERNAME", "blushing.wildebeest7119")
    monkeypatch.setattr(fr, "current_exposure_usd", lambda c: 0.0)
    monkeypatch.setattr(
        fr.follow_resolve, "resolve_ticker",
        lambda c, s: ({"ticker": ticker, "event_ticker": "KXMLBGAME-26AUG06",
                       "market": {"ticker": ticker, "title": "t"}}
                      if resolve else None))
    monkeypatch.setattr(fr.follow_prob, "model_prob",
                        lambda *a, **k: (our_p, model))


def _note(text):
    return {"id": 1, "text": text}


BUY = ("blushing.wildebeest7119 bought 5,000 Yes on Chicago C at 42¢\n"
       "Los Angeles D vs Chicago C · Yes · Chicago C")


def _rows(tmp_path):
    import csv
    with open(tmp_path / "follow_trades.csv", newline="") as fh:
        return list(csv.DictReader(fh))


# --- the central rule ----------------------------------------------------

def test_no_model_view_means_no_order(monkeypatch, tmp_path):
    """His conviction is not our evidence. If no model of ours prices the
    market, nothing is placed — this is the rule most likely to regress."""
    _wire(monkeypatch, tmp_path, our_p=None, model=None)
    client = FakeClient()
    placed = fr.handle(client, _settings(), _note(BUY),
                       {"seen": set(), "events": set()})
    assert placed is False
    assert client.orders == []
    assert _rows(tmp_path)[0]["reason"] == "no model of ours prices this market"


def test_model_edge_places_the_order(monkeypatch, tmp_path):
    """The positive case: our model says 70%, the ask is 44c (inside the
    slippage tolerance on his 42c entry), so it fires."""
    _wire(monkeypatch, tmp_path, our_p=0.70)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _note(BUY),
                     {"seen": set(), "events": set()}) is True
    assert len(client.orders) == 1
    ticker, side, action, count, price = client.orders[0]
    assert side == "yes" and action == "buy" and count >= 1
    assert price == 44          # we pay today's ask, not his 42c entry


def test_no_edge_no_bet(monkeypatch, tmp_path):
    """Our model agreeing with the market is not a reason to trade."""
    _wire(monkeypatch, tmp_path, our_p=0.50)
    client = FakeClient(yes_ask=50)
    assert fr.handle(client, _settings(), _note(BUY),
                     {"seen": set(), "events": set()}) is False
    assert client.orders == []


# --- the slippage guard --------------------------------------------------

def test_skips_when_the_line_ran_past_his_entry(monkeypatch, tmp_path):
    """He bought at 42c; the book is now 60c because his own 350k-contract
    order moved it. Copying that is buying his impact, not his edge."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr, "MAX_SLIP_CENTS", 3.0)
    client = FakeClient(yes_ask=60)
    assert fr.handle(client, _settings(), _note(BUY),
                     {"seen": set(), "events": set()}) is False
    assert client.orders == []
    assert "past his 42c entry" in _rows(tmp_path)[0]["reason"]


def test_small_slip_within_tolerance_still_trades(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr, "MAX_SLIP_CENTS", 3.0)
    client = FakeClient(yes_ask=44)          # 2c past his 42c — inside
    assert fr.handle(client, _settings(), _note(BUY),
                     {"seen": set(), "events": set()}) is True


def test_missing_price_disables_the_guard_but_not_the_trade(monkeypatch,
                                                            tmp_path):
    """A notification without a price is still tradeable; it just cannot be
    slippage-checked."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=60)
    note = _note("blushing.wildebeest7119 placed a bet\n"
                 "Los Angeles D vs Chicago C · Yes · Chicago C")
    assert fr.handle(client, _settings(), note,
                     {"seen": set(), "events": set()}) is True


# --- dedupe --------------------------------------------------------------

def test_never_trades_the_same_event_twice(monkeypatch, tmp_path):
    """Two notifications, one event — the second must not become the other
    side of a match we already hold."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=44)
    state = {"seen": set(), "events": set()}
    assert fr.handle(client, _settings(), _note(BUY), state) is True
    assert fr.handle(client, _settings(), _note(BUY), state) is False
    assert len(client.orders) == 1


# --- the hard rails ------------------------------------------------------

def test_kill_switch_blocks_the_copy(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(kill_switch=True), _note(BUY),
                     {"seen": set(), "events": set()}) is False
    assert client.orders == []
    assert _rows(tmp_path)[0]["decision"] == "blocked"


def test_dry_run_places_nothing_but_records_the_decision(monkeypatch,
                                                         tmp_path):
    """The paper phase must still capture what it WOULD have done — that
    ledger is the only evidence that decides whether this goes live."""
    _wire(monkeypatch, tmp_path, our_p=0.90)
    monkeypatch.setattr(fr, "DRY_RUN", True)
    client = FakeClient(yes_ask=44)
    assert fr.handle(client, _settings(), _note(BUY),
                     {"seen": set(), "events": set()}) is False
    assert client.orders == []
    row = _rows(tmp_path)[0]
    assert row["decision"] == "paper"
    assert int(row["contracts"]) >= 1        # sizing still computed
    assert row["our_p"] == "0.9000"


def test_price_band_rejects_a_longshot(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.99)
    monkeypatch.setattr(fr, "MIN_PRICE_CENTS", 10.0)
    client = FakeClient(yes_ask=4)
    note = _note("blushing.wildebeest7119 bought Yes on Chicago C\n"
                 "Los Angeles D vs Chicago C · Yes · Chicago C")
    assert fr.handle(client, _settings(), note,
                     {"seen": set(), "events": set()}) is False
    assert client.orders == []


def test_unresolvable_market_is_skipped(monkeypatch, tmp_path):
    """A market we cannot pin to a ticker is a miss, never a guess."""
    _wire(monkeypatch, tmp_path, resolve=False)
    client = FakeClient()
    assert fr.handle(client, _settings(), _note(BUY),
                     {"seen": set(), "events": set()}) is False
    assert client.orders == []


def test_unparsed_notification_is_kept_for_inspection(monkeypatch, tmp_path):
    """We have never seen a real notification, so anything unreadable has to
    survive for the pattern to be fixed."""
    _wire(monkeypatch, tmp_path)
    client = FakeClient()
    assert fr.handle(client, _settings(), _note("You've got money!"),
                     {"seen": set(), "events": set()}) is False
    assert "You've got money!" in (tmp_path / "unparsed.log").read_text()


def test_another_traders_notification_never_trades(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, our_p=0.90)
    client = FakeClient(yes_ask=44)
    note = _note("smiling.aardvark1234 bought 5,000 Yes on Chicago C at 42¢\n"
                 "Los Angeles D vs Chicago C · Yes · Chicago C")
    assert fr.handle(client, _settings(), note,
                     {"seen": set(), "events": set()}) is False
    assert client.orders == []


def test_order_failure_is_logged_not_raised(monkeypatch, tmp_path):
    """A broken order path must not kill the loop — the next notification
    still has to be handled."""
    _wire(monkeypatch, tmp_path, our_p=0.90)

    class Boom(FakeClient):
        def create_limit_order(self, *a, **k):
            raise RuntimeError("kalshi 500")

    client = Boom(yes_ask=44)
    assert fr.handle(client, _settings(), _note(BUY),
                     {"seen": set(), "events": set()}) is False
    assert _rows(tmp_path)[0]["decision"] == "error"


# --- cursor handling -----------------------------------------------------

def test_cursor_advances_only_over_handled_events(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "CURSOR_PATH", tmp_path / "cursor.txt")
    _wire(monkeypatch, tmp_path, our_p=None, model=None)
    monkeypatch.setattr(fr, "drain", lambda since: [{"id": 7, "text": BUY}])
    fr.run_once(FakeClient(), _settings(), {"seen": set(), "events": set()})
    assert fr.read_cursor() == 7


def test_drain_failure_does_not_move_the_cursor(monkeypatch, tmp_path):
    """A flaky network must never look like 'he placed a trade'."""
    monkeypatch.setattr(fr, "CURSOR_PATH", tmp_path / "cursor.txt")
    fr.write_cursor(4)
    monkeypatch.setattr(fr, "drain", lambda since: [])
    fr.run_once(FakeClient(), _settings(), {"seen": set(), "events": set()})
    assert fr.read_cursor() == 4
