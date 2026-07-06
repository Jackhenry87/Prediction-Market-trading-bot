"""Tests for the macro resolution-lag model. Run: pytest tests/"""

import strategy_macro as sm


def test_transforms():
    obs = [(f"2025-{m:02d}-01", 100.0 + m) for m in range(1, 14)]  # 13 months
    # level = latest value
    assert sm.latest_actual(obs, "level")[1] == 113.0
    # yoy_pct = (latest/12-months-ago - 1)*100 = (113/101 - 1)*100
    assert abs(sm.latest_actual(obs, "yoy_pct")[1] - (113/101 - 1) * 100) < 1e-9
    # mom change in jobs = (latest - prev) * 1000
    assert abs(sm.latest_actual(obs, "mom_change_jobs")[1] - 1000.0) < 1e-9


def test_known_outcome_yes_side():
    # actual 3.2 lands in [3.0, 3.5) -> YES certain; ask 88c -> buy YES +EV
    m = {"ticker": "T", "floor_strike": 3.0, "cap_strike": 3.5,
         "yes_ask": 88, "yes_bid": 84}
    s = sm.known_outcome_signal(m, 3.2)
    assert s["side"] == "yes" and s["model_prob"] == 1.0 and s["ev_cents"] > 3


def test_known_outcome_no_side():
    # actual 4.0 is NOT in [3.0, 3.5) -> NO certain; yes_bid 60 -> NO costs 40c
    m = {"ticker": "T", "floor_strike": 3.0, "cap_strike": 3.5,
         "yes_ask": 66, "yes_bid": 60}
    s = sm.known_outcome_signal(m, 4.0)
    assert s["side"] == "no" and s["ev_cents"] > 3


def test_no_signal_when_already_priced():
    # YES certain but already at 99c -> no lag left to capture
    m = {"ticker": "T", "floor_strike": 3.0, "cap_strike": 3.5,
         "yes_ask": 99, "yes_bid": 98}
    assert sm.known_outcome_signal(m, 3.2) is None


def test_tail_market_open_bounds():
    # "3.5 or above": floor 3.5, no cap. actual 4.0 -> YES certain
    m = {"ticker": "T", "floor_strike": 3.5, "cap_strike": None,
         "yes_ask": 80, "yes_bid": 76}
    assert sm.known_outcome_signal(m, 4.0)["side"] == "yes"
