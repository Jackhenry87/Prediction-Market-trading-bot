"""Tests for the weather-edge math. Run: pytest tests/"""

import strategy_weather as sw


def test_normal_cdf_basics():
    assert abs(sw.normal_cdf(0, 0, 1) - 0.5) < 1e-9
    assert sw.normal_cdf(10, 0, 1) > 0.999
    assert sw.normal_cdf(-10, 0, 1) < 0.001


def test_bucket_probability_sums_to_one():
    mu = 88.0
    # a full partition of the line must sum to 1 regardless of sigma
    buckets = [(None, 84), (84, 86), (86, 90), (90, 92), (92, None)]
    total = sum(sw.bucket_probability(mu, lo, hi) for lo, hi in buckets)
    assert abs(total - 1.0) < 1e-9
    # the wide bucket centered on the forecast is the mode
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
    # bucket 85-91 is ~50% under the model (sigma 4.5); priced there -> no edge
    market = {"ticker": "T-3", "subtitle": "85 to 91",
              "floor_strike": 85, "cap_strike": 91,
              "yes_ask": 52, "yes_bid": 48}
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


def test_held_tickers():
    from auto_trade import held_tickers
    positions = {"market_positions": [
        {"ticker": "A", "position": 5},
        {"ticker": "B", "position": 0},      # flat -> not held
        {"ticker": "C", "position": -3}]}    # short counts as held
    orders = [{"ticker": "D"}, {}]
    held = held_tickers(positions, orders)
    assert held == {"A", "C", "D"}


def test_position_exposure_cents_variants():
    from kalshi_exposure import _position_exposure_cents
    assert _position_exposure_cents({"market_exposure": 250}) == 250
    assert _position_exposure_cents({"market_exposure_dollars": "2.50"}) == 250
    assert _position_exposure_cents({"total_traded": 300}) == 300
    assert _position_exposure_cents({"total_traded_dollars": "3.00"}) == 300
    assert _position_exposure_cents({"position": 5}) is None  # unknown -> fail closed


def test_event_of_and_dynamic_caps():
    from auto_trade import event_of, dynamic_order_caps
    from dataclasses import dataclass

    assert event_of("KXHIGHNY-26JUL02-B99.5") == "KXHIGHNY-26JUL02"
    assert event_of("KXBTCD-26JUL0317-T59999.99") == "KXBTCD-26JUL0317"
    assert event_of("WEIRD") == "WEIRD"

    @dataclass
    class S:
        max_order_size: float = 2.0
        max_order_pct: float = 4.0
        min_order_pct: float = 1.0

    # $25 bankroll ($20 cash + $5 positions): 4% = $1.00, 1% = $0.25
    mx, mn = dynamic_order_caps(2000, 5.0, S())
    assert abs(mx - 1.0) < 1e-9 and abs(mn - 0.25) < 1e-9
    # large bankroll: absolute $2 ceiling still wins
    mx, _ = dynamic_order_caps(100_000, 0.0, S())
    assert mx == 2.0


def test_scoreboard_build(tmp_path):
    import scoreboard
    csv_file = tmp_path / "paper_trades.csv"
    csv_file.write_text(
        "scanned_at_utc,market_date,ticker,side,price_cents,model_prob,"
        "ev_cents,nws_forecast_f,outcome\n"
        "2026-07-02T14:00:00+00:00,2026-07-02,KXHIGHNY-26JUL02-B99.5,no,65,"
        "0.870,20.4,100.0,win (+35c)\n"
        "2026-07-02T14:00:00+00:00,2026-07-02,KXHIGHNY-26JUL02-T99,no,51,"
        "0.630,10.3,100.0,loss (-51c)\n"
        "2026-07-03T14:00:00+00:00,2026-07-03,KXHIGHCHI-26JUL03-B95.5,yes,40,"
        "0.700,15.0,91.0,\n"
    )
    out = tmp_path / "SCOREBOARD.md"
    orig = scoreboard.SOURCES
    scoreboard.SOURCES = [("🌡️ Weather model", csv_file)]
    try:
        scoreboard.build(out)
    finally:
        scoreboard.SOURCES = orig
    text = out.read_text()
    assert "🟢 1 W — 🔴 1 L — ⏳ 1 pending" in text
    assert "net **-16¢**" in text
    assert "🟢 **win (+35c)**" in text and "🔴 loss (-51c)" in text
