"""Tests for within-Kalshi risk-free basket detection. This is the money-
critical logic — it must FAIL CLOSED on anything it can't prove risk-free."""

import kalshi_arb


def _event(mutually_exclusive=True, **kw):
    return dict(event_ticker="E1", title="Test ladder",
                mutually_exclusive=mutually_exclusive, **kw)


def _mkt(ticker, yes_ask, status="active", yes_bid=None):
    return {"ticker": ticker, "yes_ask": yes_ask, "yes_bid": yes_bid,
            "status": status}


def test_arb_detected_when_basket_below_dollar():
    # 3 legs at 30/30/32 = 92c + small fees -> guaranteed ~5c (buy YES)
    markets = [_mkt("A", 30), _mkt("B", 30), _mkt("C", 32)]
    arb = kalshi_arb.evaluate_event(_event(), markets)
    assert arb is not None and arb["side"] == "yes"
    assert arb["n"] == 3 and arb["cost_cents"] == 92
    assert arb["profit_cents"] > 2
    assert all(side == "yes" for _, _, side in arb["legs"])


def test_no_basket_arb_when_bids_rich():
    # yes_bids 55/55 sum 110 > 100 -> buy NO on both; pays (2-1)*100=100c,
    # cost = (100-55)*2 = 90c, profit ~ 110-100-fees ~ +7c
    markets = [_mkt("A", 60, yes_bid=55), _mkt("B", 60, yes_bid=55)]
    arb = kalshi_arb.evaluate_event(_event(), markets)
    assert arb is not None and arb["side"] == "no"
    assert arb["payout_cents"] == 100 and arb["profit_cents"] > 2
    assert all(side == "no" for _, _, side in arb["legs"])


def test_picks_the_more_profitable_side():
    # YES ask-sum 80 -> ~+16c; NO bid-sum 116 -> ~+12c. Both qualify; the
    # richer YES side is returned.
    markets = [_mkt("A", 40, yes_bid=58), _mkt("B", 40, yes_bid=58)]
    arb = kalshi_arb.evaluate_event(_event(), markets)
    assert arb is not None and arb["side"] == "yes"
    assert arb["profit_cents"] > 10


def test_not_mutually_exclusive_never_arbs():
    # cheap basket but not a proven MECE ladder -> must skip (could all lose)
    markets = [_mkt("A", 10), _mkt("B", 10)]
    assert kalshi_arb.evaluate_event(_event(mutually_exclusive=False),
                                     markets) is None


def test_unquoted_leg_fails_closed():
    # one leg has no ask -> basket incomplete -> skip even though others cheap
    markets = [_mkt("A", 20), _mkt("B", 20), _mkt("C", None)]
    assert kalshi_arb.evaluate_event(_event(), markets) is None


def test_closed_leg_fails_closed():
    markets = [_mkt("A", 30), _mkt("B", 30, status="settled")]
    assert kalshi_arb.evaluate_event(_event(), markets) is None


def test_no_arb_when_sum_at_or_above_dollar():
    markets = [_mkt("A", 50), _mkt("B", 51)]        # 101c -> negative
    assert kalshi_arb.evaluate_event(_event(), markets) is None


def test_profit_below_buffer_skipped():
    # 49/50 = 99c, ~1c gross, fees push it under the 2c floor -> skip
    markets = [_mkt("A", 49), _mkt("B", 50)]
    assert kalshi_arb.evaluate_event(_event(), markets) is None


def test_single_leg_is_not_a_basket():
    assert kalshi_arb.evaluate_event(_event(), [_mkt("A", 10)]) is None
