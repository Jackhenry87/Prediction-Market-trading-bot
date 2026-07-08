"""Tests for the player-props model: OddsBlaze parsing, devig, value."""

import props_model as pm


def test_norm_name_strips_accents_suffix_punct():
    assert pm.norm_name("José  Ramírez Jr.") == "jose ramirez"
    assert pm.norm_name("Ronald Acuña II") == "ronald acuna"
    assert pm.norm_name("Shohei Ohtani") == "shohei ohtani"


def test_american_to_decimal():
    assert abs(pm.american_to_decimal("+100") - 2.0) < 1e-9
    assert abs(pm.american_to_decimal("-137") - 1.7299) < 1e-3
    assert abs(pm.american_to_decimal(-200) - 1.5) < 1e-9
    assert pm.american_to_decimal(0) is None
    assert pm.american_to_decimal("x") is None


def _book(sportsbook, judge_over, judge_under, line=1.5):
    """One OddsBlaze payload: Aaron Judge Total Bases over/under at `line`."""
    def odd(side, price):
        return {"market": "Player Total Bases",
                "name": f"Aaron Judge {side} {line}", "price": price,
                "selection": {"name": "Aaron Judge", "side": side, "line": line}}
    return {"sportsbook": {"id": sportsbook},
            "events": [{"odds": [odd("Over", judge_over),
                                 odd("Under", judge_under),
                                 # a non-two-way market that must be ignored
                                 {"market": "1st PA Result 8-Way",
                                  "name": "Single", "price": "+400",
                                  "selection": {"name": "x"}}]}]}


def _book_stat(sportsbook, market, line, over, under, player="Aaron Judge"):
    """One OddsBlaze payload for an arbitrary market/line."""
    def odd(side, price):
        return {"market": market, "name": f"{player} {side} {line}",
                "price": price,
                "selection": {"name": player, "side": side, "line": line}}
    return {"sportsbook": {"id": sportsbook},
            "events": [{"odds": [odd("Over", over), odd("Under", under)]}]}


def test_parse_book_two_way_only():
    q = pm.parse_book(_book("draftkings", "-110", "-110"))
    key = ("aaron judge", "Player Total Bases", 1.5)
    assert key in q
    assert "over" in q[key] and "under" in q[key]
    # the 8-way junk market was skipped
    assert len(q) == 1


def test_board_lines_needs_both_sides():
    lines = pm.board_lines(_book("prizepicks", "-137", "-137"))
    assert len(lines) == 1
    d = lines[0]
    assert d["player"] == "Aaron Judge" and d["market"] == "Player Total Bases"
    assert d["display_stat"] == "Total Bases" and d["line"] == 1.5
    # a payload missing the under side yields no bettable line
    half = _book("prizepicks", "-137", "-137")
    half["events"][0]["odds"] = half["events"][0]["odds"][:1]  # over only
    assert pm.board_lines(half) == []


def test_sharp_consensus_averages_books():
    # three books all ~ even money -> fair ~0.5 over, counts 3 books
    payloads = {b: _book(b, "-110", "-110")
                for b in ("draftkings", "betmgm", "caesars")}
    fair = pm.sharp_consensus(payloads)
    key = ("aaron judge", "Player Total Bases", 1.5)
    assert key in fair
    assert 0.45 < fair[key]["p"] < 0.55 and fair[key]["books"] == 3


def test_sharp_means_backs_out_the_mean():
    # both books quote strikeouts 5.5 at even money -> P(over)=0.5 -> mean=5.5
    payloads = {b: _book_stat(b, "Player Strikeouts", 5.5, "-110", "-110")
                for b in ("draftkings", "betmgm")}
    means = pm.sharp_means(payloads)
    m = means[("aaron judge", "Player Strikeouts")]
    assert abs(m["mean"] - 5.5) < 0.05 and m["books"] == 2


def test_interpolation_finds_stale_line_edge():
    # Sharp mean is 6.5 Ks (books quote 6.5). PrizePicks posts 5.5 -> "over 5.5"
    # is really ~68%, and at 1.73 that's a real +EV over the exact model missed.
    sharp = {b: _book_stat(b, "Player Strikeouts", 6.5, "-110", "-110")
             for b in ("draftkings", "betmgm")}
    means = pm.sharp_means(sharp)
    dfs = [dict(player="Aaron Judge", player_norm="aaron judge",
                display_stat="Strikeouts", market="Player Strikeouts",
                line=5.5, over_decimal=1.73, under_decimal=1.73,
                title="x", source="prizepicks")]
    # no exact 5.5 quote in `fair`, so only interpolation can catch it
    picks = pm.find_value(dfs, {}, means, min_edge_pct=6)
    assert len(picks) == 1
    assert picks[0]["side"] == "over" and picks[0]["priced_by"] == "model"
    assert picks[0]["edge_pct"] > 6
    # and with interpolation OFF (means=None) the edge is invisible
    assert pm.find_value(dfs, {}, None, min_edge_pct=6) == []


def test_interpolation_respects_offset_gate():
    # PrizePicks line 12.5 is way beyond MAX_OFFSET_SIGMA of a 6.5 mean -> skip
    sharp = {b: _book_stat(b, "Player Strikeouts", 6.5, "-110", "-110")
             for b in ("draftkings", "betmgm")}
    means = pm.sharp_means(sharp)
    dfs = [dict(player="Aaron Judge", player_norm="aaron judge",
                display_stat="Strikeouts", market="Player Strikeouts",
                line=12.5, over_decimal=1.73, under_decimal=1.73,
                title="x", source="prizepicks")]
    assert pm.find_value(dfs, {}, means, min_edge_pct=6) == []


def test_find_value_takes_edge_and_skips_thin():
    dfs = pm.board_lines(_book("prizepicks", "-137", "-137"))  # 1.73 each side
    key = ("aaron judge", "Player Total Bases", 1.5)
    # sharp 65% over vs 1.73 payout: EV = 0.65*1.73-1 = +0.12 -> value over
    picks = pm.find_value(dfs, {key: {"p": 0.65, "books": 3}}, min_edge_pct=6)
    assert len(picks) == 1 and picks[0]["side"] == "over"
    assert picks[0]["edge_pct"] > 6
    # coin-flip 52% -> both EV under the floor -> no pick
    assert pm.find_value(dfs, {key: {"p": 0.52, "books": 3}},
                         min_edge_pct=6) == []
    # juicy edge but only 1 book agrees -> skipped
    assert pm.find_value(dfs, {key: {"p": 0.80, "books": 1}},
                         min_edge_pct=6) == []
