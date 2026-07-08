"""Tests for the player-props model: Underdog parsing, sharp devig, value."""

import props_model as pm


def test_norm_name_strips_accents_suffix_punct():
    assert pm.norm_name("José  Ramírez Jr.") == "jose ramirez"
    assert pm.norm_name("Ronald Acuña II") == "ronald acuna"
    assert pm.norm_name("Shohei Ohtani") == "shohei ohtani"


UNDERDOG_FIXTURE = {
    "players": [
        {"id": "p1", "first_name": "Aaron", "last_name": "Judge",
         "sport_id": "MLB"},
        {"id": "p2", "first_name": "Kylian", "last_name": "Mbappe",
         "sport_id": "FIFA"},
    ],
    "appearances": [
        {"id": "a1", "player_id": "p1"},
        {"id": "a2", "player_id": "p2"},
    ],
    "over_under_lines": [
        {   # MLB, mapped stat, active -> should parse
            "id": "l1", "status": "active", "stat_value": "1.5",
            "over_under": {"category": "player_prop",
                           "title": "Aaron Judge Total Bases O/U",
                           "appearance_stat": {"appearance_id": "a1",
                                               "display_stat": "Total Bases"}},
            "options": [
                {"choice": "higher", "status": "active", "decimal_price": "1.80"},
                {"choice": "lower", "status": "active", "decimal_price": "1.95"},
            ],
        },
        {   # soccer -> filtered out for MLB
            "id": "l2", "status": "active", "stat_value": "1.5",
            "over_under": {"category": "player_prop",
                           "title": "Mbappe Shots O/U",
                           "appearance_stat": {"appearance_id": "a2",
                                               "display_stat": "Shots on Target"}},
            "options": [
                {"choice": "higher", "status": "active", "decimal_price": "1.5"},
                {"choice": "lower", "status": "active", "decimal_price": "2.5"},
            ],
        },
        {   # suspended line -> skipped
            "id": "l3", "status": "suspended", "stat_value": "0.5",
            "over_under": {"category": "player_prop",
                           "title": "Aaron Judge HR O/U",
                           "appearance_stat": {"appearance_id": "a1",
                                               "display_stat": "Home Runs"}},
            "options": [
                {"choice": "higher", "status": "active", "decimal_price": "3.0"},
                {"choice": "lower", "status": "active", "decimal_price": "1.3"},
            ],
        },
    ],
}


def test_parse_underdog_filters_and_maps():
    lines = pm.parse_underdog(UNDERDOG_FIXTURE, "MLB")
    assert len(lines) == 1
    d = lines[0]
    assert d["player"] == "Aaron Judge"
    assert d["market"] == "batter_total_bases"
    assert d["line"] == 1.5
    assert d["over_decimal"] == 1.80 and d["under_decimal"] == 1.95


def _event_odds(over_price, under_price, pinnacle_over, pinnacle_under):
    """Two books quoting Aaron Judge total bases 1.5 over/under."""
    def book(key, ov, un):
        return {"key": key, "markets": [{"key": "batter_total_bases",
                "outcomes": [
                    {"name": "Over", "description": "Aaron Judge",
                     "point": 1.5, "price": ov},
                    {"name": "Under", "description": "Aaron Judge",
                     "point": 1.5, "price": un}]}]}
    return {"bookmakers": [book("draftkings", over_price, under_price),
                           book("pinnacle", pinnacle_over, pinnacle_under)]}


def test_sharp_over_probs_pinnacle_weighted():
    # both books ~ even money -> fair ~0.5, and it counts 2 books
    fair = pm.sharp_over_probs(_event_odds(1.95, 1.95, 2.0, 2.0))
    key = ("aaron judge", "batter_total_bases", 1.5)
    assert key in fair
    assert 0.45 < fair[key]["p"] < 0.55 and fair[key]["books"] == 2


def test_find_value_takes_the_edge_side():
    # sharp says over is ~65% likely; Underdog pays 1.80 on the over.
    # EV_over = 0.65*1.80 - 1 = +0.17 -> a clear over value pick.
    fair = {("aaron judge", "batter_total_bases", 1.5): {"p": 0.65, "books": 3}}
    dfs = pm.parse_underdog(UNDERDOG_FIXTURE, "MLB")
    picks = pm.find_value(dfs, fair, min_edge_pct=6)
    assert len(picks) == 1
    p = picks[0]
    assert p["side"] == "over" and p["player"] == "Aaron Judge"
    assert p["edge_pct"] > 6


def test_find_value_skips_thin_and_underbooked():
    dfs = pm.parse_underdog(UNDERDOG_FIXTURE, "MLB")
    # a coin-flip fair (0.52) at 1.80/1.95 -> EV_over=-0.064, EV_under=+0.014,
    # both under the 6% floor -> no pick
    assert pm.find_value(
        dfs, {("aaron judge", "batter_total_bases", 1.5):
              {"p": 0.52, "books": 3}}, min_edge_pct=6) == []
    # even a juicy edge is skipped when too few books agree
    assert pm.find_value(
        dfs, {("aaron judge", "batter_total_bases", 1.5):
              {"p": 0.80, "books": 1}}, min_edge_pct=6) == []
