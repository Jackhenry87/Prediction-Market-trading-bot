"""Tests for the smart-money (Polymarket sharp-wallet consensus) model."""

import strategy_smartmoney as sm

NOW = 1_783_360_000.0


def _trade(wallet, slug, outcome, price, size, ts=NOW - 3600, side="BUY"):
    return dict(proxyWallet=wallet, slug=slug, outcome=outcome, price=price,
                size=size, timestamp=ts, side=side, title=slug)


def test_pnl_change_windows():
    day = 86400
    curve = [(NOW - 20 * day, 0.0), (NOW - 14 * day, 100.0),
             (NOW - 7 * day, 400.0), (NOW - 1 * day, 900.0)]
    # 2w window: baseline is the point AT now-14d -> 900 - 100
    assert sm.pnl_change(curve, 14, now_ts=NOW) == 800.0
    # 1w window -> 900 - 400
    assert sm.pnl_change(curve, 7, now_ts=NOW) == 500.0
    # history shorter than window -> falls back to curve start
    assert sm.pnl_change(curve[2:], 30, now_ts=NOW) == 500.0
    assert sm.pnl_change([], 14, now_ts=NOW) == 0.0


def test_select_sharp_wallets_filters(monkeypatch):
    day = 86400
    tape = [_trade("0xup", "x", "Yes", 0.5, 2000),
            _trade("0xlucky", "x", "Yes", 0.5, 2000),
            _trade("0xdown", "x", "Yes", 0.5, 2000)]
    curves = {
        # steadily up: qualifies
        "0xup": [(NOW - 14 * day, 0.0), (NOW - 7 * day, 400.0),
                 (NOW - day, 900.0)],
        # big fortnight but bleeding this week: rejected (consistency)
        "0xlucky": [(NOW - 14 * day, 0.0), (NOW - 7 * day, 2000.0),
                    (NOW - day, 1500.0)],
        # below the 2-week floor: rejected
        "0xdown": [(NOW - 14 * day, 0.0), (NOW - day, 100.0)],
    }
    monkeypatch.setattr(sm, "fetch_big_trades", lambda: tape)
    monkeypatch.setattr(sm, "fetch_pnl_curve", lambda w: curves[w])
    monkeypatch.setattr(sm, "SHARP_MIN_PNL_2W", 500.0)
    sharps = sm.select_sharp_wallets()
    assert list(sharps) == ["0xup"]


def test_consensus_needs_distinct_wallets(monkeypatch):
    trades = {
        "w1": [_trade("w1", "mlb-phi-kc-2026-07-06", "Phillies", 0.60, 200),
               # same wallet buying twice must NOT count as two sharps
               _trade("w1", "mlb-phi-kc-2026-07-06", "Phillies", 0.62, 300)],
        "w2": [_trade("w2", "mlb-phi-kc-2026-07-06", "Phillies", 0.58, 150)],
        "w3": [_trade("w3", "mlb-phi-kc-2026-07-06", "Phillies", 0.61, 500),
               _trade("w3", "nba-lal-bos-2026-07-06", "Lakers", 0.40, 400)],
    }
    monkeypatch.setattr(sm, "fetch_wallet_buys",
                        lambda w, h, now_ts=None: trades[w])
    monkeypatch.setattr(sm, "MIN_WALLETS", 3)
    cons = sm.build_consensus({"w1": 1, "w2": 1, "w3": 1}, now_ts=NOW)
    assert len(cons) == 1                      # Lakers had only 1 sharp
    c = cons[0]
    assert c["slug"] == "mlb-phi-kc-2026-07-06" and c["wallets"] == 3
    # stake-weighted average entry
    total = 0.60 * 200 * 0.60 + 0.62 * 300 * 0.62 + 0.58 * 150 * 0.58 \
        + 0.61 * 500 * 0.61
    stake = 0.60 * 200 + 0.62 * 300 + 0.58 * 150 + 0.61 * 500
    assert abs(c["avg_price"] - total / stake) < 1e-9


def test_moneyline_slug_regex():
    assert sm.MONEYLINE_RE.match("mlb-phi-kc-2026-07-06")
    assert sm.MONEYLINE_RE.match("wnba-ny-la-2026-07-06")
    # spreads, totals, props, and non-Kalshi venues are rejected
    assert not sm.MONEYLINE_RE.match("mlb-phi-kc-2026-07-06-spread-away-1pt5")
    assert not sm.MONEYLINE_RE.match("fifwc-prt-esp-2026-07-06-halftime-result-away")
    assert not sm.MONEYLINE_RE.match("xrp-updown-15m-1783359900")


def test_kalshi_date_token():
    assert sm.kalshi_date_token("2026", "07", "06") == "26JUL06"
    assert sm.kalshi_date_token("2027", "12", "31") == "27DEC31"


EVENTS = [{
    "event_ticker": "KXMLBGAME-26JUL06PHIKC",
    "title": "Phillies vs Royals",
    "markets": [
        {"ticker": "KXMLBGAME-26JUL06PHIKC-PHI", "status": "active",
         "yes_sub_title": "Philadelphia Phillies", "yes_ask": 60, "yes_bid": 57},
        {"ticker": "KXMLBGAME-26JUL06PHIKC-KC", "status": "active",
         "yes_sub_title": "Kansas City Royals", "yes_ask": 42, "yes_bid": 38},
    ],
}]


def _cons(price=0.58):
    return dict(slug="mlb-phi-kc-2026-07-06", outcome="Philadelphia Phillies",
                title="Phillies vs Royals ML", wallets=3, stake=1000.0,
                avg_price=price)


def test_consensus_signal_maps_to_kalshi():
    sig = sm.consensus_signal(_cons(0.58), EVENTS)
    assert sig and sig["ticker"] == "KXMLBGAME-26JUL06PHIKC-PHI"
    assert sig["side"] == "yes" and sig["price_cents"] == 60
    # prob = sharp entry + premium
    assert abs(sig["model_prob"] - (0.58 + sm.PREMIUM_PTS / 100)) < 1e-9
    assert "3 sharps" in sig["subtitle"]


def test_no_chase_when_line_ran_away():
    # sharps got 58c but Kalshi now asks 80c -> EV fails -> refuse to chase
    events = [dict(EVENTS[0])]
    events[0] = {**EVENTS[0], "markets": [
        {**EVENTS[0]["markets"][0], "yes_ask": 80}]}
    assert sm.consensus_signal(_cons(0.58), events) is None


def test_wrong_date_or_league_not_mapped():
    cons = _cons()
    cons["slug"] = "mlb-phi-kc-2026-07-07"       # tomorrow's game
    assert sm.consensus_signal(cons, EVENTS) is None
    cons["slug"] = "fifwc-prt-esp-2026-07-06"    # no Kalshi twin
    assert sm.consensus_signal(cons, EVENTS) is None


def test_league_series_mapping_sane():
    for league in ("mlb", "nba", "nfl", "nhl", "wnba"):
        assert sm.LEAGUE_SERIES[league].startswith("KX")


def test_wc_slug_regex_and_routing():
    assert sm.WC_RE.match("fifwc-prt-esp-2026-07-06-esp")
    assert sm.WC_RE.match("fifwc-prt-esp-2026-07-06-prt")
    assert sm.WC_RE.match("fifwc-prt-esp-2026-07-06-draw")
    # props, advance, totals, scores: all rejected (different bets)
    assert not sm.WC_RE.match("fifwc-usa-bel-2026-07-06-team-to-advance")
    assert not sm.WC_RE.match("fifwc-prt-esp-2026-07-06-total-8pt5")
    assert not sm.WC_RE.match("fifwc-prt-esp-2026-07-06-exact-score-2-1")
    assert not sm.WC_RE.match("fifwc-prt-esp-2026-07-06-btts")
    assert not sm.WC_RE.match("fifwc-prt-esp-2026-07-06-goals-lamine-yamal-gte1")


WC_EVENTS = [{
    "event_ticker": "KXFIFAGAME-26JUL06PRTESP",
    "title": "Portugal vs Spain",
    "markets": [
        {"ticker": "KXFIFAGAME-26JUL06PRTESP-PRT", "status": "active",
         "yes_sub_title": "Portugal", "yes_ask": 30, "yes_bid": 27},
        {"ticker": "KXFIFAGAME-26JUL06PRTESP-ESP", "status": "active",
         "yes_sub_title": "Spain", "yes_ask": 46, "yes_bid": 43},
        {"ticker": "KXFIFAGAME-26JUL06PRTESP-TIE", "status": "active",
         "yes_sub_title": "Tie", "yes_ask": 29, "yes_bid": 26},
    ],
}]


def _wc_cons(slug, outcome="Yes", price=0.44):
    return dict(slug=slug, outcome=outcome, title="Will Spain win?",
                wallets=4, stake=2500.0, avg_price=price)


def test_wc_signal_maps_match_winner():
    sig = sm.wc_signal(_wc_cons("fifwc-prt-esp-2026-07-06-esp"), WC_EVENTS)
    assert sig and sig["ticker"] == "KXFIFAGAME-26JUL06PRTESP-ESP"
    assert sig["side"] == "yes" and sig["price_cents"] == 46
    assert "4 sharps" in sig["subtitle"]
    # reversed code order in the Kalshi ticker still matches
    rev = [dict(WC_EVENTS[0], event_ticker="KXFIFAGAME-26JUL06ESPPRT")]
    assert sm.wc_signal(_wc_cons("fifwc-prt-esp-2026-07-06-esp"), rev)


def test_wc_signal_maps_draw_to_tie():
    sig = sm.wc_signal(_wc_cons("fifwc-prt-esp-2026-07-06-draw", price=0.27),
                       WC_EVENTS)
    assert sig and sig["ticker"] == "KXFIFAGAME-26JUL06PRTESP-TIE"


def test_wc_signal_fails_closed():
    # no chase: sharps at 30c, Kalshi asks 46c -> blocked
    assert sm.wc_signal(_wc_cons("fifwc-prt-esp-2026-07-06-esp", price=0.30),
                        WC_EVENTS) is None
    # wrong date -> no event match
    assert sm.wc_signal(_wc_cons("fifwc-prt-esp-2026-07-07-esp"),
                        WC_EVENTS) is None
    # suffix that is a trigram but NOT one of the two teams -> refused
    assert sm.wc_signal(_wc_cons("fifwc-prt-esp-2026-07-06-bra"),
                        WC_EVENTS) is None
    # missing side market (no TIE listed) -> no trade
    no_tie = [dict(WC_EVENTS[0], markets=WC_EVENTS[0]["markets"][:2])]
    assert sm.wc_signal(_wc_cons("fifwc-prt-esp-2026-07-06-draw"),
                        no_tie) is None


TENNIS_EVENTS = [
    {"event_ticker": "KXATPMATCH-26JUL08LEHZVE", "title": "Lehecka vs Zverev",
     "markets": [
         {"ticker": "KXATPMATCH-26JUL08LEHZVE-LEH", "status": "active",
          "yes_sub_title": "Lehecka", "yes_ask": 30, "yes_bid": 27},
         {"ticker": "KXATPMATCH-26JUL08LEHZVE-ZVE", "status": "active",
          "yes_sub_title": "Zverev", "yes_ask": 84, "yes_bid": 81}]},
    {"event_ticker": "KXATPMATCH-26JUL08COBFER", "title": "Cobolli vs Fery",
     "markets": []},
]


def _tennis_cons(price=0.85, outcome="Alexander Zverev"):
    return dict(slug="wimbledon-atp-lehecka-zverev",
                title="Wimbledon ATP: Jiri Lehecka vs Alexander Zverev",
                outcome=outcome, wallets=3, stake=21544.0, avg_price=price)


def test_tennis_signal_maps_by_surname():
    sig = sm.tennis_signal(_tennis_cons(), TENNIS_EVENTS)
    assert sig and sig["ticker"] == "KXATPMATCH-26JUL08LEHZVE-ZVE"
    assert sig["price_cents"] == 84
    # underdog side maps too
    sig = sm.tennis_signal(_tennis_cons(price=0.28, outcome="Jiri Lehecka"),
                           TENNIS_EVENTS)
    assert sig and sig["ticker"] == "KXATPMATCH-26JUL08LEHZVE-LEH"
    # and the 25c floor still refuses true lottery tickets
    cheap = [dict(TENNIS_EVENTS[0], markets=[
        dict(TENNIS_EVENTS[0]["markets"][0], yes_ask=15)])]
    assert sm.tennis_signal(_tennis_cons(price=0.13,
                                         outcome="Jiri Lehecka"),
                            cheap) is None


def test_tennis_signal_fails_closed():
    # not a tennis title
    c = _tennis_cons()
    c["title"] = "Portugal vs. Spain: O/U 2.5"
    assert sm.tennis_signal(c, TENNIS_EVENTS) is None
    # ambiguous: two events with the same surname -> refuse
    dup = TENNIS_EVENTS + [dict(TENNIS_EVENTS[0],
                                event_ticker="KXATPMATCH-26JUL09LEHZVE")]
    assert sm.tennis_signal(_tennis_cons(), dup) is None
    # no matching event at all
    assert sm.tennis_signal(_tennis_cons(), TENNIS_EVENTS[1:]) is None


ADV_BINARY = [{
    "event_ticker": "KXWHATEVER-26JUL06USABEL",
    "title": "United States vs Belgium",
    "markets": [
        {"ticker": "KXWHATEVER-26JUL06USABEL-USA", "status": "active",
         "yes_sub_title": "United States", "yes_ask": 55, "yes_bid": 52},
        {"ticker": "KXWHATEVER-26JUL06USABEL-BEL", "status": "active",
         "yes_sub_title": "Belgium", "yes_ask": 47, "yes_bid": 44}]}]


def _adv_cons(price=0.53):
    return dict(slug="fifwc-usa-bel-2026-07-06-team-to-advance",
                title="United States vs. Belgium: Team to Advance",
                outcome="United States", wallets=7, stake=511274.0,
                avg_price=price)


def test_advance_maps_binary_knockout_market():
    assert sm.WC_ADV_RE.match(_adv_cons()["slug"])
    sig = sm.advance_signal(_adv_cons(), ADV_BINARY)
    assert sig and sig["ticker"] == "KXWHATEVER-26JUL06USABEL-USA"
    assert sig["price_cents"] == 55 and sig["wallets"] == 7


def test_advance_refuses_regulation_venue_with_tie():
    # a TIE market means regulation settlement — a DIFFERENT bet: refuse
    with_tie = [dict(ADV_BINARY[0], markets=ADV_BINARY[0]["markets"] + [
        {"ticker": "KXWHATEVER-26JUL06USABEL-TIE", "status": "active",
         "yes_sub_title": "Tie", "yes_ask": 25, "yes_bid": 22}])]
    assert sm.advance_signal(_adv_cons(), with_tie) is None


def test_advance_fails_closed_on_ambiguity():
    # no matching event
    assert sm.advance_signal(_adv_cons(), []) is None
    # two events matching both team names -> refuse
    dup = ADV_BINARY + [dict(ADV_BINARY[0], event_ticker="KXOTHER-USABEL")]
    assert sm.advance_signal(_adv_cons(), dup) is None
    # unparseable title
    c = _adv_cons()
    c["title"] = "Team to Advance"
    assert sm.advance_signal(c, ADV_BINARY) is None


class _MarketClient:
    def __init__(self, results):
        self._r = results

    def get_market(self, ticker):
        return {"result": self._r.get(ticker, "")}


def test_wallet_grading_blacklists_losers(tmp_path, monkeypatch):
    wl = tmp_path / "wallets.csv"
    bl = tmp_path / "blacklist.json"
    monkeypatch.setattr(sm, "BLACKLIST_PATH", bl)
    monkeypatch.setattr(sm, "BLACKLIST_MIN_SETTLED", 4)
    # loser backed four settled losers; winner backed four winners;
    # newbie has only one settled copy (below the minimum: never judged)
    for i in range(4):
        sm.log_copy_wallets(f"T{i}", "yes", 60, ["0xloser", "0xwinner"],
                            path=wl)
    sm.log_copy_wallets("T9", "yes", 60, ["0xnewbie"], path=wl)
    results = {f"T{i}": ("no" if True else "yes") for i in range(4)}
    # winner's markets actually settle yes... give winner separate tickers
    wl.unlink()
    for i in range(4):
        sm.log_copy_wallets(f"L{i}", "yes", 60, ["0xloser"], path=wl)
        sm.log_copy_wallets(f"W{i}", "yes", 60, ["0xwinner"], path=wl)
    sm.log_copy_wallets("N0", "yes", 60, ["0xnewbie"], path=wl)
    results = {f"L{i}": "no" for i in range(4)}
    results |= {f"W{i}": "yes" for i in range(4)}
    results["N0"] = "no"
    got = sm.grade_wallets(_MarketClient(results), path=wl, bl_path=bl)
    assert got == {"0xloser"}
    # outcomes persisted; re-grade is stable
    assert sm.grade_wallets(_MarketClient({}), path=wl, bl_path=bl) == {
        "0xloser"}
    # and selection excludes the graded-out wallet
    monkeypatch.setattr(sm, "fetch_big_trades", lambda: [
        _trade("0xloser", "x", "Yes", 0.5, 5000),
        _trade("0xwinner", "x", "Yes", 0.5, 4000)])
    day = 86400
    curve = [(NOW - 14 * day, 0.0), (NOW - 7 * day, 400.0),
             (NOW - day, 900.0)]
    monkeypatch.setattr(sm, "fetch_pnl_curve", lambda w: curve)
    monkeypatch.setattr(sm, "SHARP_MIN_PNL_2W", 500.0)
    sharps = sm.select_sharp_wallets()
    assert "0xloser" not in sharps and "0xwinner" in sharps


def test_consensus_and_signal_carry_wallet_ids(monkeypatch):
    trades = {w: [_trade(w, "mlb-phi-kc-2026-07-06", "Phillies", 0.6, 200)]
              for w in ("w1", "w2", "w3")}
    monkeypatch.setattr(sm, "fetch_wallet_buys",
                        lambda w, h, now_ts=None: trades[w])
    monkeypatch.setattr(sm, "MIN_WALLETS", 3)
    cons = sm.build_consensus({"w1": 1, "w2": 1, "w3": 1}, now_ts=NOW)
    assert cons[0]["wallet_ids"] == ["w1", "w2", "w3"]
    sig = sm.consensus_signal(cons[0], EVENTS)
    assert sig and sig["wallet_ids"] == ["w1", "w2", "w3"]
