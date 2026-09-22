"""Scan-only arbitrage recorder.

This module exists to answer one empirical question — do risk-free baskets
actually appear on Kalshi at our cadence? — so the tests that matter are the
ones protecting the integrity of that measurement: it must never place an
order, must not invent sightings, and must not silently swallow a broken scan.
"""

import csv

import arb_scan


def _arb(event="KXTEST-26AUG07", cost=97.0, profit=2.5, side="yes", n=3):
    return dict(event_ticker=event, title="Who wins the thing?", side=side,
                n=n, cost_cents=cost, fees_cents=0.5,
                payout_cents=100.0, profit_cents=profit)


# --- it must not be able to trade ------------------------------------------

def test_module_imports_no_order_path():
    # The whole safety argument is that this file cannot place. If someone
    # later imports an executor here, this test is the thing that objects.
    src = open(arb_scan.__file__).read()
    for forbidden in ("create_limit_order", "favorite_runner", "log_execution",
                      "check_order", "place_signals"):
        assert forbidden not in src, f"arb_scan must not reference {forbidden}"


# --- recording --------------------------------------------------------------

def test_records_one_row_per_basket_with_header(tmp_path):
    p = tmp_path / "arb_scan.csv"
    n = arb_scan.record([_arb("A"), _arb("B")], path=p, now="2026-08-07T00:00:00")
    assert n == 2
    rows = list(csv.DictReader(open(p)))
    assert [r["event_ticker"] for r in rows] == ["A", "B"]
    assert rows[0]["profit_cents"] == "2.5"
    assert rows[0]["scanned_at_utc"] == "2026-08-07T00:00:00"


def test_header_written_once_across_appends(tmp_path):
    p = tmp_path / "arb_scan.csv"
    arb_scan.record([_arb("A")], path=p, now="t1")
    arb_scan.record([_arb("B")], path=p, now="t2")
    lines = [l for l in open(p).read().splitlines() if l.strip()]
    assert len(lines) == 3                       # header + two sightings
    assert lines[0].startswith("scanned_at_utc")


def test_empty_scan_writes_nothing(tmp_path):
    # A quiet market must leave no trace, or "no arbs exist" and "the scanner
    # ran" become indistinguishable in the file.
    p = tmp_path / "arb_scan.csv"
    assert arb_scan.record([], path=p) == 0
    assert not p.exists()


def test_repeat_sightings_are_kept_not_deduped(tmp_path):
    # Deliberate: the questions are how OFTEN a basket appears and how LONG it
    # survives. Deduping by event would destroy exactly that signal.
    p = tmp_path / "arb_scan.csv"
    arb_scan.record([_arb("A")], path=p, now="t1")
    arb_scan.record([_arb("A")], path=p, now="t2")
    rows = list(csv.DictReader(open(p)))
    assert len(rows) == 2 and {r["scanned_at_utc"] for r in rows} == {"t1", "t2"}


def test_profit_percentage_is_relative_to_cost(tmp_path):
    p = tmp_path / "arb_scan.csv"
    arb_scan.record([_arb(cost=50.0, profit=5.0)], path=p, now="t")
    assert list(csv.DictReader(open(p)))[0]["profit_pct_of_cost"] == "10.00"


def test_zero_cost_does_not_divide_by_zero(tmp_path):
    p = tmp_path / "arb_scan.csv"
    arb_scan.record([_arb(cost=0.0, profit=1.0)], path=p, now="t")
    assert list(csv.DictReader(open(p)))[0]["profit_pct_of_cost"] == ""


# --- the summary that answers the question ---------------------------------

def test_summary_before_any_sighting(tmp_path):
    assert "no sightings" in arb_scan.summarise(tmp_path / "nope.csv")


def test_summary_counts_distinct_events_and_days(tmp_path):
    p = tmp_path / "arb_scan.csv"
    arb_scan.record([_arb("A", profit=2.0), _arb("B", profit=9.0)],
                    path=p, now="2026-08-07T01:00:00")
    arb_scan.record([_arb("A", profit=3.0)], path=p, now="2026-08-08T01:00:00")
    s = arb_scan.summarise(p)
    assert "3 takeable sighting(s)" in s and "2 event(s)" in s
    assert "2 day(s)" in s and "+9.0c" in s


def test_summary_survives_a_malformed_row(tmp_path):
    p = tmp_path / "arb_scan.csv"
    arb_scan.record([_arb("A", profit=4.0)], path=p, now="2026-08-07T01:00:00")
    with open(p, "a") as fh:
        fh.write("2026-08-07T02:00:00,B,junk,yes,3,x,y,z,NOT_A_NUMBER,\n")
    s = arb_scan.summarise(p)          # must not raise
    assert "sighting(s)" in s and "+4.0c" in s


def test_takeable_and_restable_are_never_pooled(tmp_path):
    # A maker basket posting at 47c reports +53c. Reported next to a +2.3c
    # takeable one it would read as the better find, when it is the one nobody
    # has filled. They must be counted apart.
    p = tmp_path / "arb_scan.csv"
    arb_scan.record([_arb("A", profit=2.3),
                     _arb("B", profit=53.0, side="yes-maker")],
                    path=p, now="2026-08-21T01:00:00")
    s = arb_scan.summarise(p)
    assert "1 takeable sighting(s)" in s and "best +2.3c" in s
    assert "1 restable sighting(s)" in s and "best +53.0c" in s
