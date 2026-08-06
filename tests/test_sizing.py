"""Position sizing.

The requirement is that units vary with edge and confidence — never a flat
percentage, never a token bet on a signal too weak to earn one. These tests
pin the arithmetic and, more importantly, the refusals.
"""

import sizing


# --- the Kelly arithmetic ---------------------------------------------------

def test_kelly_is_edge_over_upside():
    # 2c edge on an 89c contract: risk 89 to win 11, so f* = 2/11.
    assert round(sizing.kelly_fraction(89, 2.0), 6) == round(2.0 / 11.0, 6)
    # the same 2c edge on a 60c contract is a much smaller fraction: 2/40
    assert round(sizing.kelly_fraction(60, 2.0), 6) == 0.05


def test_no_edge_no_bet():
    assert sizing.kelly_fraction(89, 0.0) == 0.0
    assert sizing.kelly_fraction(89, -3.0) == 0.0
    assert sizing.kelly_fraction(100, 5.0) == 0.0     # no upside left


# --- confidence weighting ---------------------------------------------------

def test_edge_splits_into_mechanical_and_assumed():
    # mid 90, entry 89 -> 1c certain. bias 1c at half confidence -> 0.5c.
    assert sizing.effective_edge_cents(90, 89, 1.0, 0.5) == 1.5


def test_lower_confidence_shrinks_only_the_assumed_part():
    full = sizing.effective_edge_cents(90, 89, 2.0, 1.0)
    none = sizing.effective_edge_cents(90, 89, 2.0, 0.0)
    assert full == 3.0 and none == 1.0        # mechanical 1c survives at zero
                                              # confidence in the research claim


def test_two_signals_same_headline_edge_size_differently():
    # Both 2c of headline edge, but A's is mechanical and B's is assumption.
    a = sizing.effective_edge_cents(mid=91, entry=89, bias_cents=0.0,
                                    bias_confidence=0.5)
    b = sizing.effective_edge_cents(mid=89, entry=89, bias_cents=4.0,
                                    bias_confidence=0.5)
    assert a == 2.0 and b == 2.0              # same discounted edge...
    raw_b = sizing.effective_edge_cents(89, 89, 4.0, 1.0)
    assert raw_b > a                          # ...but B leaned on the claim


# --- sizing decisions -------------------------------------------------------

def test_bigger_edge_earns_more_contracts():
    small = sizing.size_position(50.0, 89, 1.0)
    big = sizing.size_position(50.0, 89, 3.0)
    assert big["contracts"] > small["contracts"]


def test_thin_edge_is_refused_not_rounded_up():
    # THE anti-hail-mary rule: a stake below one contract is zero contracts.
    r = sizing.size_position(50.0, 89, sizing.MIN_EDGE_CENTS - 0.01)
    assert r["contracts"] == 0 and "floor" in r["reason"]


def test_sub_contract_stake_is_zero_not_one():
    # $5 bankroll, 1c edge on 89c: quarter Kelly is ~2.3% = $0.11, well under
    # one contract. Must refuse rather than bet a whole contract (~18% of
    # bankroll) — that would be a 8x overbet.
    r = sizing.size_position(5.0, 89, 1.0)
    assert r["contracts"] == 0
    assert "no bet" in r["reason"]


def test_quarter_kelly_on_a_50_dollar_bankroll():
    # 2c edge, 89c entry: full Kelly 18.2%, quarter 4.5% of $50 = $2.27,
    # which buys 2 contracts at 89c.
    r = sizing.size_position(50.0, 89, 2.0)
    assert round(r["kelly_full"], 3) == 0.182
    assert round(r["kelly_used"], 4) == 0.0455
    assert r["contracts"] == 2


def test_hard_cap_overrides_kelly():
    # A huge claimed edge must not produce a huge bet.
    r = sizing.size_position(1000.0, 50, 40.0, max_bankroll_pct=5)
    assert r["kelly_used"] == 0.05
    assert r["stake_usd"] == 50.0


def test_sizing_scales_with_bankroll():
    small = sizing.size_position(50.0, 89, 2.0)
    large = sizing.size_position(500.0, 89, 2.0)
    assert large["contracts"] > small["contracts"] * 8


def test_reason_is_always_populated():
    for args in ((50.0, 89, 2.0), (50.0, 89, 0.1), (0.0, 89, 2.0),
                 (50.0, 0, 2.0)):
        assert sizing.size_position(*args)["reason"]
