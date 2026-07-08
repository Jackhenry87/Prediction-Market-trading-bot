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


def test_confidence_floor_skips_coin_flips(monkeypatch):
    monkeypatch.setattr(ss, "SPORTS_REQUIRE_STEAM", False)   # isolate the floor
    monkeypatch.setattr(ss, "SPORTS_MIN_CONFIDENCE", 0.60)
    # symmetric prices -> both sides ~50%, below the 60% floor
    game = {"id": "g", "home_team": "Alpha Cats", "away_team": "Beta Dogs",
            "commence_time": _in_hours(6), "bookmakers": [
                {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Alpha Cats", "price": 1.95},
                    {"name": "Beta Dogs", "price": 1.95}]}]}]}
    market = {"ticker": "T", "yes_sub_title": "Beta Dogs", "status": "active",
              "yes_ask": 40, "yes_bid": 37}
    assert ss.evaluate_market(market, [game], None) == []    # 50% < 60% floor


def test_sports_placed_today_counts(tmp_path, monkeypatch):
    import csv

    import ledger
    log = tmp_path / "exec.csv"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(log, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(ledger.EXEC_COLUMNS)
        w.writerow([today + "T12:00:00Z", "sports", "K1", "yes", "1", "55",
                    "0.55", "o", ""])
        w.writerow([today + "T13:00:00Z", "sports", "K2", "no", "1", "52",
                    "0.52", "o", ""])
        w.writerow(["2020-01-01T00:00:00Z", "sports", "K3", "yes", "1", "50",
                    "0.50", "o", ""])                        # old day
        w.writerow([today + "T14:00:00Z", "weather", "K4", "no", "1", "60",
                    "0.60", "o", ""])                        # not sports
    monkeypatch.setattr(ledger, "EXEC_LOG", log)
    assert ss._sports_placed_today() == 2


def test_ml_daily_budget_caps_scan(monkeypatch):
    monkeypatch.setattr(ss, "SPORTS_MAX_ML_PER_DAY", 2)
    monkeypatch.setattr(ss, "SPORTS_MAX_TOTALS_PER_DAY", 0)
    monkeypatch.setattr(ss, "_sports_placed_today", lambda kind="all": 0)
    monkeypatch.setattr(ss, "in_season_sports", lambda k: {"baseball_mlb"})
    monkeypatch.setattr(ss, "fetch_games", lambda k, s: [
        {"id": "g", "home_team": "A A", "away_team": "B B", "bookmakers": []}])
    # five qualifying plays with ascending edge -> only the top 2 come back
    monkeypatch.setattr(ss, "evaluate_market", lambda m, g, h: [dict(
        side="yes", price_cents=50, model_prob=0.7,
        ev_cents=float(m["ticker"][1:]), steam=0.02,
        ticker=m["ticker"], subtitle="x")])

    class _Fake:
        def __init__(self, *a, **k): pass
        def get_positions(self): return {"market_positions": []}
        def _request(self, method, path, params=None):
            # moneyline series only; totals series returns nothing
            if "TOTAL" in str(params.get("series_ticker", "")):
                return {"events": []}
            return {"events": [{"event_ticker": "E1", "title": "t",
                                "markets": [{"ticker": f"K{i}",
                                             "status": "active"}
                                            for i in range(5)]}]}
    monkeypatch.setattr(ss, "KalshiClient", _Fake)
    results = ss.scan("key")
    tickers = [s["ticker"] for r in results for s in r["signals"]]
    assert len(tickers) == 2 and set(tickers) == {"K4", "K3"}   # top 2 by edge


def test_fair_total_mean_and_over_prob(monkeypatch):
    monkeypatch.setattr(ss, "TOTAL_SIGMA", 3.0)
    # symmetric over/under at 8.5 -> devigged P(over)=0.5 -> mean == line
    game = {"bookmakers": [{"key": "pinnacle", "markets": [{"key": "totals",
            "outcomes": [{"name": "Over", "price": 1.95, "point": 8.5},
                         {"name": "Under", "price": 1.95, "point": 8.5}]}]}]}
    assert abs(ss.fair_total_mean(game) - 8.5) < 1e-6
    # at the mean, P(over) is 0.5; well below the mean it's high
    assert abs(ss.over_prob(8.5, 8.5) - 0.5) < 1e-9
    assert ss.over_prob(8.5, 5.5) > 0.8


def test_total_game_match_fails_closed():
    games = [{"home_team": "Colorado Rockies", "away_team": "Los Angeles Dodgers"},
             {"home_team": "New York Yankees", "away_team": "Boston Red Sox"}]
    g = ss.match_total_game("Colorado vs Los Angeles D: Total Runs", games)
    assert g and g["home_team"] == "Colorado Rockies"
    # a doubleheader (same two teams twice) can't be told apart -> refuse
    dh = [{"home_team": "Colorado Rockies", "away_team": "Los Angeles Dodgers"},
          {"home_team": "Colorado Rockies", "away_team": "Los Angeles Dodgers"}]
    assert ss.match_total_game("Colorado vs Los Angeles D: Total Runs", dh) is None


def test_evaluate_total_market_gates(monkeypatch):
    monkeypatch.setattr(ss, "SPORTS_REQUIRE_STEAM", False)
    monkeypatch.setattr(ss, "TOTAL_SIGMA", 3.0)
    monkeypatch.setattr(ss, "SPORTS_MIN_CONFIDENCE", 0.60)
    monkeypatch.setattr(ss, "MIN_EDGE_CENTS", 5.0)
    # mean 8.5; Over 6.5 (within the 2.5 window) is ~75% -> ask 60c = edge
    mkt = {"ticker": "T", "yes_sub_title": "Over 6.5", "status": "active",
           "floor_strike": 6.5, "yes_ask": 60, "yes_bid": 57}
    sig = ss.evaluate_total_market(mkt, mean=8.5)
    assert sig and sig[0]["side"] == "yes" and sig[0]["model_prob"] > 0.7
    # a strike far from the mean is outside the reliable window -> skipped
    far = dict(mkt, floor_strike=13.5)
    assert ss.evaluate_total_market(far, mean=8.5) == []


def test_leagues_configurable(monkeypatch):
    # MLB is back in by default; SPORTS_LEAGUES still gates each league
    by_name = {c["name"]: c for c in ss.SERIES}
    monkeypatch.setattr(ss, "ENABLED_LEAGUES", {"mlb", "wnba"})
    assert ss.league_enabled(by_name["MLB"])
    assert ss.league_enabled(by_name["WNBA"])
    assert not ss.league_enabled(by_name["NBA"])     # excluded by the Variable
