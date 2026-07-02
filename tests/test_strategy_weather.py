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


def test_order_cost_cents_v1_and_v2():
    from kalshi_exposure import _order_cost_cents
    # V1 vocabulary
    assert _order_cost_cents({"action": "buy", "side": "yes",
                              "yes_price": 10, "remaining_count": 5}) == 50
    assert _order_cost_cents({"action": "sell", "side": "yes",
                              "yes_price": 10, "remaining_count": 5}) == 0
    # V2 vocabulary: bid + dollar-string price
    assert _order_cost_cents({"side": "bid", "price": "0.1000",
                              "remaining_count": "10.00"}) == 100
    assert _order_cost_cents({"side": "ask", "price": "0.9000",
                              "remaining_count": "10.00"}) == 0
    # unparseable -> None (caller fails closed)
    assert _order_cost_cents({"side": "bid", "remaining_count": "10.00"}) is None


def test_pick_best_per_event_and_sizing():
    from auto_trade import pick_best_per_event, size_order
    from dataclasses import dataclass

    results = [
        {"date": "2026-07-02", "mu": 100.0, "title": "t", "signals": [
            {"ticker": "A", "side": "no", "price_cents": 65, "ev_cents": 20.4,
             "model_prob": 0.87, "subtitle": ""},
            {"ticker": "B", "side": "no", "price_cents": 51, "ev_cents": 10.3,
             "model_prob": 0.63, "subtitle": ""}]},
        {"date": "2026-07-03", "mu": 99.0, "title": "t", "signals": []},
    ]
    picks = pick_best_per_event(results)
    assert len(picks) == 1 and picks[0]["ticker"] == "A"

    @dataclass
    class S:
        max_order_size: float = 5.0
        max_total_exposure: float = 20.0

    assert size_order(65, 0.0, S()) == 7        # $5 cap / 65c
    assert size_order(65, 18.0, S()) == 3       # only $2 exposure room
    assert size_order(65, 20.0, S()) == 0       # no room
    assert size_order(65, 25.0, S()) == 0       # over cap already


def test_cities_config_sane():
    series = [c["series"] for c in sw.CITIES]
    assert len(series) == len(set(series))          # no duplicates
    for c in sw.CITIES:
        assert c["series"].startswith("KXHIGH")
        assert 24 < c["lat"] < 50 and -125 < c["lon"] < -66  # continental US
