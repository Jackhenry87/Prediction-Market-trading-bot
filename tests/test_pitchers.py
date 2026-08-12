"""Probable-starter corroboration.

This is a VETO on implausible edges, not a price. The pregame fair value is a
devigged book consensus that already prices the starters, so ERA cannot sharpen
it — what it can do is show that a number is not about this game at all, which
is what both bad trades this month turned out to be.
"""

import pitchers
import strategy_sports as ss


def _m(home_era, away_era, home="Los Angeles Angels", away="Texas Rangers"):
    return dict(home_team=home, away_team=away, home_era=home_era,
                away_era=away_era, home_pitcher="H", away_pitcher="A",
                commence="2026-08-13T02:00Z")


# --- the trade this was built to stop --------------------------------------

def test_the_angels_trade_is_vetoed():
    # 2026-08-12: bought the Angels at 44c on a 21.4c "edge" while their
    # starter carried a 7.27 ERA against Texas's 3.56.
    m = _m(home_era=7.27, away_era=3.56)
    assert pitchers.favours(m) == "away"
    assert pitchers.supports_side(m, "home") is False
    assert pitchers.supports_side(m, "away") is True


def test_a_lopsided_matchup_the_other_way():
    m = _m(home_era=2.50, away_era=6.00)
    assert pitchers.favours(m) == "home"
    assert pitchers.supports_side(m, "away") is False


# --- it must not have an opinion it has not earned -------------------------

def test_a_close_matchup_has_no_lean():
    assert pitchers.favours(_m(3.20, 3.30)) is None
    assert pitchers.supports_side(_m(3.20, 3.30), "home") is True
    assert pitchers.supports_side(_m(3.20, 3.30), "away") is True


def test_the_gap_threshold_is_the_boundary(monkeypatch):
    monkeypatch.setattr(pitchers, "ERA_GAP", 1.50)
    assert pitchers.favours(_m(3.00, 4.50)) == "home"   # exactly the gap
    assert pitchers.favours(_m(3.00, 4.49)) is None


# --- missing data must fail OPEN -------------------------------------------

def test_an_unannounced_starter_does_not_veto():
    # Refusing every game with a TBA starter would disable the model on
    # mornings before lineups are posted — a worse failure than the one the
    # veto prevents.
    assert pitchers.favours(_m(None, 3.50)) is None
    assert pitchers.supports_side(_m(None, 3.50), "home") is True
    assert pitchers.supports_side(_m(3.50, None), "away") is True
    assert pitchers.supports_side(None, "home") is True


# --- parsing ----------------------------------------------------------------

def _competitor(era="3.56", name="Cal Quantrill"):
    return {"probables": [{"athlete": {"displayName": name},
                           "statistics": [{"abbreviation": "W",
                                           "displayValue": "4"},
                                          {"abbreviation": "ERA",
                                           "displayValue": era}]}]}


def test_reads_era_and_name_from_the_scoreboard_shape():
    assert pitchers._era(_competitor()) == 3.56
    assert pitchers._name(_competitor()) == "Cal Quantrill"


def test_a_missing_or_junk_era_is_none_not_a_crash():
    assert pitchers._era({}) is None
    assert pitchers._era({"probables": []}) is None
    assert pitchers._era(_competitor(era="--")) is None
    assert pitchers._name({}) is None


# --- game lookup uses the caller's matcher ---------------------------------

def test_find_uses_the_injected_matcher():
    rows = [_m(7.27, 3.56)]
    found = pitchers.find(rows, "Los Angeles Angels", "Texas Rangers",
                          ss.label_matches_team)
    assert found and found["home_era"] == 7.27


def test_find_will_not_cross_two_teams_in_one_city():
    # The exact failure that caused the bad trade, in the module that would
    # have caught it. It must not reproduce the bug while checking for it.
    rows = [_m(2.0, 5.0, home="Los Angeles Dodgers", away="Kansas City Royals")]
    assert pitchers.find(rows, "Los Angeles Angels", "Texas Rangers",
                         ss.label_matches_team) is None


def test_find_returns_none_when_the_game_is_absent():
    assert pitchers.find([], "A", "B", ss.label_matches_team) is None


# --- the gate inside the model ---------------------------------------------

GAME = {
    "id": "g1", "home_team": "Los Angeles Angels",
    "away_team": "Texas Rangers", "commence_time": "2030-01-01T02:00:00Z",
    "bookmakers": [{"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
        {"name": "Los Angeles Angels", "price": 1.45},      # model: big fav
        {"name": "Texas Rangers", "price": 2.90}]}]}],
}
MARKET = {"ticker": "KXMLBGAME-X-LAA", "yes_sub_title": "Los Angeles A",
          "status": "active", "expected_expiration_time": "2030-01-01T05:00:00Z",
          "yes_ask": 44, "yes_bid": 43}


def test_a_large_edge_contradicted_by_the_starters_is_dropped():
    sigs = ss.evaluate_market(MARKET, [GAME], None,
                              {"g1": _m(home_era=7.27, away_era=3.56)})
    assert [s for s in (sigs or []) if s["backing"] == "home"] == []


def test_the_same_edge_survives_when_the_starters_agree():
    sigs = ss.evaluate_market(MARKET, [GAME], None,
                              {"g1": _m(home_era=2.50, away_era=6.00)})
    assert [s for s in (sigs or []) if s["backing"] == "home"]


def test_the_same_edge_survives_when_starters_are_unknown():
    sigs = ss.evaluate_market(MARKET, [GAME], None,
                              {"g1": _m(home_era=None, away_era=None)})
    assert [s for s in (sigs or []) if s["backing"] == "home"]


def test_a_small_edge_is_recorded_but_not_vetoed(monkeypatch):
    # Below the corroboration threshold the check is a feature, not a gate:
    # ERA is too blunt to overrule a few cents.
    monkeypatch.setattr(ss, "SPORTS_CORROBORATE_ABOVE_CENTS", 99.0)
    sigs = ss.evaluate_market(MARKET, [GAME], None,
                              {"g1": _m(home_era=7.27, away_era=3.56)})
    home = [s for s in (sigs or []) if s["backing"] == "home"]
    assert home and home[0]["pitcher_lean"] == "away"


def test_eras_are_recorded_on_every_signal():
    sigs = ss.evaluate_market(MARKET, [GAME], None,
                              {"g1": _m(home_era=2.50, away_era=6.00)})
    for s in sigs or []:
        assert s["home_era"] == 2.50 and s["away_era"] == 6.00
        assert s["pitcher_lean"] == "home"


def test_no_matchup_map_at_all_does_not_break_the_model():
    assert ss.evaluate_market(MARKET, [GAME], None, None) is not None
