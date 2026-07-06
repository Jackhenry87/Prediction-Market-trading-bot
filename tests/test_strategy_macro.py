"""Tests for the macro resolution-lag model. Run: pytest tests/"""

import strategy_macro as sm


def test_fetch_fred_requests_newest_and_returns_newest_last(monkeypatch):
    """FRED applies 'limit' AFTER sorting: asc+limit returns the OLDEST rows
    of the whole series. The first paper run caught 'current' unemployment
    coming back as Jan 1950. Must request desc and re-order newest-last."""
    captured = {}

    class R:
        def raise_for_status(self): pass
        def json(self):
            return {"observations": [   # what FRED returns for desc
                {"date": "2026-06-01", "value": "4.1"},
                {"date": "2026-05-01", "value": "4.2"},
                {"date": "2026-04-01", "value": "."},   # gap dropped
                {"date": "2026-03-01", "value": "4.0"},
            ]}

    def fake_get(url, params=None, timeout=None):
        captured.update(params)
        return R()

    monkeypatch.setattr(sm.requests, "get", fake_get)
    obs = sm.fetch_fred("UNRATE", "key")
    assert captured["sort_order"] == "desc"
    assert obs == [("2026-03-01", 4.0), ("2026-05-01", 4.2),
                   ("2026-06-01", 4.1)]
    assert obs[-1][0] == "2026-06-01"   # newest LAST


def test_event_period_parsing():
    assert sm.event_period("KXU3-26NOV") == (2026, 11, None)
    assert sm.event_period("KXJOBLESSCLAIMS-26JUL09") == (2026, 7, 9)
    assert sm.event_period("KXCPIYOY-26JUN") == (2026, 6, None)
    assert sm.event_period("GARBAGE") is None
    assert sm.event_period("") is None
    assert sm.event_period("KXU3-26XYZ") is None   # not a month


def test_monthly_market_must_match_reference_month():
    # June observation settles the June market, and ONLY the June market —
    # the other bug the paper run caught (Nov market priced off June print).
    assert sm.event_matches_observation("KXU3-26JUN", "2026-06-01")
    assert not sm.event_matches_observation("KXU3-26NOV", "2026-06-01")
    assert not sm.event_matches_observation("KXU3-27JUN", "2026-06-01")
    assert not sm.event_matches_observation("KXCPIYOY-26SEP", "2026-06-01")


def test_weekly_claims_release_matches_week_ending():
    # claims for week ending Sat Jul 4 are released Thu Jul 9 (5 days later)
    assert sm.event_matches_observation("KXJOBLESSCLAIMS-26JUL09", "2026-07-04")
    # the NEXT release is a different week's number
    assert not sm.event_matches_observation("KXJOBLESSCLAIMS-26JUL16",
                                            "2026-07-04")
    # and a release can't precede its own week's end
    assert not sm.event_matches_observation("KXJOBLESSCLAIMS-26JUL02",
                                            "2026-07-04")


def test_unparseable_period_refuses_certainty():
    assert not sm.event_matches_observation("KXWEIRD", "2026-06-01")
    assert not sm.event_matches_observation("KXU3-26JUN", "not-a-date")


def test_transforms():
    obs = [(f"2025-{m:02d}-01", 100.0 + m) for m in range(1, 14)]  # 13 months
    # level = latest value
    assert sm.latest_actual(obs, "level")[1] == 113.0
    # yoy_pct = (latest/12-months-ago - 1)*100 = (113/101 - 1)*100
    assert abs(sm.latest_actual(obs, "yoy_pct")[1] - (113/101 - 1) * 100) < 1e-9
    # mom change in jobs = (latest - prev) * 1000
    assert abs(sm.latest_actual(obs, "mom_change_jobs")[1] - 1000.0) < 1e-9


def test_known_outcome_yes_side():
    # actual 3.2 lands in [3.0, 3.5) -> YES certain; ask 88c -> buy YES +EV
    m = {"ticker": "T", "floor_strike": 3.0, "cap_strike": 3.5,
         "yes_ask": 88, "yes_bid": 84}
    s = sm.known_outcome_signal(m, 3.2)
    assert s["side"] == "yes" and s["model_prob"] == 1.0 and s["ev_cents"] > 3


def test_known_outcome_no_side():
    # actual 4.0 is NOT in [3.0, 3.5) -> NO certain; yes_bid 60 -> NO costs 40c
    m = {"ticker": "T", "floor_strike": 3.0, "cap_strike": 3.5,
         "yes_ask": 66, "yes_bid": 60}
    s = sm.known_outcome_signal(m, 4.0)
    assert s["side"] == "no" and s["ev_cents"] > 3


def test_no_signal_when_already_priced():
    # YES certain but already at 99c -> no lag left to capture
    m = {"ticker": "T", "floor_strike": 3.0, "cap_strike": 3.5,
         "yes_ask": 99, "yes_bid": 98}
    assert sm.known_outcome_signal(m, 3.2) is None


def test_tail_market_open_bounds():
    # "3.5 or above": floor 3.5, no cap. actual 4.0 -> YES certain
    m = {"ticker": "T", "floor_strike": 3.5, "cap_strike": None,
         "yes_ask": 80, "yes_bid": 76}
    assert sm.known_outcome_signal(m, 4.0)["side"] == "yes"
