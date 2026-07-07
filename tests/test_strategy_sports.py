"""Tests for the sports (devigged odds) model. Run: pytest tests/"""

from datetime import datetime, timedelta, timezone

import strategy_sports as ss


def _in_hours(h):
    return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat()


GAME = {
    "id": "game1",
    "home_team": "Washington Nationals",
    "away_team": "Detroit Tigers",
    "commence_time": _in_hours(6),
    "bookmakers": [
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Washington Nationals", "price": 2.50},
            {"name": "Detroit Tigers", "price": 1.60}]}]},
        {"key": "somebook", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Washington Nationals", "price": 2.70},
            {"name": "Detroit Tigers", "price": 1.50}]}]},
    ],
}

# History that says the home (Nationals) line has DROPPED since last run, i.e.
# the sharp money moved toward Detroit — so backing Detroit passes the steam
# gate. (Current fair home prob for GAME is ~0.38.)
DET_STEAM = {"game1": {"home_prob": 0.45}}


def test_shin_devig_strips_vig():
    # symmetric odds -> exactly 50%, even though 1/1.9 + 1/1.9 > 1
    assert abs(ss.shin_two_way(1.9, 1.9) - 0.5) < 1e-9
    # devigged probabilities of both sides sum to 1
    p = ss.shin_devig([2.5, 1.6])
    assert abs(sum(p) - 1.0) < 1e-9
    assert abs(ss.shin_two_way(2.5, 1.6) + ss.shin_two_way(1.6, 2.5) - 1.0) < 1e-9


def test_shin_no_overround_passthrough():
    # odds implying < 100% (no vig) just normalize, no crash
    p = ss.shin_devig([3.0, 3.0])
    assert abs(sum(p) - 1.0) < 1e-9 and abs(p[0] - 0.5) < 1e-9


def test_fair_prob_weights_pinnacle():
    p = ss.fair_home_prob(GAME)
    expected = (ss.PINNACLE_WEIGHT * ss.shin_two_way(2.50, 1.60)
                + 1.0 * ss.shin_two_way(2.70, 1.50)) / (ss.PINNACLE_WEIGHT + 1.0)
    assert abs(p - expected) < 1e-9
    # weighting pulls the consensus toward Pinnacle's (higher) home prob and
    # away from the soft book — closer to Pinnacle than a plain average would be
    pin, soft = ss.shin_two_way(2.50, 1.60), ss.shin_two_way(2.70, 1.50)
    assert soft < p < pin
    assert abs(p - pin) < abs(p - soft)


def test_fair_prob_without_pinnacle():
    game = dict(GAME, bookmakers=[b for b in GAME["bookmakers"]
                                  if b["key"] != "pinnacle"])
    assert abs(ss.fair_home_prob(game) - ss.shin_two_way(2.70, 1.50)) < 1e-9
    assert ss.fair_home_prob(dict(GAME, bookmakers=[])) is None


def test_match_team_unambiguous_only():
    games = [GAME,
             {"home_team": "New York Yankees", "away_team": "Boston Red Sox",
              "commence_time": _in_hours(3), "bookmakers": []},
             {"home_team": "New York Mets", "away_team": "Chicago Cubs",
              "commence_time": _in_hours(4), "bookmakers": []}]
    game, side = ss.match_team("Detroit", games)
    assert side == "away" and game is GAME
    game, side = ss.match_team("Washington Nationals", games)
    assert side == "home"
    # "New York" alone matches two teams -> refuse to guess
    assert ss.match_team("New York", games) is None
    assert ss.match_team("Yankees", games)[1] == "home"
    assert ss.match_team("", games) is None


def test_evaluate_market_finds_gap_with_steam():
    # Pinnacle-weighted fair: Tigers ~62% to win. Kalshi asks only 45c for
    # Tigers YES -> buy YES with ~15c EV, and the sharp line moved toward
    # Detroit (DET_STEAM), so the steam gate lets it through.
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    signals = ss.evaluate_market(market, [GAME], DET_STEAM)
    yes = [s for s in signals if s["side"] == "yes"]
    assert yes and yes[0]["ev_cents"] > 10

    # fairly priced -> no signal even with steam
    fair = {"ticker": "T", "yes_sub_title": "Detroit",
            "yes_ask": 61, "yes_bid": 58}
    assert ss.evaluate_market(fair, [GAME], DET_STEAM) == []


def test_steam_gate_blocks_without_prior_line():
    # No history -> we can't confirm the line moved -> no trade (conservative).
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    assert ss.evaluate_market(market, [GAME], {}) == []
    assert ss.evaluate_market(market, [GAME], None) == []


def test_steam_gate_blocks_when_line_moves_against():
    # Line moved TOWARD the home side (home_prob rose to ~0.38 from 0.30),
    # i.e. against Detroit -> backing Detroit is blocked.
    against = {"game1": {"home_prob": 0.30}}
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    assert ss.evaluate_market(market, [GAME], against) == []


def test_steam_can_be_disabled(monkeypatch):
    monkeypatch.setattr(ss, "SPORTS_REQUIRE_STEAM", False)
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    # with the gate off, the edge alone is enough even without any history
    signals = ss.evaluate_market(market, [GAME], None)
    assert any(s["side"] == "yes" for s in signals)


def test_steam_min_move_threshold(monkeypatch):
    # require a 5-point move; a 2-point drift toward Detroit isn't enough
    monkeypatch.setattr(ss, "SPORTS_MIN_MOVE", 0.05)
    p_home = ss.fair_home_prob(GAME)
    small = {"game1": {"home_prob": round(p_home + 0.02, 4)}}
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    assert ss.evaluate_market(market, [GAME], small) == []
    big = {"game1": {"home_prob": round(p_home + 0.10, 4)}}
    assert ss.evaluate_market(market, [GAME], big)   # 10-pt move clears 5-pt bar


def test_line_history_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "LINE_HISTORY", tmp_path / "hist.json")
    assert ss.load_line_history() == {}
    ss.save_line_history({"g": {"home_prob": 0.5}})
    assert ss.load_line_history() == {"g": {"home_prob": 0.5}}


def test_in_season_filter(monkeypatch):
    class R:
        def raise_for_status(self): pass
        def json(self): return [
            {"key": "baseball_mlb", "active": True, "has_outrights": False},
            {"key": "basketball_nba", "active": False, "has_outrights": False},
            {"key": "baseball_world_series", "active": True, "has_outrights": True},
        ]
    monkeypatch.setattr(ss.requests, "get", lambda *a, **k: R())
    active = ss.in_season_sports("key")
    assert active == {"baseball_mlb"}   # inactive NBA and outrights excluded


def test_series_config_sane():
    keys = [c["sport"] for c in ss.SERIES]
    tickers = [c["series"] for c in ss.SERIES]
    assert len(keys) == len(set(keys)) and len(tickers) == len(set(tickers))
    for c in ss.SERIES:
        assert c["series"].startswith("KX")
        assert "_" in c["sport"]  # odds-api keys look like 'basketball_nba'


def test_mlb_cut_from_hourly_but_leagues_reversible(monkeypatch):
    # owner call: MLB out of the hourly devig model by default
    by_name = {c["name"]: c for c in ss.SERIES}
    monkeypatch.setattr(ss, "ENABLED_LEAGUES", {"nba", "nfl", "nhl", "wnba"})
    assert not ss.league_enabled(by_name["MLB"])
    assert ss.league_enabled(by_name["NBA"])
    assert ss.league_enabled(by_name["WNBA"])
    # one repo Variable brings it back
    monkeypatch.setattr(ss, "ENABLED_LEAGUES", {"mlb"})
    assert ss.league_enabled(by_name["MLB"])
