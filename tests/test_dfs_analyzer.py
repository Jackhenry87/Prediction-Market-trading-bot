"""Tests for the DFS +EV analyzer. Run: pytest tests/"""

import dfs_analyzer as da


def test_devig_and_implied():
    assert abs(da.implied_prob(2.0) - 0.5) < 1e-9
    assert abs(da.devig_two_way(1.9, 1.9) - 0.5) < 1e-9
    # favorite over: over 1.5, under 3.0 -> P(over) ~ 0.667
    assert abs(da.devig_two_way(1.5, 3.0) - (1/1.5)/((1/1.5)+(1/3.0))) < 1e-9


def test_breakeven_prob():
    # 2 legs at 3x -> each needs (1/3)^(1/2) ~ 0.577
    assert abs(da.breakeven_prob(3.0, 2) - (1/3) ** 0.5) < 1e-9
    # 3 legs at 5x -> (1/5)^(1/3)
    assert abs(da.breakeven_prob(5.0, 3) - (1/5) ** (1/3)) < 1e-9


def test_analyze_pick_plus_ev():
    # sharp says over is 67%, breakeven for 2-leg 3x is 57.7% -> +EV
    row = {"player": "X", "market": "points", "dfs_line": "25.5",
           "dfs_side": "over", "payout_mult": "3.0", "num_legs": "2",
           "sharp_line": "25.5", "sharp_over_odds": "1.5",
           "sharp_under_odds": "3.0", "fair_prob": ""}
    a = da.analyze_pick(row)
    assert a["status"] == "+EV" and a["edge"] > 0


def test_analyze_pick_minus_ev_and_missing():
    # fair 50%, breakeven 57.7% -> -EV
    row = {"player": "Y", "market": "pts", "dfs_line": "20", "dfs_side": "over",
           "payout_mult": "3.0", "num_legs": "2", "sharp_line": "20",
           "sharp_over_odds": "1.9", "sharp_under_odds": "1.9", "fair_prob": ""}
    assert da.analyze_pick(row)["status"] == "-EV"
    # no sharp reference at all
    bare = {"player": "Z", "market": "pts", "dfs_side": "over",
            "payout_mult": "3.0", "num_legs": "2"}
    assert da.analyze_pick(bare)["status"] == "no sharp reference"


def test_line_discrepancy_note():
    row = {"player": "X", "market": "points", "dfs_line": "25.5",
           "dfs_side": "over", "payout_mult": "3.0", "num_legs": "2",
           "sharp_line": "27.5", "sharp_over_odds": "1.9",
           "sharp_under_odds": "1.9", "fair_prob": ""}
    a = da.analyze_pick(row)
    assert "favor" in a["line_note"]   # over at a lower line than sharp = good


def test_build_writes_report(tmp_path):
    picks = tmp_path / "dfs_picks.csv"
    picks.write_text(
        "player,market,dfs_line,dfs_side,payout_mult,num_legs,sharp_line,"
        "sharp_over_odds,sharp_under_odds,fair_prob\n"
        "LeBron,points,25.5,over,3.0,2,25.5,1.5,3.0,\n")
    out = tmp_path / "DFS_ANALYSIS.md"
    assert da.build(picks, out) == 0
    text = out.read_text()
    assert "+EV" in text and "LeBron" in text
