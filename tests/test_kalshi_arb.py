"""Tests for within-Kalshi risk-free basket detection. This is the money-
critical logic — it must FAIL CLOSED on anything it can't prove risk-free."""

import kalshi_arb


def _event(mutually_exclusive=True, **kw):
    return dict(event_ticker="E1", title="Test ladder",
                mutually_exclusive=mutually_exclusive, **kw)


def _ladder(asks, bids=None, status=None):
    """A NUMERIC MECE partition (a 'less' bottom tail + 'between' buckets + a
    'greater' top tail) — the only provably-exhaustive shape the scanner trades.
    asks[i] is each leg's yes_ask (None to leave a leg unquoted)."""
    n = len(asks)
    mk = []
    for i, a in enumerate(asks):
        if i == 0:
            st, floor, cap = "less", None, 70
        elif i == n - 1:
            st, floor, cap = "greater", 70 + n, None
        else:
            st, floor, cap = "between", 70 + i, 70 + i + 1
        m = {"ticker": chr(65 + i), "yes_ask": a, "strike_type": st,
             "floor_strike": floor, "cap_strike": cap,
             "status": (status[i] if status else "active")}
        if bids is not None:
            m["yes_bid"] = bids[i]
        mk.append(m)
    return mk


def _categorical(asks):
    """Mutually-exclusive but CATEGORICAL (candidate names, no numeric strike) —
    NOT collectively exhaustive; must be rejected."""
    return [{"ticker": chr(65 + i), "yes_ask": a, "status": "active"}
            for i, a in enumerate(asks)]


def test_arb_detected_when_basket_below_dollar():
    # 3-leg numeric ladder at 30/30/32 = 92c -> ~+3.5c after fees (buy YES)
    arb = kalshi_arb.evaluate_event(_event(), _ladder([30, 30, 32]))
    assert arb is not None and arb["side"] == "yes"
    assert arb["n"] == 3 and arb["cost_cents"] == 92
    assert 2 <= arb["profit_cents"] <= 7
    assert all(side == "yes" for _, _, side in arb["legs"])


def test_no_basket_arb_when_bids_rich():
    # yes_bids 55/55 sum 110 > 100 -> buy NO on both; pays (2-1)*100=100c,
    # cost 90c -> ~+6.5c after fees
    arb = kalshi_arb.evaluate_event(_event(), _ladder([60, 60], bids=[55, 55]))
    assert arb is not None and arb["side"] == "no"
    assert arb["payout_cents"] == 100 and 2 <= arb["profit_cents"] <= 7
    assert all(side == "no" for _, _, side in arb["legs"])


def test_picks_the_more_profitable_side():
    # YES ask 46/46 -> ~+4.5c; NO bid 53/53 -> ~+2.5c. Both within the cap; the
    # richer YES side is returned.
    arb = kalshi_arb.evaluate_event(_event(), _ladder([46, 46], bids=[53, 53]))
    assert arb is not None and arb["side"] == "yes"
    assert arb["profit_cents"] > 3


def test_categorical_field_is_rejected():
    # a 'who wins' field (no numeric strikes) is NOT provably exhaustive even if
    # it sums below $1 -> the YES basket must be rejected (the field candidate
    # could win). Only yes_asks quoted here, so no NO basket to fall back on.
    assert kalshi_arb.evaluate_event(_event(), _categorical([46, 46])) is None


def test_no_basket_allowed_on_categorical_mece_event():
    # A categorical (non-numeric) at-most-one-YES field with rich bids: the NO
    # basket is risk-free WITHOUT exhaustiveness (>= n-1 legs pay $1; if none
    # win, all pay), so it must be allowed even though the YES side is gated.
    mk = [{"ticker": "A", "yes_bid": 54, "status": "active"},
          {"ticker": "B", "yes_bid": 54, "status": "active"}]   # sum 108 > 100
    arb = kalshi_arb.evaluate_event(_event(), mk)
    assert arb is not None and arb["side"] == "no"
    assert 2 <= arb["profit_cents"] <= 7
    # ...but the YES basket on the same categorical field is still rejected.
    mk_yes = [{"ticker": "A", "yes_ask": 46, "status": "active"},
              {"ticker": "B", "yes_ask": 46, "status": "active"}]
    assert kalshi_arb.evaluate_event(_event(), mk_yes) is None


def test_not_mutually_exclusive_never_arbs():
    assert kalshi_arb.evaluate_event(_event(mutually_exclusive=False),
                                     _ladder([30, 30])) is None


def test_unquoted_leg_fails_closed():
    # one leg has no ask -> basket incomplete -> skip even though others cheap
    assert kalshi_arb.evaluate_event(_event(), _ladder([20, 20, None])) is None


def test_closed_leg_fails_closed():
    assert kalshi_arb.evaluate_event(
        _event(), _ladder([30, 30], status=["active", "settled"])) is None


def test_no_arb_when_sum_at_or_above_dollar():
    assert kalshi_arb.evaluate_event(_event(), _ladder([50, 51])) is None


def test_profit_below_buffer_skipped():
    # 49/50 = 99c, ~1c gross, fees push it under the 2c floor -> skip
    assert kalshi_arb.evaluate_event(_event(), _ladder([49, 50])) is None


def test_single_leg_is_not_a_basket():
    assert kalshi_arb.evaluate_event(_event(), _ladder([10])) is None


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


def test_size_basket_sets_marginal_limit_prices():
    # Leg A: 3 YES @ 30 then 1000 @ 50; Leg B: 1000 YES @ 30. Sizing walks A
    # past its 30c top level, so the placement limit for A must be the MARGINAL
    # (worst) level consumed — 50 — not the stale 30c top-of-book (which would
    # under-fill). B never leaves its single level, so its limit stays 30.
    client = _FakeBook({
        "A": {"no": [[70, 3], [50, 1000]], "yes": []},
        "B": {"no": [[70, 1000]], "yes": []},
    })
    sized = kalshi_arb.size_basket(client, _yes_arb(["A", "B"]), 100000,
                                   max_pct=100, reserve_usd=0, buffer_cents=2)
    limits = {t: p for t, p, _ in sized["legs"]}
    assert sized["count"] == 1000
    assert limits["A"] == 50 and limits["B"] == 30


def test_no_size_when_book_empty():
    client = _FakeBook({"A": {"no": [], "yes": []}, "B": {"no": [[70, 5]]}})
    assert kalshi_arb.size_basket(client, _yes_arb(["A", "B"]), 100000,
                                  buffer_cents=2) is None


def test_huge_profit_is_capped_even_on_a_numeric_ladder():
    # a numeric ladder summing to 15c would claim +85c — implausible for a
    # liquid exhaustive market, so the max-profit cap rejects it too.
    assert kalshi_arb.evaluate_event(_event(), _ladder([5, 5, 5])) is None


def test_plausible_small_arb_still_passes():
    # a real, exhaustive numeric ladder a few cents under par -> genuine arb
    arb = kalshi_arb.evaluate_event(_event(), _ladder([31, 31, 31]))   # 93c
    assert arb is not None and 2 <= arb["profit_cents"] <= 7
