"""Tests for the standalone smart-money copy runner."""

import smartmoney_runner as smr
import strategy_smartmoney as sm
from config import Settings


def test_copy_pct_scales_with_conviction(monkeypatch):
    monkeypatch.setattr(sm, "MIN_WALLETS", 3)
    assert smr.copy_pct(3) == 4.0     # minimum consensus -> 4% (owner spec)
    assert smr.copy_pct(4) == 5.0
    assert smr.copy_pct(6) == 7.0
    assert smr.copy_pct(7) == 8.0     # capped at 8%
    assert smr.copy_pct(12) == 8.0


def test_contracts_for_budget():
    assert smr.contracts_for(1.23, 46) == 2     # $1.23 buys 2x 46c
    assert smr.contracts_for(0.40, 46) == 0     # can't afford one
    assert smr.contracts_for(0.46, 46) == 1
    assert smr.contracts_for(5.00, 0) == 0      # nonsense price -> nothing


def test_sm_price_floor_blocks_lottery_tickets():
    market = {"ticker": "T", "status": "active", "yes_ask": 20, "yes_bid": 18}
    cons = dict(slug="x", outcome="Yes", title="t", wallets=4, stake=100.0,
                avg_price=0.18)
    event = {"event_ticker": "E", "title": "e"}
    assert sm._priced_signal(cons, market, event) is None   # 20c < 25c floor
    market["yes_ask"] = 30
    cons["avg_price"] = 0.28
    sig = sm._priced_signal(cons, market, event)
    assert sig and sig["price_cents"] == 30                 # floor passed
    assert sig["wallets"] == 4 and sig["stake"] == 100.0


def test_signal_event_prefers_authoritative_ticker():
    # the mapper-carried event_ticker wins; ticker prefix is the fallback
    assert smr.signal_event(
        {"event_ticker": "KXWTAMATCH-26JUL07PEGGAU",
         "ticker": "KXWTAMATCH-26JUL07PEGGAU-GAU"}
    ) == "KXWTAMATCH-26JUL07PEGGAU"
    assert smr.signal_event(
        {"ticker": "KXWTAMATCH-26JUL07PEGGAU-PEG"}
    ) == "KXWTAMATCH-26JUL07PEGGAU"


class _FakeClient:
    """Live snapshot deliberately reports NOTHING held — reproducing the
    real bug where a maker order placed one pass hasn't surfaced in
    positions/resting by the next pass."""

    def __init__(self):
        self.orders = []

    def get_balance_cents(self):
        return 4000                      # $40 bankroll

    def get_positions(self):
        return {"market_positions": []}

    def get_resting_orders(self):
        return []

    def create_limit_order(self, ticker, side, action, count, price):
        self.orders.append((ticker, side, action, count, price))
        return {"order_id": f"oid-{len(self.orders)}"}


def _one_side(ticker, event, price):
    """A single scan result carrying one copyable side of a match."""
    return {"signals": [dict(
        ticker=ticker, event_ticker=event, side="yes", price_cents=price,
        wallets=4, stake=500.0, wallet_ids=["w1", "w2", "w3", "w4"],
        subtitle="sharp pile")]}


def test_never_both_sides_across_passes(monkeypatch):
    # THE regression: PEG bought pass 1, GAU offered pass 2 on the SAME
    # event. Even though the live snapshot never reports PEG (fill lag),
    # durable session memory must refuse the opposite side.
    ev = "KXWTAMATCH-26JUL07PEGGAU"
    passes = [
        [_one_side(ev + "-PEG", ev, 78)],   # pass 1: sharps on Pegula
        [_one_side(ev + "-GAU", ev, 26)],   # pass 2: sharps on Gauff
    ]
    monkeypatch.setattr(sm, "scan", lambda: passes.pop(0))
    monkeypatch.setattr(smr, "log_signals", lambda *a, **k: None)
    monkeypatch.setattr(smr, "log_execution", lambda *a, **k: None)
    monkeypatch.setattr(sm, "log_copy_wallets", lambda *a, **k: None)
    import kalshi_exposure
    monkeypatch.setattr(kalshi_exposure, "current_exposure_usd",
                        lambda c: 0.0)

    client = _FakeClient()
    settings = Settings(dry_run=False, max_order_size=50.0,
                        max_total_exposure=250.0)
    seen, events = set(), set()

    assert smr.copy_pass(client, settings, seen, events) == 1   # PEG placed
    assert smr.copy_pass(client, settings, seen, events) == 0   # GAU refused
    assert len(client.orders) == 1
    assert client.orders[0][0] == ev + "-PEG"          # only Pegula, not Gauff


def test_never_both_sides_within_one_pass(monkeypatch):
    # Both sides surfacing in a SINGLE scan must also collapse to one bet.
    ev = "KXATPMATCH-26JUL08COBFER"
    monkeypatch.setattr(sm, "scan", lambda: [
        _one_side(ev + "-COB", ev, 55), _one_side(ev + "-FER", ev, 47)])
    monkeypatch.setattr(smr, "log_signals", lambda *a, **k: None)
    monkeypatch.setattr(smr, "log_execution", lambda *a, **k: None)
    monkeypatch.setattr(sm, "log_copy_wallets", lambda *a, **k: None)
    import kalshi_exposure
    monkeypatch.setattr(kalshi_exposure, "current_exposure_usd",
                        lambda c: 0.0)

    client = _FakeClient()
    settings = Settings(dry_run=False, max_order_size=50.0,
                        max_total_exposure=250.0)
    assert smr.copy_pass(client, settings, set(), set()) == 1
    assert len(client.orders) == 1
