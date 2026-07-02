"""Tests for the sports (devigged odds) model. Run: pytest tests/"""

from datetime import datetime, timedelta, timezone

import strategy_sports as ss


def _in_hours(h):
    return (datetime.now(timezone.utc) + timedelta(hours=h)).isoformat()


GAME = {
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


def test_devig_strips_vig():
    # symmetric odds -> exactly 50%, even though 1/1.9 + 1/1.9 > 1
    assert abs(ss.devig(1.9, 1.9) - 0.5) < 1e-9
    # devigged probabilities of both sides sum to 1
    assert abs(ss.devig(2.5, 1.6) + ss.devig(1.6, 2.5) - 1.0) < 1e-9


def test_fair_prob_prefers_pinnacle():
    p = ss.fair_home_prob(GAME)
    expected = ss.devig(2.50, 1.60)  # pinnacle's line, not somebook's
    assert abs(p - expected) < 1e-9


def test_fair_prob_median_without_pinnacle():
    game = dict(GAME, bookmakers=[b for b in GAME["bookmakers"]
                                  if b["key"] != "pinnacle"])
    assert abs(ss.fair_home_prob(game) - ss.devig(2.70, 1.50)) < 1e-9
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


def test_evaluate_market_finds_gap():
    # Pinnacle fair: Tigers ~59.5% to win. Kalshi asks only 45c for Tigers
    # YES -> buy YES signal with ~13c+ EV.
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    signals = ss.evaluate_market(market, [GAME])
    yes = [s for s in signals if s["side"] == "yes"]
    assert yes and yes[0]["ev_cents"] > 10

    # fairly priced -> no signal
    fair = {"ticker": "T", "yes_sub_title": "Detroit",
            "yes_ask": 61, "yes_bid": 58}
    assert ss.evaluate_market(fair, [GAME]) == []
