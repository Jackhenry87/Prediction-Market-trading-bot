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
