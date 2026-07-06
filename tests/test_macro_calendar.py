"""Tests for the macro release calendar. Run: pytest tests/"""

from datetime import datetime, timezone

import macro_calendar as mc


def test_weekly_thursday_and_utc_dst():
    # A Wednesday in summer (EDT = UTC-4): next claims is Thu 8:30 ET = 12:30 UTC
    now = datetime(2026, 7, 8, 0, 0, tzinfo=timezone.utc)  # Wed Jul 8 2026
    ev = mc.next_releases(now, horizon_days=3)
    claims = [e for e in ev if e[1] == "Initial jobless claims"]
    assert claims
    when = claims[0][0]
    assert when.weekday() == 3 and when.hour == 12 and when.minute == 30


def test_events_sorted_and_future():
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    ev = mc.next_releases(now, horizon_days=21)
    assert ev == sorted(ev, key=lambda e: e[0])
    assert all(e[0] > now for e in ev)


def test_first_friday_payrolls():
    now = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    ev = mc.next_releases(now, horizon_days=10)
    nfp = [e for e in ev if e[1] == "Nonfarm payrolls"]
    assert nfp
    # first Friday of July 2026 is the 3rd
    assert nfp[0][0].strftime("%Y-%m-%d") == "2026-07-03"


def test_next_release_returns_one():
    now = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)
    nxt = mc.next_release(now)
    assert nxt and len(nxt) == 3
