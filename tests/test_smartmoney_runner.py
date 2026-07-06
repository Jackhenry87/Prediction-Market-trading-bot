"""Tests for the standalone smart-money copy runner."""

import smartmoney_runner as smr
import strategy_smartmoney as sm


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
