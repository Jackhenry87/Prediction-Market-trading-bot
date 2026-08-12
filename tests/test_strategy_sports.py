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

# GAME starts in 6h; a real Kalshi game market settles a few hours after that.
# covers_game uses this to tell tonight's moneyline apart from a season future.
GAME_SETTLES = _in_hours(9)


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
              "expected_expiration_time": GAME_SETTLES,
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    signals = ss.evaluate_market(market, [GAME], DET_STEAM)
    yes = [s for s in signals if s["side"] == "yes"]
    assert yes and yes[0]["ev_cents"] > 10

    # fairly priced -> no signal even with steam
    fair = {"ticker": "T", "yes_sub_title": "Detroit",
            "expected_expiration_time": GAME_SETTLES,
            "yes_ask": 61, "yes_bid": 58}
    assert ss.evaluate_market(fair, [GAME], DET_STEAM) == []


def test_steam_gate_blocks_without_prior_line():
    # Steam is no longer a precondition. Requiring the line to have already
    # moved toward us meant entering AFTER the move, which is why the model
    # measured -6.0c CLV. With no history at all we now still trade.
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "expected_expiration_time": GAME_SETTLES,
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    assert ss.evaluate_market(market, [GAME], {})
    assert ss.evaluate_market(market, [GAME], None)


def test_line_moving_against_us_no_longer_blocks():
    # Line moved toward HOME, i.e. away from Detroit. Backing Detroit is now
    # allowed — being early is the point — but the signal must SAY it is early.
    against = {"game1": {"home_prob": 0.30}}
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "expected_expiration_time": GAME_SETTLES,
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    sigs = ss.evaluate_market(market, [GAME], against)
    yes = [s for s in sigs if s["side"] == "yes"]
    assert yes, "an early entry must still produce a signal"
    assert yes[0]["steam"] < 0, "backing the side the line moved away from is negative steam"


def test_steam_is_signed_per_side():
    p_home = ss.fair_home_prob(GAME)
    # line drifted 6 points toward Detroit (the away side) since last look
    hist = {"game1": {"home_prob": round(p_home + 0.06, 4)}}
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "expected_expiration_time": GAME_SETTLES,
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    sigs = ss.evaluate_market(market, [GAME], hist)
    for s in sigs:
        assert s["steam"] is not None
        # backing Detroit (away) is positive steam; backing home is negative
        assert (s["steam"] > 0) == (s["backing"] == "away")


def test_no_prior_line_records_steam_as_unknown_not_zero():
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "expected_expiration_time": GAME_SETTLES,
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    sigs = ss.evaluate_market(market, [GAME], None)
    assert sigs and all(s["steam"] is None for s in sigs), \
        "unknown movement must be None, not 0.0 — 0 would read as 'no move'"


def test_is_home_is_recorded_for_moneylines():
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "expected_expiration_time": GAME_SETTLES,
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    sigs = ss.evaluate_market(market, [GAME], None)
    for s in sigs:
        assert s["is_home"] == (s["backing"] == "home")


def test_old_steam_gate_still_works_when_enabled(monkeypatch):
    # The gate is off by default but must remain functional, so the
    # chase-steam hypothesis can be re-tested against real CLV later.
    monkeypatch.setattr(ss, "SPORTS_REQUIRE_STEAM", True)
    monkeypatch.setattr(ss, "SPORTS_MIN_MOVE", 0.05)
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "expected_expiration_time": GAME_SETTLES,
              "status": "active", "yes_ask": 45, "yes_bid": 41}
    assert ss.evaluate_market(market, [GAME], None) == []      # no history
    p_home = ss.fair_home_prob(GAME)
    small = {"game1": {"home_prob": round(p_home + 0.02, 4)}}
    assert ss.evaluate_market(market, [GAME], small) == []     # 2pt < 5pt bar
    big = {"game1": {"home_prob": round(p_home + 0.10, 4)}}
    assert ss.evaluate_market(market, [GAME], big)             # 10pt clears it


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
              "expected_expiration_time": _in_hours(9),
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


# --- odds API credit budget -------------------------------------------------
# The free tier is 500 credits/month and a call costs markets x regions = 2.
# The previous generation burned a month in ~3 days, then got 401 for the rest
# of it while every workflow still reported success.

def test_budget_left_defaults_true_when_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "ODDS_QUOTA_FILE", tmp_path / "none.json")
    assert ss.budget_left() is True          # never blocks before we've looked


def test_budget_blocks_at_the_reserve(tmp_path, monkeypatch):
    q = tmp_path / "q.json"
    monkeypatch.setattr(ss, "ODDS_QUOTA_FILE", q)
    monkeypatch.setattr(ss, "ODDS_MIN_REMAINING", 50)
    q.write_text('{"remaining": 51}')
    assert ss.budget_left() is True
    q.write_text('{"remaining": 50}')
    assert ss.budget_left() is False         # at the reserve, not just under
    q.write_text('{"remaining": 3}')
    assert ss.budget_left() is False


def test_fetch_refuses_to_spend_below_reserve(tmp_path, monkeypatch):
    q = tmp_path / "q.json"
    q.write_text('{"remaining": 10}')
    monkeypatch.setattr(ss, "ODDS_QUOTA_FILE", q)
    monkeypatch.setattr(ss, "ODDS_MIN_REMAINING", 50)

    def _boom(*a, **k):
        raise AssertionError("must not spend a credit below the reserve")
    monkeypatch.setattr(ss.requests, "get", _boom)
    import pytest
    with pytest.raises(ss.OddsBudgetExhausted):
        ss.fetch_games("key", "baseball_mlb")


def test_quota_headers_are_recorded(tmp_path, monkeypatch):
    q = tmp_path / "q.json"
    monkeypatch.setattr(ss, "ODDS_QUOTA_FILE", q)

    class R:
        headers = {"x-requests-remaining": "418", "x-requests-used": "82",
                   "x-requests-last": "2"}
    ss._note_quota(R())
    import json
    assert json.loads(q.read_text())["remaining"] == 418


def test_missing_headers_do_not_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "ODDS_QUOTA_FILE", tmp_path / "q.json")

    class R:
        pass
    ss._note_quota(R())                       # must not raise


# --- the championship-future bug (2026-08-07) -------------------------------
#
# The bot bought 30 shares of KXWNBA-26-ATL — Atlanta to win the WNBA title —
# at 8c, because SERIES pointed at the season championship series instead of
# the per-game one. match_team saw the label "Atlanta", matched it to Atlanta's
# game tonight, priced a 66% game win probability against an 8c futures quote
# and reported a 58c edge. Nothing downstream objected; Kelly staked the
# maximum precisely because the number was absurd.
#
# Three independent guards now have to fail together for that to recur.

def test_series_tickers_are_per_game_not_season_futures():
    # KXWNBA and KXNBA are "Women's Pro Basketball Champion" and "2027 Pro
    # Basketball Champion". The per-game series carry the GAME suffix.
    for cfg in ss.SERIES:
        assert cfg["series"].endswith("GAME"), (
            f"{cfg['series']} is not a per-game series — a season futures "
            "ticker here prices a title bet off one game's win probability")


def test_covers_game_accepts_a_market_settling_after_the_game():
    market = {"ticker": "KXMLBGAME-X-DET",
              "expected_expiration_time": _in_hours(9)}   # game starts in 6h
    assert ss.covers_game(market, GAME)


def test_covers_game_rejects_a_season_future():
    # The real one: KXWNBA-26-ATL expires 2026-12-01, months after tonight.
    future = {"ticker": "KXWNBA-26-ATL",
              "expected_expiration_time": _in_hours(24 * 116)}
    assert not ss.covers_game(future, GAME)


def test_covers_game_rejects_a_market_settling_before_the_game_starts():
    early = {"ticker": "X", "expected_expiration_time": _in_hours(1)}
    assert not ss.covers_game(early, GAME)


def test_covers_game_fails_closed_without_timestamps():
    # Same convention match_team follows: skip what cannot be placed, never
    # guess. A market with no settlement time cannot be tied to a game.
    assert not ss.covers_game({"ticker": "X"}, GAME)
    assert not ss.covers_game({"expected_expiration_time": _in_hours(9)},
                              {"home_team": "Washington Nationals"})


def test_covers_game_prefers_expected_expiration_over_close_time():
    # close_time runs days past a game (KXMLBGAME closes 3 days out), so using
    # it as the primary would let a genuine game market fail the window.
    market = {"expected_expiration_time": _in_hours(9),
              "close_time": _in_hours(72)}
    assert ss.covers_game(market, GAME)
    assert ss.market_expiry(market) == ss.parse_iso(market[
        "expected_expiration_time"])


def test_evaluate_market_refuses_a_future_that_names_a_playing_team():
    # End to end: the exact shape of the trade that lost real money. The label
    # matches a team playing tonight and the price is wildly off our game
    # model, which is precisely what made it look like free money.
    future = {"ticker": "KXWNBA-26-ATL", "yes_sub_title": "Detroit",
              "status": "active",
              "expected_expiration_time": _in_hours(24 * 116),
              "yes_ask": 8, "yes_bid": 7}
    assert ss.evaluate_market(future, [GAME], None) == []


def test_edge_ceiling_rejects_an_implausible_edge():
    assert ss.edge_ok(10.0, "T")
    assert not ss.edge_ok(ss.SPORTS_MAX_EDGE_CENTS + 0.1, "T")
    assert not ss.edge_ok(1.0, "T")            # still floors on fees
    assert ss.edge_ok(ss.SPORTS_MAX_EDGE_CENTS, "T")   # boundary is inclusive


def test_edge_ceiling_blocks_a_signal_even_when_the_market_is_the_right_game():
    # The ceiling is the backstop that does not depend on knowing WHY the
    # number is wrong — a bad devig or a stale quote gets caught too.
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 8, "yes_bid": 7}
    assert ss.evaluate_market(market, [GAME], None) == []


def test_edge_ceiling_is_configurable(monkeypatch):
    monkeypatch.setattr(ss, "SPORTS_MAX_EDGE_CENTS", 60.0)
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 8, "yes_bid": 7}
    assert ss.evaluate_market(market, [GAME], None)


# --- underdogs (owner call, 2026-08-08) ------------------------------------
#
# SPORTS_MIN_CONFIDENCE = 0.52 could only ever back favourites: an underdog is
# a side our model puts under 50% by definition. The moneyline leg reported
# "0 qualifying" on every run of its life, which was the gate being shut, not
# the gate being selective.

def test_probability_floor_no_longer_excludes_every_underdog():
    assert ss.SPORTS_MIN_PROB < 0.50, (
        "a floor at or above 0.50 makes underdog bets structurally impossible")


def test_an_undervalued_underdog_now_produces_a_signal():
    # Fair for Detroit is ~62%, so Washington (home) is a ~38% dog. Kalshi
    # asking 30c for Washington YES is a real gap on a genuine underdog.
    market = {"ticker": "KXMLBGAME-X-WSH", "yes_sub_title": "Washington",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 30, "yes_bid": 28}
    sigs = ss.evaluate_market(market, [GAME], None)
    yes = [s for s in sigs if s["side"] == "yes"]
    assert yes, "a 38% dog priced at 30c should clear the gates"
    assert yes[0]["model_prob"] < 0.50
    assert yes[0]["is_underdog"] is True


def test_true_longshots_stay_out():
    # Below the floor the favourite-longshot bias is worst and apparent edge is
    # more likely our own model error. Nothing under SPORTS_MIN_PROB trades.
    lopsided = dict(GAME, bookmakers=[
        {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
            {"name": "Washington Nationals", "price": 12.0},
            {"name": "Detroit Tigers", "price": 1.04}]}]}])
    market = {"ticker": "KXMLBGAME-X-WSH", "yes_sub_title": "Washington",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 2, "yes_bid": 1}
    assert [s for s in ss.evaluate_market(market, [lopsided], None)
            if s["side"] == "yes"] == []


def test_is_underdog_is_recorded_on_both_sides():
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 45, "yes_bid": 41}
    for s in ss.evaluate_market(market, [GAME], None):
        assert s["is_underdog"] is (s["price_cents"] < 50)


def test_a_favourite_is_not_flagged_as_an_underdog():
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 55, "yes_bid": 52}
    yes = [s for s in ss.evaluate_market(market, [GAME], None)
           if s["side"] == "yes"]
    assert yes and yes[0]["is_underdog"] is False


def test_the_edge_ceiling_still_applies_to_underdogs():
    # Opening the floor must not reopen the championship-future hole: a wild
    # edge on a cheap contract is still a mispriced match, not an opportunity.
    market = {"ticker": "KXMLBGAME-X-WSH", "yes_sub_title": "Washington",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 5, "yes_bid": 4}
    assert [s for s in ss.evaluate_market(market, [GAME], None)
            if s["side"] == "yes"] == []


def test_explicit_min_confidence_still_overrides(monkeypatch):
    # Back-compat: anyone who set SPORTS_MIN_CONFIDENCE keeps their gate.
    monkeypatch.setattr(ss, "SPORTS_MIN_CONFIDENCE", 0.52)
    market = {"ticker": "KXMLBGAME-X-WSH", "yes_sub_title": "Washington",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 30, "yes_bid": 28}
    assert [s for s in ss.evaluate_market(market, [GAME], None)
            if s["side"] == "yes"] == []


# --- why did nothing qualify? ----------------------------------------------
#
# "0 qualifying" was blamed on the series ticker, then the confidence floor,
# then the settlement guard. Two of those three guesses were wrong. The gates
# now count themselves so a run answers the question instead of us inferring
# it from silence.

def test_rejects_are_counted_by_reason():
    ss.REJECTS.clear()
    fair = {"ticker": "T", "yes_sub_title": "Detroit", "status": "active",
            "expected_expiration_time": GAME_SETTLES,
            "yes_ask": 61, "yes_bid": 58}          # priced right, no edge
    assert ss.evaluate_market(fair, [GAME], None) == []
    assert sum(ss.REJECTS.values()) > 0
    assert any("edge" in why for why in ss.REJECTS)


def test_probability_floor_rejection_is_named(monkeypatch):
    monkeypatch.setattr(ss, "SPORTS_MIN_CONFIDENCE", 0.99)
    ss.REJECTS.clear()
    market = {"ticker": "T", "yes_sub_title": "Detroit", "status": "active",
              "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 45, "yes_bid": 41}
    ss.evaluate_market(market, [GAME], None)
    assert ss.REJECTS["below the probability floor"] == 1


def test_settlement_rejection_is_named():
    ss.REJECTS.clear()
    future = {"ticker": "KXWNBA-26-ATL", "yes_sub_title": "Detroit",
              "status": "active", "yes_ask": 8, "yes_bid": 7,
              "expected_expiration_time": _in_hours(24 * 116)}
    ss.evaluate_market(future, [GAME], None)
    assert ss.REJECTS["settles on a different game"] == 1


def test_unmatched_team_is_named():
    ss.REJECTS.clear()
    market = {"ticker": "T", "yes_sub_title": "Nobody At All",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 45, "yes_bid": 41}
    ss.evaluate_market(market, [GAME], None)
    assert ss.REJECTS["no unambiguous team match"] == 1


def test_counting_never_changes_the_decision():
    # The instrument must not perturb what it measures.
    market = {"ticker": "KXMLBGAME-X-DET", "yes_sub_title": "Detroit",
              "status": "active", "expected_expiration_time": GAME_SETTLES,
              "yes_ask": 45, "yes_bid": 41}
    ss.REJECTS.clear()
    first = ss.evaluate_market(market, [GAME], None)
    second = ss.evaluate_market(market, [GAME], None)
    assert first == second and first


# --- the odds cache (2026-08-10) -------------------------------------------
#
# The free tier hit its reserve and the sports model went COMPLETELY DARK:
# "we cannot afford a call right now" and "the model does not run" were the
# same condition. The model must evaluate on every run; only the paid refresh
# is rationed. Running on yesterday's lines is worse than today's and far
# better than not running.

def _payload(hours_out=6.0):
    from datetime import datetime, timedelta, timezone
    t = (datetime.now(timezone.utc) + timedelta(hours=hours_out)).isoformat()
    return [{"id": "g1", "commence_time": t,
             "home_team": "Washington Nationals",
             "away_team": "Detroit Tigers", "bookmakers": []}]


def _use_tmp_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "ODDS_CACHE_FILE", tmp_path / "odds_cache.json")


def test_a_fresh_cache_is_used_without_spending_a_credit(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload())

    def boom(*a, **k):
        raise AssertionError("must not call the paid endpoint on a fresh cache")

    monkeypatch.setattr(ss.requests, "get", boom)
    assert len(ss.fetch_games("key", "baseball_mlb")) == 1


def test_exhausted_credits_fall_back_to_stale_lines(tmp_path, monkeypatch):
    # THE fix: with no budget and a cache present, the model keeps running.
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload())
    monkeypatch.setattr(ss, "ODDS_CACHE_TTL_MIN", 0.0)      # force it stale
    monkeypatch.setattr(ss, "budget_left", lambda: False)
    monkeypatch.setattr(ss.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no budget: must not call")))
    assert len(ss.fetch_games("key", "baseball_mlb")) == 1


def test_no_budget_and_no_cache_is_still_a_clean_stop(tmp_path, monkeypatch):
    import pytest
    _use_tmp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(ss, "budget_left", lambda: False)
    with pytest.raises(ss.OddsBudgetExhausted):
        ss.fetch_games("key", "baseball_mlb")


def test_the_cache_holds_raw_lines_and_refilters_by_start_time(tmp_path,
                                                               monkeypatch):
    # A cached slate must drop games that have since started, or a stale cache
    # would keep offering games already in progress.
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload(hours_out=-1.0))   # already begun
    monkeypatch.setattr(ss.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("fresh cache: must not call")))
    assert ss.fetch_games("key", "baseball_mlb") == []


def test_a_stale_cache_triggers_a_paid_refresh_when_budget_allows(tmp_path,
                                                                  monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload())
    monkeypatch.setattr(ss, "ODDS_CACHE_TTL_MIN", 0.0)
    monkeypatch.setattr(ss, "budget_left", lambda: True)
    calls = []

    class _Resp:
        headers = {}
        def raise_for_status(self): pass
        def json(self): return _payload()

    def fake_get(*a, **k):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(ss.requests, "get", fake_get)
    assert len(ss.fetch_games("key", "baseball_mlb")) == 1
    assert len(calls) == 1


def test_a_refresh_replaces_the_cached_lines(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", [])
    games, age = ss.cached_odds("baseball_mlb")
    assert games == [] and age is not None and age < 1.0


def test_an_unreadable_cache_is_not_fatal(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    (tmp_path / "odds_cache.json").write_text("{ not json")
    assert ss.cached_odds("baseball_mlb") == (None, None)


def test_each_sport_is_cached_separately(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload())
    assert ss.cached_odds("baseball_mlb")[0] is not None
    assert ss.cached_odds("icehockey_nhl") == (None, None)


# --- same-city teams (2026-08-10) ------------------------------------------
#
# The dominant reason the moneyline leg never qualified. A live run rejected
# 71 candidates for "no unambiguous team match" — more than every other reason
# combined — because _words() dropped tokens shorter than three characters,
# which is exactly the character Kalshi uses to tell same-city teams apart.
# All of these label forms are real, taken from live KXMLBGAME markets.

CITY_GAMES = [
    {"home_team": "New York Yankees", "away_team": "Boston Red Sox"},
    {"home_team": "New York Mets", "away_team": "Pittsburgh Pirates"},
    {"home_team": "Chicago Cubs", "away_team": "Chicago White Sox"},
    {"home_team": "Los Angeles Dodgers", "away_team": "Los Angeles Angels"},
]


def test_a_trailing_initial_disambiguates_same_city_teams():
    for label, expected in [("New York Y", "New York Yankees"),
                            ("New York M", "New York Mets"),
                            ("Chicago C", "Chicago Cubs"),
                            ("Los Angeles D", "Los Angeles Dodgers"),
                            ("Los Angeles A", "Los Angeles Angels")]:
        m = ss.match_team(label, CITY_GAMES)
        assert m, f"{label!r} should resolve to {expected}"
        assert m[0][f"{m[1]}_team"] == expected


def test_initials_disambiguate_a_two_word_nickname():
    # "WS" is not a prefix of "White" or "Sox" — it is their initials.
    m = ss.match_team("Chicago WS", CITY_GAMES)
    assert m and m[0][f"{m[1]}_team"] == "Chicago White Sox"


def test_a_bare_city_is_still_refused():
    for label in ("New York", "Chicago", "Los Angeles"):
        assert ss.match_team(label, CITY_GAMES) is None


def test_a_nickname_only_label_still_matches():
    # The precise rule cannot match "Yankees" against "New York Yankees", so a
    # loose fallback runs ONLY when the precise rule found nothing.
    m = ss.match_team("Yankees", CITY_GAMES)
    assert m and m[0][f"{m[1]}_team"] == "New York Yankees"


def test_the_fallback_cannot_reintroduce_same_city_ambiguity():
    # "New York Y" must not also match the Mets via the loose rule.
    assert len([1 for g in CITY_GAMES for s in ("home", "away")
                if ss.label_matches_team("New York Y", g[f"{s}_team"])]) == 1


def test_label_matches_team_is_directional():
    assert ss.label_matches_team("New York Y", "New York Yankees")
    assert not ss.label_matches_team("New York Y", "New York Mets")
    assert not ss.label_matches_team("Chicago C", "Chicago White Sox")
    assert not ss.label_matches_team("", "New York Mets")
    assert not ss.label_matches_team("Boston", "")


# --- not paying to re-price a slate we cannot bet on yet --------------------
#
# 500 credits a month is 250 calls at h2h+totals over one region, so every
# avoided call is ~0.4% of the month. A US slate is clustered in the evening,
# so skipping overnight refreshes is close to a 2x saving on a fixed TTL.

def test_no_refresh_when_the_next_game_is_far_away(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload(hours_out=20.0))
    monkeypatch.setattr(ss, "ODDS_CACHE_TTL_MIN", 0.0)     # cache is stale
    monkeypatch.setattr(ss, "ODDS_REFRESH_LEAD_H", 14.0)
    monkeypatch.setattr(ss, "budget_left", lambda: True)
    monkeypatch.setattr(ss.requests, "get",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("next game is 20h out: must not pay")))
    ss.fetch_games("key", "baseball_mlb")      # must not raise


def test_a_refresh_does_happen_once_a_game_is_near(tmp_path, monkeypatch):
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload(hours_out=3.0))
    monkeypatch.setattr(ss, "ODDS_CACHE_TTL_MIN", 0.0)
    monkeypatch.setattr(ss, "ODDS_REFRESH_LEAD_H", 14.0)
    monkeypatch.setattr(ss, "budget_left", lambda: True)
    calls = []

    class _Resp:
        headers = {}
        def raise_for_status(self): pass
        def json(self): return _payload(hours_out=3.0)

    monkeypatch.setattr(ss.requests, "get",
                        lambda *a, **k: (calls.append(1), _Resp())[1])
    ss.fetch_games("key", "baseball_mlb")
    assert len(calls) == 1


def test_an_empty_cache_still_refreshes(tmp_path, monkeypatch):
    # The lead-time gate must never be able to prevent the FIRST fetch, or a
    # cold start would never acquire lines at all.
    _use_tmp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(ss, "budget_left", lambda: True)
    calls = []

    class _Resp:
        headers = {}
        def raise_for_status(self): pass
        def json(self): return _payload()

    monkeypatch.setattr(ss.requests, "get",
                        lambda *a, **k: (calls.append(1), _Resp())[1])
    ss.fetch_games("key", "baseball_mlb")
    assert len(calls) == 1


def test_a_slate_of_only_finished_games_does_not_freeze_the_cache(tmp_path,
                                                                  monkeypatch):
    # Every start time in the past means there is no positive lead time. That
    # must NOT read as "nothing is near", or yesterday's finished slate would
    # block every future refresh and the model would never see a new game.
    _use_tmp_cache(tmp_path, monkeypatch)
    ss._cache_store("baseball_mlb", _payload(hours_out=-5.0))
    monkeypatch.setattr(ss, "ODDS_CACHE_TTL_MIN", 0.0)
    monkeypatch.setattr(ss, "budget_left", lambda: True)
    calls = []

    class _Resp:
        headers = {}
        def raise_for_status(self): pass
        def json(self): return _payload(hours_out=3.0)

    monkeypatch.setattr(ss.requests, "get",
                        lambda *a, **k: (calls.append(1), _Resp())[1])
    assert len(ss.fetch_games("key", "baseball_mlb")) == 1
    assert len(calls) == 1, "a stale, all-finished slate must trigger a refresh"


def test_the_markets_parameter_is_configurable(tmp_path, monkeypatch):
    # A call costs markets x regions, so this parameter is the only lever that
    # halves the price of every call. It must reach the request, not just the
    # module constant.
    _use_tmp_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(ss, "ODDS_MARKETS", "totals")
    monkeypatch.setattr(ss, "budget_left", lambda: True)
    seen = {}

    class _Resp:
        headers = {}
        def raise_for_status(self): pass
        def json(self): return _payload()

    def fake_get(url, params=None, **k):
        seen.update(params or {})
        return _Resp()

    monkeypatch.setattr(ss.requests, "get", fake_get)
    ss.fetch_games("key", "baseball_mlb")
    assert seen["markets"] == "totals"
    assert seen["regions"] == "us"          # cost is markets x regions
