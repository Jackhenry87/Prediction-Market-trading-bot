"""Tests for the commodities vol model. Run: pytest tests/"""

from datetime import datetime, timedelta, timezone

import strategy_commodities as sc


def test_realized_vol_flat_is_zero():
    assert sc.realized_vol_annual([70.0] * 40) == 0.0


def test_realized_vol_positive_on_moves():
    closes, p = [], 70.0
    for i in range(60):
        p *= 1.02 if i % 2 == 0 else 1 / 1.02
        closes.append(p)
    assert sc.realized_vol_annual(closes) > 0.1


def test_evaluate_market_window_and_edge():
    soon = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    # WTI spot $70, strike $90 in 6h -> almost surely NOT above; YES priced 30c
    # -> buying NO (cheap side) is +EV.
    market = {"ticker": "KXWTI-X-T90", "close_time": soon,
              "floor_strike": 90, "cap_strike": None,
              "yes_ask": 33, "yes_bid": 30, "subtitle": "$90 or above"}
    sigs = sc.evaluate_market(market, spot=70.0, sigma=0.4)
    no = [s for s in sigs if s["side"] == "no"]
    assert no and no[0]["model_prob"] > 0.98

    # market closing in 30 days is outside the window -> no signal
    far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    assert sc.evaluate_market(dict(market, close_time=far), 70.0, 0.4) == []


def test_assets_config():
    series = [a["series"] for a in sc.ASSETS]
    assert "KXWTI" in series and len(series) == len(set(series))
    for a in sc.ASSETS:
        assert a["series"].startswith("KX") and a["yahoo"]
