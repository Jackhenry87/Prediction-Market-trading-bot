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


# ---------- the maker side of the same ladders ----------
#
# The full sweep on 2026-08-21 found 2 takeable baskets in 3,609 quoted
# mutually-exclusive ladders, and every daily-resolving ladder priced ABOVE par
# at the ask. restable_basket measures the other side of that: what the same $1
# would cost if you posted rather than took. It is a measurement, not a signal —
# these tests pin the arithmetic and the fail-closed guards, not a trade.

def _lad(quotes, exhaustive=True):
    """quotes: list of (yes_bid, yes_ask). Numeric strikes so the ladder is
    provably exhaustive unless told otherwise."""
    out = []
    for i, (b, a) in enumerate(quotes):
        st = ("less" if i == 0 else
              "greater" if i == len(quotes) - 1 else "between")
        m = {"ticker": f"T{i}", "status": "active", "yes_bid": b, "yes_ask": a,
             "strike_type": st if exhaustive else "structured"}
        if st == "between":
            m["floor_strike"], m["cap_strike"] = i, i + 1
        out.append(m)
    return out


EV = {"event_ticker": "E1", "title": "t", "mutually_exclusive": True}


def test_posts_one_tick_inside_each_bid():
    # bids 40/30/20 -> post 41/31/21 = 93c for a guaranteed 100c.
    r = kalshi_arb.restable_basket(EV, _lad([(40, 45), (30, 35), (20, 25)]))
    assert [round(p) for _, p, _ in r["legs"]] == [41, 31, 21]
    assert r["cost_cents"] == 93 and r["profit_cents"] == 7
    assert r["fees_cents"] == 0.0            # Kalshi charges no maker fee
    assert r["side"] == "yes-maker"


def test_a_post_never_crosses_the_ask():
    # A 1c-wide leg cannot be improved: posting bid+1 would be the ask, which
    # is taking, not making. Cap at the ask so the cost stays honest.
    r = kalshi_arb.restable_basket(EV, _lad([(40, 41), (30, 35), (20, 25)]))
    assert [round(p) for _, p, _ in r["legs"]] == [41, 31, 21]


def test_a_leg_with_no_bid_still_costs_the_minimum_tick():
    # The structural tax that makes same-day temperature ladders unmakeable:
    # 1c is the exchange minimum, so five worthless legs cost 5c to complete
    # even though they are worth nothing.
    r = kalshi_arb.restable_basket(
        EV, _lad([(0, 1), (0, 1), (0, 1), (0, 1), (0, 1), (95, 99)]))
    assert r["cost_cents"] == 5 + 96         # five dead legs + the live one
    assert r["profit_cents"] == -1           # ...which is 1c OVER par: a loss
    assert r["dead_legs"] == 5


def test_a_ladder_of_stub_quotes_is_rejected():
    # Six legs each showing a 99c ask sum to 594c: that is not an expensive
    # market, it is an unquoted one. Its empty bid side would otherwise price a
    # "6c basket for a guaranteed $1", the most seductive wrong answer here.
    assert kalshi_arb.restable_basket(EV, _lad([(0, 99)] * 6)) is None


def test_the_wellformed_ceiling_is_the_boundary(monkeypatch):
    monkeypatch.setattr(kalshi_arb, "ARB_WELLFORMED_MAX_CENTS", 110.0)
    assert kalshi_arb.restable_basket(EV, _lad([(40, 55), (30, 55)])) is not None
    assert kalshi_arb.restable_basket(EV, _lad([(40, 56), (30, 55)])) is None


def test_the_take_cost_is_recorded_alongside():
    # The comparison is the point: a basket can be cheap to post and a
    # guaranteed loss to take, which is the normal case on this exchange.
    r = kalshi_arb.restable_basket(EV, _lad([(40, 55), (30, 50)]))
    assert r["take_cost_cents"] == 105 and r["cost_cents"] == 72


# --- it must fail closed on everything it cannot prove ---------------------

def test_a_non_exhaustive_ladder_is_rejected():
    # Same reason the YES arb is gated on exhaustiveness: an untradeable "none
    # of the above" outcome means the basket can pay nothing at all.
    assert kalshi_arb.restable_basket(
        EV, _lad([(40, 45), (30, 35)], exhaustive=False)) is None


def test_a_non_mutually_exclusive_event_is_rejected():
    assert kalshi_arb.restable_basket(
        {"event_ticker": "E1", "mutually_exclusive": False},
        _lad([(40, 45), (30, 35)])) is None


def test_an_unquoted_or_closed_leg_is_rejected():
    lad = _lad([(40, 45), (30, 35)])
    lad[1]["yes_ask"] = None
    assert kalshi_arb.restable_basket(EV, lad) is None
    lad = _lad([(40, 45), (30, 35)])
    lad[1]["status"] = "closed"
    assert kalshi_arb.restable_basket(EV, lad) is None


def test_a_single_leg_is_not_a_basket():
    assert kalshi_arb.restable_basket(EV, _lad([(40, 45)])) is None
