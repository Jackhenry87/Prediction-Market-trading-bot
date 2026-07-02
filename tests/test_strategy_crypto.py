"""Tests for the crypto vol-model math. Run: pytest tests/"""

import math
from datetime import datetime, timedelta, timezone

import strategy_crypto as sc


def test_realized_vol_flat_prices_is_zero():
    assert sc.realized_vol_annual([100.0] * 50) == 0.0


def test_realized_vol_scales_with_moves():
    # alternating +1%/-1% hourly moves -> ~1% hourly vol, annualized
    closes, price = [], 100.0
    for i in range(200):
        price *= 1.01 if i % 2 == 0 else 1 / 1.01
        closes.append(price)
    vol = sc.realized_vol_annual(closes)
    assert 0.5 < vol < 1.5  # ~0.93 expected; just sanity-band it


def test_prob_above_basics():
    spot, sigma, tau = 100_000, 0.5, 1 / 365
    # strike far below spot -> near certain; far above -> near zero
    assert sc.prob_above(spot, sigma, tau, 50_000) > 0.999
    assert sc.prob_above(spot, sigma, tau, 200_000) < 0.001
    # at-the-money is near 50% (slightly below due to lognormal drift term)
    atm = sc.prob_above(spot, sigma, tau, 100_000)
    assert 0.45 < atm < 0.51


def test_bucket_probabilities_sum_to_one():
    spot, sigma, tau = 100_000, 0.6, 12 / (24 * 365)
    edges = [None, 95_000, 99_000, 101_000, 105_000, None]
    total = sum(
        sc.bucket_probability(spot, sigma, tau, lo, hi)
        for lo, hi in zip(edges[:-1], edges[1:])
    )
    assert abs(total - 1.0) < 1e-9


def test_hours_to_close_and_window():
    soon = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    h = sc.hours_to_close({"close_time": soon})
    assert 4.9 < h < 5.1
    assert sc.hours_to_close({}) is None

    # market outside the window produces no signals even if mispriced
    far = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    market = {"ticker": "X", "close_time": far, "floor_strike": 1,
              "cap_strike": None, "yes_ask": 1, "yes_bid": 1}
    assert sc.evaluate_market(market, spot=100_000, sigma=0.5) == []


def test_evaluate_market_flags_overpriced_far_strike():
    # strike 3x spot closing in 6h priced at 30c -> NO is nearly free money
    close = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    market = {"ticker": "KXBTCD-X-T300000", "close_time": close,
              "floor_strike": 300_000, "cap_strike": None,
              "yes_ask": 32, "yes_bid": 30, "subtitle": "$300,000 or above"}
    signals = sc.evaluate_market(market, spot=100_000, sigma=0.6)
    no = [s for s in signals if s["side"] == "no"]
    assert no and no[0]["model_prob"] > 0.99
