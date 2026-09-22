"""Scoring the in-play recorder against settlements.

The recorder ran for six weeks unscored because live_scan.csv has no result
column. These tests protect the integrity of the verdict, which is the only
thing standing between a measured anti-edge and someone wiring it to an order
path: an unsettled market must never be counted as a loss, the fee must be
charged, and the slope of the edge curve must be reported with its real sign.
"""

import csv
import json

import live_score


def _row(price=50, edge=8.0, prob=0.60, ticker="T1"):
    return {"ticker": ticker, "price_cents": str(price),
            "edge_cents": str(edge), "model_prob": str(prob)}


# --- it must not be able to trade ------------------------------------------

def test_module_imports_no_order_path():
    src = open(live_score.__file__).read()
    for forbidden in ("create_limit_order", "log_execution", "check_order",
                      "size_position", "sports_runner", "favorite_runner"):
        assert forbidden not in src, f"live_score must not reference {forbidden}"


# --- the arithmetic ---------------------------------------------------------

def test_a_winner_pays_the_complement_less_the_fee():
    # 40c winner -> +60c gross, minus 0.07*0.4*0.6*100 = 1.68c
    assert round(live_score.pnl_cents(40, "yes"), 2) == round(60 - 1.68, 2)


def test_a_loser_costs_the_stake_plus_the_fee():
    assert round(live_score.pnl_cents(40, "no"), 2) == round(-40 - 1.68, 2)


def test_the_fee_is_largest_at_the_middle():
    assert live_score.taker_fee_cents(50) > live_score.taker_fee_cents(10)
    assert live_score.taker_fee_cents(50) > live_score.taker_fee_cents(90)


# --- an unsettled market is not a loss -------------------------------------

def test_unsettled_rows_are_excluded_not_counted_as_losses():
    # The bias that would matter most: treating "we don't know" as "we lost"
    # would manufacture exactly the negative result this file reports.
    rows = [_row(ticker="WIN"), _row(ticker="OPEN")]
    buckets = live_score.score(rows, {"WIN": "yes", "OPEN": ""})
    assert buckets[0]["n"] == 1 and buckets[0]["mean_cents"] > 0


def test_a_missing_ticker_is_excluded():
    assert live_score.score([_row(ticker="NOPE")], {})[0]["n"] == 0


def test_malformed_numbers_are_skipped_not_fatal():
    bad = {"ticker": "T1", "price_cents": "x", "edge_cents": "8",
           "model_prob": "0.6"}
    assert live_score.score([bad], {"T1": "yes"})[0]["n"] == 0


# --- the buckets are cumulative thresholds, and that is the point ----------

def test_higher_thresholds_select_subsets():
    rows = [_row(edge=1.0, ticker="A"), _row(edge=8.0, ticker="B"),
            _row(edge=16.0, ticker="C")]
    res = {"A": "yes", "B": "yes", "C": "yes"}
    by = {b["label"]: b["n"] for b in live_score.score(rows, res)}
    assert by["all comparisons"] == 3
    assert by["edge >= 7c (trade gate)"] == 2
    assert by["edge >= 15c"] == 1


# --- the verdict must report the SIGN of the slope ------------------------
#
# This is the finding the whole file exists to state. A model with real edge
# improves as the gate rises; one whose "edge" is its own error degrades. Both
# are unprofitable at the pooled level, so only the slope tells them apart.

def test_a_degrading_curve_is_called_anti_predictive():
    # low edge wins, high edge loses -> worse as the threshold rises
    rows = ([_row(edge=1.0, ticker=f"w{i}") for i in range(5)]
            + [_row(edge=16.0, ticker=f"l{i}") for i in range(5)])
    res = {f"w{i}": "yes" for i in range(5)}
    res.update({f"l{i}": "no" for i in range(5)})
    v = live_score.verdict(live_score.score(rows, res))
    assert "anti-predictive" in v and "Do not trade" in v


def test_an_improving_profitable_curve_is_called_tradeable():
    rows = ([_row(edge=1.0, ticker=f"l{i}") for i in range(5)]
            + [_row(edge=16.0, ticker=f"w{i}") for i in range(5)])
    res = {f"l{i}": "no" for i in range(5)}
    res.update({f"w{i}": "yes" for i in range(5)})
    v = live_score.verdict(live_score.score(rows, res))
    assert "small live test" in v


def test_an_improving_but_still_losing_curve_is_not_tradeable():
    # Improving is not enough — the strongest bucket must actually clear fees.
    rows = [_row(edge=1.0, price=95, ticker="a"),
            _row(edge=16.0, price=90, ticker="b")]
    v = live_score.verdict(live_score.score(rows, {"a": "no", "b": "no"}))
    assert "do not trade" in v.lower()


def test_too_little_data_does_not_produce_a_verdict():
    assert "Not enough" in live_score.verdict([{"label": "x", "n": 0}])


# --- calibration comparison ------------------------------------------------

def test_brier_compares_model_against_the_price():
    # model says 90%, price says 50%, outcome yes -> model is better here
    b = live_score.score([_row(price=50, prob=0.90)], {"T1": "yes"})[0]
    assert b["brier_model"] < b["brier_market"]


# --- the report ------------------------------------------------------------

def test_render_marks_empty_buckets_and_keeps_the_verdict():
    out = live_score.render(live_score.score([], {}))
    assert "| 0 |" in out and "Not enough" in out


def test_render_reports_unresolved_rows_explicitly():
    out = live_score.render(live_score.score([_row()], {"T1": "yes"}),
                            unresolved=7)
    assert "7 comparison(s) not yet settled" in out


# --- the settlement cache --------------------------------------------------

def test_cache_round_trips(tmp_path):
    p = tmp_path / "r.json"
    live_score.save_results({"A": "yes"}, p)
    assert live_score.load_results(p) == {"A": "yes"}


def test_a_corrupt_cache_is_ignored_not_fatal(tmp_path):
    p = tmp_path / "r.json"
    p.write_text("{not json")
    assert live_score.load_results(p) == {}
    assert live_score.load_results(tmp_path / "absent.json") == {}


def test_fetch_only_requests_the_unknown_ones():
    asked = []

    class _C:
        def _request(self, method, path, params=None, body=None):
            asked.append(path)
            return {"market": {"result": "no"}}

    got = live_score.fetch_results(_C(), ["A", "B"], {"A": "yes"})
    assert got == {"A": "yes", "B": "no"}
    assert asked == ["/markets/B"]


def test_a_failed_lookup_leaves_the_row_unscored():
    class _C:
        def _request(self, method, path, params=None, body=None):
            raise RuntimeError("boom")

    assert live_score.fetch_results(_C(), ["A"], {}) == {"A": ""}
