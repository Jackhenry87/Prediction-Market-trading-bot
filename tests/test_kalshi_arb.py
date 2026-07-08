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


# ---------- depth-aware sizing ----------
class _FakeBook:
    """A client stub returning canned order books per ticker."""
    def __init__(self, books):
        self.books = books

    def get_orderbook(self, ticker, depth=10):
        return self.books[ticker]


def _yes_arb(tickers):
    # a YES basket over the given tickers (top-of-book prices unused by sizing)
    return dict(event_ticker="E1", title="t", side="yes", n=len(tickers),
                legs=[(t, 30, "yes") for t in tickers])


def test_sizing_capped_by_thinnest_leg_depth():
    # Buying YES matches resting NO orders: yes_price = 100 - no_price.
    # Leg A: 100 NO @ 70  -> 100 YES @ 30.   Leg B: only 5 NO @ 70 -> 5 YES @ 30.
    # basket 30+30=60c -> ~+38c/ea, but B caps size at 5.
    client = _FakeBook({
        "A": {"no": [[70, 100]], "yes": []},
        "B": {"no": [[70, 5]], "yes": []},
    })
    sized = kalshi_arb.size_basket(client, _yes_arb(["A", "B"]), 100000,
                                   max_pct=100, reserve_usd=0, buffer_cents=2)
    assert sized["count"] == 5 and sized["side"] == "yes"
    assert sized["profit_cents"] > 2


def test_sizing_stops_when_avg_fill_kills_the_edge():
    # Leg A cheap-then-expensive: 3 YES @ 30, then 1000 YES @ 49.
    # Leg B: 1000 YES @ 30. Taking >3 walks A's avg up until basket <2c edge.
    client = _FakeBook({
        "A": {"no": [[70, 3], [51, 1000]], "yes": []},
        "B": {"no": [[70, 1000]], "yes": []},
    })
    sized = kalshi_arb.size_basket(client, _yes_arb(["A", "B"]), 100000,
                                   max_pct=100, reserve_usd=0, buffer_cents=2)
    # at n=3 avg A=30 (basket ~+38); by n=4 avg A=(3*30+49)/4=34.75, basket
    # 34.75+30=64.75 -> still positive; edge only dies once A's avg climbs.
    assert sized["count"] >= 3
    econ = kalshi_arb.basket_econ(
        [[(30, 3), (49, 1000)], [(30, 1000)]], "yes", 2, sized["count"])
    assert econ[1] >= 2                       # profit/contract still clears buffer


def test_reserve_and_pct_caps_limit_spend():
    client = _FakeBook({
        "A": {"no": [[70, 1000]], "yes": []},
        "B": {"no": [[70, 1000]], "yes": []},
    })
    arb = _yes_arb(["A", "B"])
    # balance $10 (1000c); 60c per basket-contract. Reserve $9 -> only 100c
    # spendable -> at most 1 contract.
    sized = kalshi_arb.size_basket(client, arb, 1000, max_pct=100,
                                   reserve_usd=9, buffer_cents=2)
    assert sized["count"] == 1
    # 50% cap of $10 = 500c -> ~8 contracts (8*60=480<=500)
    sized2 = kalshi_arb.size_basket(client, arb, 1000, max_pct=50,
                                    reserve_usd=0, buffer_cents=2)
    assert sized2["count"] == 8


def test_no_size_when_book_empty():
    client = _FakeBook({"A": {"no": [], "yes": []}, "B": {"no": [[70, 5]]}})
    assert kalshi_arb.size_basket(client, _yes_arb(["A", "B"]), 100000,
                                  buffer_cents=2) is None
