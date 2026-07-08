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
    # it sums below $1 -> must be rejected (the field candidate could win).
    assert kalshi_arb.evaluate_event(_event(), _categorical([30, 30, 32])) is None


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
    assert kalshi_arb.evaluate_event(_event(), [_mkt("A", 10)]) is None
