"""Upcoming US macro release times (UTC), for the release-time runner.

Most US data drops at 8:30am ET; FOMC at 2:00pm ET. ET→UTC is handled via
zoneinfo so daylight saving is correct. Weekly/first-Friday releases are
rule-based; CPI and FOMC have no simple rule, so their dates live in
EXTRA_RELEASES and must be updated each year from the official schedule
(bls.gov / federalreserve.gov).
"""

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# One-off releases with no simple recurrence rule. UPDATE yearly from the
# official calendars. (Examples below — replace with the real schedule.)
EXTRA_RELEASES = [
    # ("2026-07-15", time(8, 30), "CPI", "CPIAUCSL"),
    # ("2026-07-29", time(14, 0), "FOMC decision", "FEDFUNDS"),
]


def _et_to_utc(d: datetime.date, t: time) -> datetime:
    return datetime.combine(d, t, tzinfo=ET).astimezone(timezone.utc)


def _weekly_thursdays(start, end):
    """Initial jobless claims: every Thursday 8:30 ET."""
    d = start.date()
    while d <= end.date():
        if d.weekday() == 3:  # Thursday
            yield (_et_to_utc(d, time(8, 30)), "Initial jobless claims", "ICSA")
        d += timedelta(days=1)


def _first_fridays(start, end):
    """Employment Situation (nonfarm payrolls): first Friday 8:30 ET."""
    d = start.replace(day=1).date()
    while d <= end.date():
        # first Friday of this month
        first = d.replace(day=1)
        offset = (4 - first.weekday()) % 7
        friday = first + timedelta(days=offset)
        if start.date() <= friday <= end.date():
            yield (_et_to_utc(friday, time(8, 30)),
                   "Nonfarm payrolls", "PAYEMS")
        # advance to next month
        d = (first.replace(day=28) + timedelta(days=7)).replace(day=1)


def next_releases(now: datetime = None, horizon_days: int = 14) -> list:
    """Sorted upcoming (utc_datetime, name, fred_series) within the horizon."""
    now = now or datetime.now(timezone.utc)
    end = now + timedelta(days=horizon_days)
    events = list(_weekly_thursdays(now, end)) + list(_first_fridays(now, end))
    for date_str, t, name, series in EXTRA_RELEASES:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        events.append((_et_to_utc(d, t), name, series))
    events = [e for e in events if e[0] > now]
    events.sort(key=lambda e: e[0])
    return events


def next_release(now: datetime = None):
    upcoming = next_releases(now, horizon_days=40)
    return upcoming[0] if upcoming else None
