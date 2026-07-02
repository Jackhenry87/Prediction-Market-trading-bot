"""Tests for the weather-edge math. Run: pytest tests/"""

import strategy_weather as sw


def test_normal_cdf_basics():
    assert abs(sw.normal_cdf(0, 0, 1) - 0.5) < 1e-9
    assert sw.normal_cdf(10, 0, 1) > 0.999
    assert sw.normal_cdf(-10, 0, 1) < 0.001


def test_bucket_probability_sums_to_one():
    mu = 88.0
    # buckets: <=85, 85-87, 87-89, 89-91, >=91 (edges shared)
    buckets = [(None, 85), (85, 87), (87, 89), (89, 91), (91, None)]
    total = sum(sw.bucket_probability(mu, lo, hi) for lo, hi in buckets)
    assert abs(total - 1.0) < 1e-9
    # the bucket containing the forecast is the most likely
    probs = [sw.bucket_probability(mu, lo, hi) for lo, hi in buckets]
    assert max(probs) == probs[2]


def test_taker_fee():
    assert abs(sw.taker_fee_cents(50) - 1.75) < 1e-9   # worst case at 50c
    assert sw.taker_fee_cents(1) < 0.1                 # tiny at the tails
    assert sw.taker_fee_cents(99) < 0.1


def test_date_from_event_ticker():
    assert sw.date_from_event_ticker("KXHIGHNY-26JUL02") == "2026-07-02"
    assert sw.date_from_event_ticker("KXHIGHNY-26DEC31") == "2026-12-31"
    assert sw.date_from_event_ticker("garbage") is None


def test_price_cents_handles_both_formats():
    assert sw.price_cents({"yes_ask": 42}, "yes_ask") == 42
    assert sw.price_cents({"yes_ask_dollars": "0.4200"}, "yes_ask") == 42
    assert sw.price_cents({}, "yes_ask") is None


def test_evaluate_market_finds_underpriced_yes():
    # forecast 88 with sigma 3: bucket 85-91 holds ~68% prob; ask 20c -> big edge
    market = {"ticker": "T-1", "subtitle": "85 to 91",
              "floor_strike": 85, "cap_strike": 91,
              "yes_ask": 20, "yes_bid": 15}
    signals = sw.evaluate_market(market, mu=88.0)
    yes = [s for s in signals if s["side"] == "yes"]
    assert yes and yes[0]["ev_cents"] > 20


def test_evaluate_market_finds_overpriced_yes():
    # bucket far from forecast yet bid 40c -> buying NO is the edge
    market = {"ticker": "T-2", "subtitle": "99 or above",
              "floor_strike": 99, "cap_strike": None,
              "yes_ask": 45, "yes_bid": 40}
    signals = sw.evaluate_market(market, mu=88.0)
    no = [s for s in signals if s["side"] == "no"]
    assert no and no[0]["ev_cents"] > 25


def test_evaluate_market_no_signal_when_fair():
    # bucket 85-91 is ~68% under the model and priced there -> fees kill it
    market = {"ticker": "T-3", "subtitle": "85 to 91",
              "floor_strike": 85, "cap_strike": 91,
              "yes_ask": 70, "yes_bid": 66}
    assert sw.evaluate_market(market, mu=88.0) == []
