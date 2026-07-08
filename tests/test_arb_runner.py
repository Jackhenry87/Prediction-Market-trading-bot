"""Tests for arb basket placement — the money-critical guarantee is that a
basket is placed FULLY risk-free or not at all. These cover the two ways a
basket could otherwise go one-sided: a risk-gate block mid-basket, and a
leg that under-fills after the book moves."""

import pytest

import arb_runner
from config import Settings


def _settings(**kw):
    d = dict(dry_run=False, kill_switch=False,
             max_order_size=50.0, max_total_exposure=200.0)
    d.update(kw)
    return Settings(**d)


class _RecordClient:
    """Records every order sent and every cancel; returns a configurable fill
    count so we can simulate full vs partial fills."""
    def __init__(self, fill=None):
        self.orders = []
        self.canceled = []
        self._fill = fill              # None -> full fill of `count`

    def create_limit_order(self, ticker, side, action, count, price):
        self.orders.append((ticker, side, action, count, price))
        fc = count if self._fill is None else self._fill
        return {"order_id": f"o{len(self.orders)}",
                "fill_count": fc, "status": "executed"}

    def cancel_order(self, oid):
        self.canceled.append(oid)


@pytest.fixture(autouse=True)
def _no_ledger_writes(monkeypatch):
    # don't touch the real executed_trades.csv from unit tests
    monkeypatch.setattr(arb_runner, "log_execution", lambda *a, **k: None)


def _arb(legs, count, n=None, side="yes"):
    return {"event_ticker": "E", "n": n or len(legs), "side": side,
            "count": count, "profit_cents": 4.0, "legs": legs}


def test_preflight_blocks_whole_basket_no_partial():
    # count 200: leg1 notional 200*0.20=$40 (< $50 cap) would pass alone, but
    # leg2 200*0.30=$60 exceeds it. Pre-flight must send NOTHING — never a
    # one-sided position from placing leg1 then blocking on leg2.
    client = _RecordClient()
    arb = _arb([("A", 20, "yes"), ("B", 30, "yes"), ("C", 25, "yes")], count=200)
    ok = arb_runner.place_basket(client, _settings(max_order_size=50), arb, 0.0)
    assert ok is False
    assert client.orders == []            # nothing sent
    assert client.canceled == []


def test_full_fill_places_all_legs():
    client = _RecordClient()              # fills == count
    arb = _arb([("A", 30, "yes"), ("B", 30, "yes")], count=10)
    ok = arb_runner.place_basket(client, _settings(), arb, 0.0)
    assert ok is True
    assert len(client.orders) == 2
    assert client.canceled == []


def test_partial_fill_detected_cancelled_and_halted():
    # first leg only fills 3 of 10 -> basket is unbalanced; must cancel the
    # remainder, stop (not place leg 2), and report failure — never silently
    # claim a basket that isn't fully hedged.
    client = _RecordClient(fill=3)
    arb = _arb([("A", 30, "yes"), ("B", 30, "yes")], count=10)
    ok = arb_runner.place_basket(client, _settings(), arb, 0.0)
    assert ok is False
    assert len(client.orders) == 1        # halted after the under-filled leg
    assert client.canceled == ["o1"]      # remainder cancelled


def test_kill_switch_sends_nothing():
    client = _RecordClient()
    arb = _arb([("A", 30, "yes"), ("B", 30, "yes")], count=10)
    ok = arb_runner.place_basket(client, _settings(kill_switch=True), arb, 0.0)
    assert ok is False
    assert client.orders == []


def test_dry_run_places_nothing_but_clears():
    client = _RecordClient()
    arb = _arb([("A", 30, "yes"), ("B", 30, "yes")], count=10)
    ok = arb_runner.place_basket(client, _settings(dry_run=True), arb, 0.0)
    assert ok is True
    assert client.orders == []            # dry-run never sends


def test_filled_count_parses_vintages():
    assert arb_runner._filled_count({"fill_count": 5}, 10) == 5
    assert arb_runner._filled_count({"remaining_count": 4}, 10) == 6
    assert arb_runner._filled_count({"status": "executed"}, 10) == 10
    assert arb_runner._filled_count({"status": "resting"}, 10) == 0
    assert arb_runner._filled_count({"nope": 1}, 10) is None
