"""Tests for the smart-money (Polymarket sharp-wallet consensus) model."""

import csv
from datetime import datetime, timezone

import strategy_smartmoney as sm

NOW = 1_783_360_000.0
DAY = 86400


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


def _daily_curve(values, end=NOW):
    """cumulative-PnL curve, one point per day ending today."""
    n = len(values)
    return [(end - (n - 1 - i) * DAY, v) for i, v in enumerate(values)]


def test_curve_quality_flags_one_lucky_day():
    # steady daily gains: healthy shape
    steady = sm.curve_quality(_daily_curve([0, 100, 200, 300, 400, 500, 600]),
                              now_ts=NOW)
    assert steady["up_frac"] == 1.0 and steady["max_share"] < 0.3
    # flat for a week then one huge day: concentrated, low quality
    spike = sm.curve_quality(_daily_curve([0, 10, 20, 30, 40, 1000, 1010]),
                             now_ts=NOW)
    assert spike["max_share"] > 0.8
    # a steadier earner has the higher risk-adjusted (Sharpe) score even
    # with a much smaller headline number
    vol = sm.curve_quality(_daily_curve([0, 500, 200, 700, 400, 900, 1200]),
                           now_ts=NOW)
    assert steady["sharpe"] > vol["sharpe"]


def test_select_ranks_by_risk_adjusted_not_size(monkeypatch):
    tape = [_trade("0xsteady", "x", "Yes", 0.5, 2000),
            _trade("0xvol", "x", "Yes", 0.5, 2000),
            _trade("0xspike", "x", "Yes", 0.5, 2000)]
    curves = {
        # steady +100/day: modest total, top risk-adjusted return
        "0xsteady": _daily_curve([0, 100, 200, 300, 400, 500, 600]),
        # bigger headline PnL but jumpy: lower Sharpe -> ranks below steady
        "0xvol": _daily_curve([0, 500, 200, 700, 400, 900, 1200]),
        # all profit from a single day: rejected outright by the spike gate
        "0xspike": _daily_curve([0, 10, 20, 30, 40, 1000, 1010]),
    }
    monkeypatch.setattr(sm, "fetch_big_trades", lambda: tape)
    monkeypatch.setattr(sm, "fetch_pnl_curve", lambda w: curves[w])
    monkeypatch.setattr(sm, "SHARP_MIN_PNL_2W", 500.0)
    monkeypatch.setattr(sm, "load_blacklist", lambda: set())
    sharps = sm.select_sharp_wallets()
    assert "0xspike" not in sharps                 # one-day spike filtered
    assert list(sharps)[0] == "0xsteady"           # risk-adjusted beats size


def test_category_of_buckets():
    assert sm.category_of("KXWTAMATCH-26JUL07PEGGAU-PEG") == "tennis"
    assert sm.category_of("KXMLBGAME-26JUL09COLSF-COL") == "usleague"
    assert sm.category_of("KXMENWORLDCUP-26-ES") == "soccer"
    assert sm.category_of("KXSENATE-26-XYZ") == "other"


def test_grade_wallets_bars_by_category(tmp_path, monkeypatch):
    # 0xmix is NET POSITIVE overall (not globally blacklisted) but loses in
    # tennis specifically -> barred from tennis only, still allowed in soccer
    log = tmp_path / "wlog.csv"
    rows = [sm.WALLET_LOG_COLUMNS]
    for i in range(4):
        rows.append(["t", f"KXWTAMATCH-x-{i}", "yes", "40", "0xmix",
                     "loss (-30c)"])
    for i in range(4):
        rows.append(["t", f"KXMENWORLDCUP-26-{i}", "yes", "20", "0xmix",
                     "win (+80c)"])
    with open(log, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)
    monkeypatch.setattr(sm, "CAT_BARS_PATH", tmp_path / "cats.json")
    monkeypatch.setattr(sm, "BLACKLIST_PATH", tmp_path / "bl.json")
    bl = sm.grade_wallets(client=None, path=log, bl_path=tmp_path / "bl.json")
    assert "0xmix" not in bl                        # net +200 overall
    assert sm.category_allowed("0xmix", "soccer") is True
    assert sm.category_allowed("0xmix", "tennis") is False


def test_score_copier_clv_samples_early(tmp_path, monkeypatch):
    import kalshi_client
    from ledger import LEDGER_COLUMNS

    def _iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()

    log = tmp_path / "paper_trades_smartmoney.csv"
    base = {c: "" for c in LEDGER_COLUMNS}
    rows = [LEDGER_COLUMNS]

    def _row(ticker, side, price, scanned_ts):
        r = dict(base, scanned_at_utc=_iso(scanned_ts), event="E",
                 ticker=ticker, side=side, price_cents=str(price),
                 model_prob="0.6", ev_cents="5")
        return [r[c] for c in LEDGER_COLUMNS]

    rows.append(_row("T-fresh", "yes", 60, NOW - 4 * 3600))    # old enough
    rows.append(_row("T-recent", "yes", 60, NOW - 1 * 3600))   # too recent
    rows.append(_row("T-settled", "yes", 60, NOW - 4 * 3600))  # already settled
    with open(log, "w", newline="") as fh:
        csv.writer(fh).writerows(rows)

    markets = {
        "T-fresh": {"last_price": 70},                 # line moved our way
        "T-recent": {"last_price": 70},
        "T-settled": {"result": "yes", "last_price": 100},
    }

    class _Fake:
        def __init__(self, *a, **k):
            pass

        def get_market(self, t):
            return markets[t]
    monkeypatch.setattr(kalshi_client, "KalshiClient", _Fake)

    sm.score_copier_clv(path=log, now_ts=NOW)

    with open(log, newline="") as fh:
        out = list(csv.DictReader(fh))
    by = {r["ticker"]: r for r in out}
    assert by["T-fresh"]["clv_cents"] == "+10"     # 70 - 60, sampled early
    assert by["T-recent"]["clv_cents"] == ""       # lag not elapsed
    assert by["T-settled"]["clv_cents"] == ""      # settled before sampling


def test_backers_multiplier_tilts_within_band(monkeypatch):
    scores = {"good": {"n": 6, "net": 180.0},   # +30c/copy -> full up-tilt
              "bad": {"n": 6, "net": -180.0},    # -30c/copy -> full down-tilt
              "thin": {"n": 2, "net": 200.0}}    # too few settled -> ignored
    monkeypatch.setattr(sm, "PROVEN_MIN", 5)
    monkeypatch.setattr(sm, "QUALITY_SCALE", 30.0)
    monkeypatch.setattr(sm, "QUALITY_BAND", 0.30)
    assert abs(sm.backers_multiplier(["good"], scores) - 1.30) < 1e-9
    assert abs(sm.backers_multiplier(["bad"], scores) - 0.70) < 1e-9
    assert sm.backers_multiplier(["thin"], scores) == 1.0     # not proven yet
    assert sm.backers_multiplier([], scores) == 1.0
    assert abs(sm.backers_multiplier(["good", "bad"], scores) - 1.0) < 1e-9


def test_wallet_scores_parse(tmp_path):
    log = tmp_path / "wlog.csv"
    with open(log, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(sm.WALLET_LOG_COLUMNS)
        w.writerow(["t", "K1", "yes", "40", "0xA", "win (+60c)"])
        w.writerow(["t", "K2", "yes", "40", "0xA", "loss (-40c)"])
        w.writerow(["t", "K3", "yes", "40", "0xB", ""])          # unsettled
    s = sm.wallet_scores(log)
    assert s["0xA"] == {"n": 2, "net": 20.0}
    assert "0xB" not in s                                        # not scored


def test_sharp_exit_fraction(monkeypatch):
    since = NOW - 3600
    recent = {
        "a": [_trade("a", "m", "A", 0.5, 100, NOW - 100, "SELL")],   # sold ours
        "b": [_trade("b", "m", "B", 0.5, 100, NOW - 100, "SELL")],   # other side
        "c": [_trade("c", "m", "A", 0.5, 100, NOW - 9000, "SELL")],  # pre-entry
        "d": [_trade("d", "m", "A", 0.5, 100, NOW - 100, "BUY")],    # not a sell
    }
    monkeypatch.setattr(sm, "fetch_wallet_recent", lambda w: recent[w])
    frac = sm.sharp_exit_fraction("m", "A", list("abcd"), since, now=NOW)
    assert abs(frac - 0.25) < 1e-9                              # only 'a' exited


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
    monkeypatch.setattr(sm, "fetch_wallet_recent", lambda w: [])   # offline
    monkeypatch.setattr(sm, "MIN_WALLETS", 3)
    cons = sm.build_consensus({"w1": 1, "w2": 1, "w3": 1}, now_ts=NOW)
    assert len(cons) == 1                      # Lakers had only 1 sharp
    c = cons[0]
    assert c["slug"] == "mlb-phi-kc-2026-07-06" and c["wallets"] == 3
    # pricing stays RAW stake-weighted (conviction only affects ranking)
    total = 0.60 * 200 * 0.60 + 0.62 * 300 * 0.62 + 0.58 * 150 * 0.58 \
        + 0.61 * 500 * 0.61
    stake = 0.60 * 200 + 0.62 * 300 + 0.58 * 150 + 0.61 * 500
    assert abs(c["avg_price"] - total / stake) < 1e-9


def test_wash_wallet_dropped_from_consensus(monkeypatch):
    # a wallet that bought BOTH outcomes of one market is an MM/wash trader
    # and must not vote; a clean wallet on the same pick still counts
    recent = {
        "wash": [_trade("wash", "mlb-x-2026-07-06", "A", 0.5, 200),
                 _trade("wash", "mlb-x-2026-07-06", "B", 0.5, 200)],
        "w2": [_trade("w2", "mlb-x-2026-07-06", "A", 0.5, 200)],
        "w3": [_trade("w3", "mlb-x-2026-07-06", "A", 0.5, 200)],
    }
    monkeypatch.setattr(sm, "fetch_wallet_recent", lambda w: recent[w])
    monkeypatch.setattr(sm, "fetch_wallet_buys",
                        lambda w, h, now_ts=None:
                        [t for t in recent[w] if t["outcome"] == "A"])
    monkeypatch.setattr(sm, "MIN_WALLETS", 3)
    # wash dropped -> only 2 clean sharps -> below MIN_WALLETS -> no consensus
    assert sm.build_consensus({"wash": 1, "w2": 1, "w3": 1}, now_ts=NOW) == []
    # without the wash wallet's noise, lowering the bar shows the clean pick
    monkeypatch.setattr(sm, "MIN_WALLETS", 2)
    cons = sm.build_consensus({"wash": 1, "w2": 1, "w3": 1}, now_ts=NOW)
    assert len(cons) == 1 and cons[0]["wallets"] == 2


def test_conviction_ranks_above_raw_size(monkeypatch):
    # 'big' bets its usual size; 'sharp' bets 10x its usual — even though
    # 'big' has more raw dollars, the high-conviction pick ranks first
    recent = {
        "big": [_trade("big", "m", "A", 0.5, 400) for _ in range(6)],   # normal ~200
        "sharp": [_trade("sharp", "m", "A", 0.5, 20)] * 5,              # normal ~10
    }
    buys = {
        "big": [_trade("big", "mlb-big-2026-07-06", "A", 0.5, 400)],    # $200
        "sharp": [_trade("sharp", "mlb-shp-2026-07-06", "A", 0.5, 200)],  # $100
    }
    monkeypatch.setattr(sm, "fetch_wallet_recent", lambda w: recent[w])
    monkeypatch.setattr(sm, "fetch_wallet_buys",
                        lambda w, h, now_ts=None: buys[w])
    monkeypatch.setattr(sm, "MIN_WALLETS", 1)
    cons = sm.build_consensus({"big": 1, "sharp": 1}, now_ts=NOW)
    assert [c["slug"] for c in cons][0] == "mlb-shp-2026-07-06"   # conviction
    assert cons[0]["stake"] < cons[1]["stake"]                    # despite less $


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
    monkeypatch.setattr(sm, "fetch_wallet_recent", lambda w: [])   # offline
    monkeypatch.setattr(sm, "MIN_WALLETS", 3)
    cons = sm.build_consensus({"w1": 1, "w2": 1, "w3": 1}, now_ts=NOW)
    assert cons[0]["wallet_ids"] == ["w1", "w2", "w3"]
    sig = sm.consensus_signal(cons[0], EVENTS)
    assert sig and sig["wallet_ids"] == ["w1", "w2", "w3"]


WC_WINNER_EVENTS = [{
    "event_ticker": "KXMENWORLDCUP-26", "title": "2026 FIFA World Cup Winner",
    "markets": [
        {"ticker": "KXMENWORLDCUP-26-EN", "status": "active",
         "yes_sub_title": "England", "yes_ask": 32, "yes_bid": 29},
        {"ticker": "KXMENWORLDCUP-26-FR", "status": "active",
         "yes_sub_title": "France", "yes_ask": 28, "yes_bid": 25}]}]


def test_wc_winner_futures_map():
    cons = dict(slug="will-england-win-the-2026-fifa-world-cup",
                title="Will England win the 2026 FIFA World Cup?",
                outcome="Yes", wallets=4, stake=90000.0, avg_price=0.30)
    sig = sm.wc_winner_signal(cons, WC_WINNER_EVENTS)
    assert sig and sig["ticker"] == "KXMENWORLDCUP-26-EN"
    # a different competition's futures never map here
    cons["title"] = "Will England win the 2026 Premier League?"
    assert sm.wc_winner_signal(cons, WC_WINNER_EVENTS) is None


UFC_EVENTS = [{
    "event_ticker": "KXUFCFIGHT-26JUL12JONASP", "title": "Jones vs Aspinall",
    "markets": [
        {"ticker": "KXUFCFIGHT-26JUL12JONASP-JON", "status": "active",
         "yes_sub_title": "Jones", "yes_ask": 58, "yes_bid": 55},
        {"ticker": "KXUFCFIGHT-26JUL12JONASP-ASP", "status": "active",
         "yes_sub_title": "Aspinall", "yes_ask": 44, "yes_bid": 41}]}]


def _ufc_cons(title="UFC 320: Jones vs Aspinall", outcome="Jones",
              price=0.55):
    return dict(slug="ufc-320-jones-aspinall", title=title, outcome=outcome,
                wallets=3, stake=40000.0, avg_price=price)


def test_generic_vs_discovery_maps_any_h2h():
    sig = sm.generic_vs_signal(_ufc_cons(), UFC_EVENTS)
    assert sig and sig["ticker"] == "KXUFCFIGHT-26JUL12JONASP-JON"
    assert sig["price_cents"] == 58


def test_generic_vs_rejects_props_and_ambiguity():
    # set/game/segment props are DIFFERENT bets — never discovery-mapped
    assert sm.generic_vs_signal(
        _ufc_cons(title="Set 4 Winner: Lehecka vs Zverev"), UFC_EVENTS) is None
    assert sm.generic_vs_signal(
        _ufc_cons(title="Dota 2: REKONIX vs Vici Gaming - Game 1 Winner"),
        UFC_EVENTS) is None
    assert sm.generic_vs_signal(
        _ufc_cons(title="Jones vs Aspinall: O/U 2.5 Rounds", outcome="Over"),
        UFC_EVENTS) is None
    # outcome must BE one of the two sides
    assert sm.generic_vs_signal(_ufc_cons(outcome="Under"), UFC_EVENTS) is None
    # two matching events -> refuse
    dup = UFC_EVENTS + [dict(UFC_EVENTS[0], event_ticker="KXOTHER-JONASP")]
    assert sm.generic_vs_signal(_ufc_cons(), dup) is None
    # a TIE market means it's not a binary winner venue -> refuse
    with_tie = [dict(UFC_EVENTS[0], markets=UFC_EVENTS[0]["markets"] + [
        {"ticker": "X-TIE", "status": "active", "yes_sub_title": "Tie",
         "yes_ask": 5, "yes_bid": 3}])]
    assert sm.generic_vs_signal(_ufc_cons(), with_tie) is None


POL_EVENTS = [
    {"event_ticker": "KXSENATEAZ-26", "title": "Arizona Senate Race Winner",
     "sub_title": "2026 general election",
     "markets": [
         {"ticker": "KXSENATEAZ-26-GAL", "status": "active",
          "yes_sub_title": "Ruben Gallego", "yes_ask": 58, "yes_bid": 55},
         {"ticker": "KXSENATEAZ-26-LAK", "status": "active",
          "yes_sub_title": "Kari Lake", "yes_ask": 43, "yes_bid": 40}]},
    {"event_ticker": "KXSENATEAZPRIM-26",
     "title": "Arizona Senate Primary Winner",
     "sub_title": "Republican primary",
     "markets": [
         {"ticker": "KXSENATEAZPRIM-26-LAK", "status": "active",
          "yes_sub_title": "Kari Lake", "yes_ask": 67, "yes_bid": 64}]},
]


def _pol_cons(title, outcome="Yes", price=0.55):
    return dict(slug="x", title=title, outcome=outcome, wallets=4,
                stake=50000.0, avg_price=price)


def test_politics_maps_exact_race():
    sig = sm.politics_signal(
        _pol_cons("Will Ruben Gallego win the Arizona Senate race?"),
        POL_EVENTS)
    assert sig and sig["ticker"] == "KXSENATEAZ-26-GAL"


def test_politics_qualifier_guard_primary_vs_general():
    # primary consensus must ONLY match the primary event (qualifier sets
    # must agree exactly), and Lake appears in both -> without the
    # qualifier guard this would be a classic wrong-market copy
    sig = sm.politics_signal(
        _pol_cons("Will Kari Lake win the Arizona Senate primary?",
                  price=0.65),
        POL_EVENTS)
    assert sig and sig["ticker"] == "KXSENATEAZPRIM-26-LAK"
    # general-election consensus on Lake maps to the GENERAL race market
    sig = sm.politics_signal(
        _pol_cons("Will Kari Lake win the Arizona Senate race?",
                  price=0.41),
        POL_EVENTS)
    assert sig and sig["ticker"] == "KXSENATEAZ-26-LAK"


def test_politics_fails_closed():
    # margin/turnout style props are different bets
    assert sm.politics_signal(
        _pol_cons("Will Gallego win the Arizona Senate race by 5 points?"),
        POL_EVENTS) is None
    # unknown candidate/race -> no event match
    assert sm.politics_signal(
        _pol_cons("Will John Smith win the Ohio Senate race?"),
        POL_EVENTS) is None
    # ambiguity: same candidate+race matching two events -> refuse
    dup = POL_EVENTS + [dict(POL_EVENTS[0], event_ticker="KXSENATEAZB-26")]
    assert sm.politics_signal(
        _pol_cons("Will Ruben Gallego win the Arizona Senate race?"),
        dup) is None
