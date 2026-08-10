"""Live model: state parsing, market matching, edge, and the recorder.

The live model is the one most exposed to a bad input: it prices off a scraped
scoreboard, and a stale or misparsed state produces a large confident edge
rather than an obvious error. Most of these tests are about refusing.
"""

import csv

import live_scan
import live_state
import strategy_live


# --- ESPN parsing -----------------------------------------------------------

def test_parses_half_and_inning_from_the_detail_string():
    assert live_state.parse_half("Bottom 2nd") == "bottom"
    assert live_state.parse_half("Top 10th") == "top"
    assert live_state.parse_inning("Bottom 2nd") == 2
    assert live_state.parse_inning("Top 10th") == 10


def test_between_innings_states_are_handled():
    # 'Mid 3rd' = between top and bottom; 'End 7th' = inning over.
    assert live_state.parse_half("Mid 3rd") == "bottom"
    assert live_state.parse_half("End 7th") == "top"


def test_unparseable_detail_falls_back_rather_than_raising():
    assert live_state.parse_half("") == "top"
    assert live_state.parse_inning("nonsense", fallback=4) == 4


def _event(state="in", detail="Bottom 2nd", outs=1, home=1, away=2):
    return {"id": "1", "shortName": "HOU @ SD",
            "status": {"period": 2, "type": {"state": state, "detail": detail}},
            "competitions": [{
                "situation": {"outs": outs, "onFirst": True,
                              "onSecond": False, "onThird": False},
                "competitors": [
                    {"homeAway": "home", "score": str(home),
                     "team": {"displayName": "San Diego Padres",
                              "abbreviation": "SD"}},
                    {"homeAway": "away", "score": str(away),
                     "team": {"displayName": "Houston Astros",
                              "abbreviation": "HOU"}}]}]}


def test_parses_a_full_event():
    g = live_state.parse_event(_event())
    assert g["home_score"] == 1 and g["away_score"] == 2
    assert g["inning"] == 2 and g["half"] == "bottom" and g["outs"] == 1
    assert g["on_first"] is True and g["on_third"] is False


def test_a_malformed_event_is_dropped_not_raised():
    assert live_state.parse_event({}) is None
    assert live_state.parse_event({"competitions": [{"competitors": []}]}) is None


def test_a_missing_score_reads_as_zero_not_a_crash():
    ev = _event()
    ev["competitions"][0]["competitors"][0]["score"] = None
    assert live_state.parse_event(ev)["home_score"] == 0


# --- market matching --------------------------------------------------------

GAME = dict(short="HOU @ SD", detail="Bottom 2nd", inning=2, half="bottom",
            outs=1, home_team="San Diego Padres", away_team="Houston Astros",
            home_score=1, away_score=2, on_first=True, on_second=False,
            on_third=False, state="in")


def test_matches_each_side_to_the_right_team():
    assert strategy_live.match_market({"yes_sub_title": "San Diego"}, GAME) == "home"
    assert strategy_live.match_market({"yes_sub_title": "Houston"}, GAME) == "away"


def test_an_unmatched_label_is_refused():
    assert strategy_live.match_market({"yes_sub_title": "Toronto"}, GAME) is None
    assert strategy_live.match_market({"yes_sub_title": ""}, GAME) is None


# --- edge -------------------------------------------------------------------

def _mkt(ask, sub="Houston", ticker="KXMLBGAME-X-HOU"):
    return {"ticker": ticker, "yes_sub_title": sub, "yes_ask": ask,
            "status": "active"}


def test_edge_is_net_of_the_taker_fee():
    # p(away)=0.58 -> 58c fair. At a 55c ask the gross gap is 3c and the fee
    # near 1.7c, so the recorded edge must be well under 3c.
    r = strategy_live.evaluate(_mkt(55), GAME, p_home=0.42)
    assert 0 < r["ev_cents"] < 3.0


def test_a_fairly_priced_market_shows_no_edge():
    r = strategy_live.evaluate(_mkt(58), GAME, p_home=0.42)
    assert r["ev_cents"] < 0


def test_decided_games_are_skipped_at_both_ends():
    # 95% and 3% are where a misparse costs most and there is least to win.
    assert strategy_live.evaluate(_mkt(95), GAME, p_home=0.03) is None
    assert strategy_live.evaluate(_mkt(95), GAME, p_home=0.97) is None


def test_the_tradeable_flag_needs_both_a_floor_and_a_ceiling():
    small = strategy_live.evaluate(_mkt(57), GAME, p_home=0.42)
    assert small["tradeable"] is False            # real but under the gate
    # A 30c disagreement in play is a stale scoreboard, not an opportunity.
    huge = strategy_live.evaluate(_mkt(30), GAME, p_home=0.10)
    assert huge is None or huge["tradeable"] is False


def test_a_missing_quote_is_not_a_signal():
    assert strategy_live.evaluate(_mkt(None), GAME, p_home=0.42) is None
    assert strategy_live.evaluate(_mkt(0), GAME, p_home=0.42) is None


# --- the recorder must not be able to trade --------------------------------

def test_scanner_has_no_order_path():
    src = open(live_scan.__file__).read()
    for forbidden in ("create_limit_order", "log_execution", "check_order",
                      "size_position", "sports_runner"):
        assert forbidden not in src, f"live_scan must not reference {forbidden}"


def test_records_every_comparison_not_only_the_tradeable_ones(tmp_path):
    # Filtering to the tail before measuring the distribution would hide
    # exactly what these first weeks exist to find out.
    p = tmp_path / "live.csv"
    rows = [dict(game="A", ev_cents=-4.0, tradeable=False, price_cents=55,
                 model_prob=0.5, p_home=0.5),
            dict(game="B", ev_cents=9.0, tradeable=True, price_cents=40,
                 model_prob=0.5, p_home=0.5)]
    assert live_scan.record(rows, path=p, now="t") == 2
    assert len(list(csv.DictReader(open(p)))) == 2


def test_header_written_once(tmp_path):
    p = tmp_path / "live.csv"
    row = [dict(game="A", ev_cents=1.0, tradeable=False, price_cents=50,
                model_prob=0.5, p_home=0.5)]
    live_scan.record(row, path=p, now="t1")
    live_scan.record(row, path=p, now="t2")
    lines = [l for l in open(p).read().splitlines() if l.strip()]
    assert len(lines) == 3 and lines[0].startswith("scanned_at_utc")


def test_an_empty_scan_writes_nothing(tmp_path):
    p = tmp_path / "live.csv"
    assert live_scan.record([], path=p) == 0
    assert not p.exists()


def test_summary_before_anything_is_recorded(tmp_path):
    assert "no live comparisons" in live_scan.summarise(tmp_path / "none.csv")


def test_summary_reports_how_often_the_edge_beat_fees(tmp_path):
    p = tmp_path / "live.csv"
    live_scan.record([
        dict(game="A", ev_cents=2.0, tradeable=False, price_cents=50,
             model_prob=0.5, p_home=0.5),
        dict(game="B", ev_cents=-3.0, tradeable=False, price_cents=50,
             model_prob=0.5, p_home=0.5),
    ], path=p, now="t")
    s = live_scan.summarise(p)
    assert "2 comparisons" in s and "50.0%" in s and "+2.0c" in s


def test_summary_survives_a_malformed_row(tmp_path):
    p = tmp_path / "live.csv"
    live_scan.record([dict(game="A", ev_cents=4.0, tradeable=False,
                           price_cents=50, model_prob=0.5, p_home=0.5)],
                     path=p, now="t")
    with open(p, "a") as fh:
        fh.write("t,B,d,1,top,0-0,T,home,x,y,z,NOT_A_NUMBER,no\n")
    assert "comparisons" in live_scan.summarise(p)
