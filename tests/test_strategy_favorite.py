"""Favourite-bias maker model.

The whole thesis is "buy the favourite, always as a maker", so the tests that
matter are the ones that stop it quietly becoming something else: taking the
longshot side, crossing the spread, or signalling on a market that cannot be
scored. Each of those would invalidate the CLV measurement this model exists
to produce.
"""

from datetime import datetime, timedelta, timezone

import strategy_favorite as fav

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _market(yes_bid=88, yes_ask=92, volume=500, hours=8, ticker="KXTEST-26AUG06-A"):
    return {"ticker": ticker, "yes_bid": yes_bid, "yes_ask": yes_ask,
            "volume": volume, "title": "test market",
            "close_time": (NOW + timedelta(hours=hours)).isoformat()}


# --- side selection ---------------------------------------------------------

def test_favourite_is_yes_when_yes_is_the_high_side():
    assert fav.favourite_side(_market(88, 92)) == ("yes", 88, 92)


def test_favourite_is_no_and_book_is_mirrored():
    # yes 8/12 -> the favourite is NO, whose book is 88/92 (100 - the mirror)
    assert fav.favourite_side(_market(8, 12)) == ("no", 88, 92)


def test_one_sided_or_crossed_book_is_skipped():
    assert fav.favourite_side(_market(0, 92)) is None      # no bid
    assert fav.favourite_side(_market(92, 88)) is None      # crossed


# --- the maker rule ---------------------------------------------------------

def test_entry_is_one_cent_inside_the_bid_and_never_crosses():
    sig = fav.evaluate_market(_market(88, 92), NOW)
    assert sig["price_cents"] == 89          # bid + 1
    assert sig["price_cents"] < 92           # strictly inside -> always a maker


def test_one_cent_spread_has_nowhere_to_rest():
    # bid+1 == ask would be a TAKER order. Must be refused, not "rounded".
    assert fav.evaluate_market(_market(90, 91), NOW) is None


# --- the band ---------------------------------------------------------------

def test_longshot_side_is_never_signalled():
    # A 10c market: the favourite is the NO side at 88/92, never the 8-12c YES.
    sig = fav.evaluate_market(_market(8, 12), NOW)
    assert sig is not None and sig["side"] == "no" and sig["price_cents"] == 89


def test_below_band_is_rejected():
    assert fav.evaluate_market(_market(60, 64), NOW) is None


def test_above_band_is_rejected():
    # 98/99 -> entry 99, above FAV_MAX_PRICE: too little left to win.
    assert fav.evaluate_market(_market(98, 100), NOW) is None


# --- scorability guards -----------------------------------------------------

def test_market_closing_too_soon_is_skipped():
    # No room for the price to move => no meaningful closing line => the
    # sample would be noise in the CLV average.
    assert fav.evaluate_market(_market(hours=0.5), NOW) is None


def test_unparseable_close_time_fails_closed():
    m = _market()
    m["close_time"] = "not a date"
    assert fav.evaluate_market(m, NOW) is None
    del m["close_time"]
    m.pop("expiration_time", None)
    assert fav.evaluate_market(m, NOW) is None


def test_thin_market_is_skipped():
    assert fav.evaluate_market(_market(volume=0), NOW) is None


# --- edge arithmetic --------------------------------------------------------

def test_edge_is_bias_plus_spread_saved_plus_fee_avoided():
    from strategy_weather import taker_fee_cents
    sig = fav.evaluate_market(_market(88, 92), NOW)
    expected = fav.FAV_BIAS_CENTS + (92 - 89) + taker_fee_cents(92)
    assert round(sig["ev_cents"], 6) == round(expected, 6)


def test_model_prob_records_the_assumption_under_test():
    sig = fav.evaluate_market(_market(88, 92), NOW)
    assert sig["model_prob"] == (89 + fav.FAV_BIAS_CENTS) / 100.0


# --- scan-level culls -------------------------------------------------------

class _Client:
    def __init__(self, markets):
        self.markets = markets

    def _request(self, method, path, params=None):
        return {"markets": self.markets, "cursor": None}


def test_scan_takes_at_most_one_market_per_event(monkeypatch):
    monkeypatch.setattr(fav, "MAX_PER_EVENT", 1)
    markets = [_market(ticker="KXHIGHNY-26AUG06-B80.5"),
               _market(ticker="KXHIGHNY-26AUG06-B84.5"),
               _market(ticker="KXHIGHCHI-26AUG06-B80.5")]
    results = fav.scan(_Client(markets), NOW)
    tickers = [s["ticker"] for r in results for s in r["signals"]]
    assert len(tickers) == 2                       # one NY + one CHI
    assert len({t.rsplit("-", 1)[0] for t in tickers}) == 2


def test_scan_caps_total_signals(monkeypatch):
    monkeypatch.setattr(fav, "MAX_SIGNALS", 2)
    monkeypatch.setattr(fav, "MAX_PER_EVENT", 99)
    markets = [_market(ticker=f"KXA-26AUG06-{i}") for i in range(10)]
    results = fav.scan(_Client(markets), NOW)
    assert sum(len(r["signals"]) for r in results) == 2


def test_scan_returns_empty_when_nothing_qualifies():
    assert fav.scan(_Client([_market(60, 64)]), NOW) == []


def test_one_bad_market_does_not_kill_the_scan():
    markets = [{"ticker": "BROKEN"}, _market(ticker="KXOK-26AUG06-A")]
    results = fav.scan(_Client(markets), NOW)
    assert [s["ticker"] for r in results for s in r["signals"]] == \
        ["KXOK-26AUG06-A"]


# --- the live API payload shape ---------------------------------------------
# These tests use the field names Kalshi ACTUALLY returns, captured from a
# real /markets response. The original fixtures used `volume` / `yes_bid` /
# `yes_ask`, none of which exist in the live payload: prices come back as
# *_dollars strings and volume as *_fp strings. The unit tests passed while
# the model signalled nothing in production, which is the failure mode these
# guard against.

LIVE_KEYS = {
    "ticker": "KXHIGHNY-26AUG06-B84.5",
    "status": "active",
    "title": "Will the high temp in NYC be 84-85 on Aug 6?",
    "yes_bid_dollars": "0.8800",
    "yes_ask_dollars": "0.9200",
    "no_bid_dollars": "0.0800",
    "no_ask_dollars": "0.1200",
    "last_price_dollars": "0.9000",
    "volume_fp": "1543.00",
    "volume_24h_fp": "212.00",
    "open_interest_fp": "845.00",
    "liquidity_dollars": "412.5500",
    "close_time": (NOW + timedelta(hours=8)).isoformat(),
}


def test_volume_reads_the_fp_field_not_the_absent_plain_one():
    assert "volume" not in LIVE_KEYS          # the live payload really lacks it
    assert fav.market_volume(LIVE_KEYS) == 1543.0
    assert fav.market_volume({"volume": 7}) == 7.0          # older vintage
    assert fav.market_volume({"volume_24h_fp": "9.00"}) == 9.0
    assert fav.market_volume({}) == 0.0
    assert fav.market_volume({"volume_fp": "not-a-number"}) == 0.0


def test_live_payload_produces_a_signal():
    # The whole bug in one assertion: with real field names this must signal.
    sig = fav.evaluate_market(LIVE_KEYS, NOW)
    assert sig is not None, "live payload must not be silently filtered out"
    assert sig["side"] == "yes" and sig["price_cents"] == 89


def test_live_payload_with_no_volume_is_still_rejected():
    thin = dict(LIVE_KEYS, volume_fp="0.00", volume_24h_fp="0.00")
    assert fav.evaluate_market(thin, NOW) is None


def test_multileg_combo_markets_are_excluded():
    # KXMVE* cross-category combos dominate the prod listing and are parlays,
    # not the single binary contracts the research describes.
    combo = dict(LIVE_KEYS, ticker="KXMVECROSSCATEGORY-S2026B9F7-A7E40")
    assert fav.evaluate_market(combo, NOW) is None


def test_listing_is_windowed_by_close_time_and_paced():
    captured = []

    class _C:
        def _request(self, method, path, params=None):
            captured.append(params)
            return {"markets": [], "cursor": None}

    fav.list_open_markets(_C(), now=NOW)
    p = captured[0]
    assert p["status"] == "open"
    # must not page the whole far-dated exchange
    assert p["max_close_ts"] - p["min_close_ts"] == int(fav.MAX_DAYS_OUT * 86400)


def test_listing_stops_at_the_page_cap(monkeypatch):
    monkeypatch.setattr(fav, "PAGE_CAP", 3)
    monkeypatch.setattr(fav, "PAGE_PAUSE_S", 0)
    calls = []

    class _C:
        def _request(self, method, path, params=None):
            calls.append(1)
            return {"markets": [dict(LIVE_KEYS)], "cursor": "more"}

    got = fav.list_open_markets(_C(), now=NOW)
    assert len(calls) == 3 and len(got) == 3
