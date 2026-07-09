"""The central longshot floor: check_order — the ONE gate every buy passes
through (auto_trade, sports, smartmoney, arb) — must refuse a directional BUY on
a near-zero-probability contract. That is the trap that put 98 NO on a 1-8c
bucket and the open hole in sports (model_prob>=60% + EV, but no market-price
floor). Risk-free arb legs opt out (min_price_cents=0) since a cheap hedged leg
is not a longshot."""

from config import Settings
from safety import check_order


def _settings(**kw):
    d = dict(dry_run=False, kill_switch=False,
             max_order_size=100.0, max_total_exposure=1000.0)
    d.update(kw)
    return Settings(**d)


def test_buy_below_floor_is_rejected():
    # a 3c BUY = betting on a market-3% outcome -> must be blocked
    problems = check_order(_settings(), "BUY", 0.03, 1, 0.0)
    assert any("floor" in p.lower() or "longshot" in p.lower() for p in problems)


def test_buy_at_or_above_floor_is_allowed():
    # a normal mid-priced bet passes cleanly
    assert check_order(_settings(), "BUY", 0.64, 1, 0.0) == []


def test_arb_legs_opt_out_of_the_floor():
    # a risk-free hedged basket leg can be cheap and must NOT be blocked
    assert check_order(_settings(), "BUY", 0.03, 1, 0.0, min_price_cents=0) == []


def test_sell_is_not_floored():
    # exiting a position at a low price is fine — the floor is buy-only
    assert check_order(_settings(), "SELL", 0.03, 1, 0.0) == []


def test_sports_style_longshot_blocked_at_the_gate():
    # the concrete sports gap: model rates it 65% but the market prices it 3c.
    # sports has no price floor of its own, so the CENTRAL gate must catch it.
    problems = check_order(_settings(), "BUY", 0.03, 40, 0.0)
    assert problems, "a 40-contract 3c sports longshot slipped through the gate"
