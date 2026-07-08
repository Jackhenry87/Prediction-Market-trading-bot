"""Tests for the portfolio spreadsheet generator (kalshi_report)."""

import kalshi_report as kr


def test_ticker_city_and_model():
    assert kr.ticker_city("KXHIGHMIA-26JUL07-B90.5") == "Miami"
    assert kr.ticker_city("KXHIGHNY-26JUL08-B82.5") == "New York"
    assert kr.ticker_city("KXMLBGAME-26JUL072210COLLAD-COL") == ""
    assert kr.ticker_model("KXHIGHDEN-26JUL07-B93.5") == "weather"
    assert kr.ticker_model("KXMLBGAME-26JUL07-COL") == "sports"
    assert kr.ticker_model("KXATPMATCH-26JUL07AUGDJO-DJO") == "tennis"


def test_open_row_marks_to_market():
    mp = {"ticker": "KXHIGHMIA-26JUL07-B90.5", "position": -2,
          "market_exposure": 140}   # No x2, cost $1.40
    mkt = {"title": "Miami temp?", "yes_sub_title": "90-91",
           "close_time": "2026-07-07T23:59:00Z", "last_price": 1}
    r = kr.open_row(mp, mkt, {}, {})
    assert r["side"] == "no" and r["count"] == "2" and r["status"] == "open"
    assert r["city"] == "Miami" and r["model"] == "weather"
    # lead column is the readable title, ticker kept for reference
    assert r["market"] == "Miami temp? · 90-91"
    assert r["ticker"] == "KXHIGHMIA-26JUL07-B90.5"
    # last yes=1c -> no worth 99c each -> $1.98 vs $1.40 -> +$0.58
    assert r["unrealized_usd"] == "+0.58" and r["copyTrade"] == ""


def test_settled_row_realized_pnl():
    s = {"ticker": "KXHIGHNY-26JUL07-T75", "market_result": "yes",
         "yes_count": 1, "no_count": 0, "revenue_dollars": 1.0,
         "yes_total_cost_dollars": 0.83, "no_total_cost_dollars": 0.0}
    r = kr.settled_row(s, {"title": "NY temp?", "yes_sub_title": "74 or below"},
                       {}, {})
    assert r["status"] == "settled" and r["side"] == "yes"
    assert r["market"] == "NY temp? · 74 or below"
    assert r["realized_usd"] == "+0.17" and r["result"] == "yes"


def test_title_falls_back_to_ticker():
    # no titles available -> lead column is still not blank
    assert kr._title("", "", "KXHIGHDEN-1") == "KXHIGHDEN-1"
    assert kr._title("Event", "", "T") == "Event"


def test_copy_trade_is_flagged():
    mp = {"ticker": "KXATPMATCH-26JUL07-DJO", "position": 1, "market_exposure": 40}
    copy_map = {"KXATPMATCH-26JUL07-DJO": "holymoses7"}
    r = kr.open_row(mp, {"last_price": 55}, {}, copy_map)
    assert r["copyTrade"] == "holymoses7"
    # a non-copy ticker stays blank
    assert kr.open_row({"ticker": "KXHIGHDEN-1", "position": 1,
                        "market_exposure": 60}, {"last_price": 60},
                       {}, copy_map)["copyTrade"] == ""


def test_build_rows_open_then_settled_dedup():
    positions = [{"ticker": "T1", "position": 1, "market_exposure": 50}]
    settlements = [{"ticker": "T1", "market_result": "yes", "yes_count": 1,
                    "revenue_dollars": 1.0, "yes_total_cost_dollars": 0.5},
                   {"ticker": "T2", "market_result": "no", "no_count": 2,
                    "revenue_dollars": 2.0, "no_total_cost_dollars": 1.0}]
    rows = kr.build_rows(positions, settlements, lambda t: {"last_price": 60},
                         {}, {})
    detail = [r for r in rows if r["status"] != "TOTAL"]
    # T1 is still open -> shown once as open, not duplicated as settled
    assert [r["ticker"] for r in detail] == ["T1", "T2"]
    assert detail[0]["status"] == "open" and detail[1]["status"] == "settled"


def test_finalize_totals_by_category_and_city():
    rows = [
        {"model": "weather", "city": "Denver", "total_usd": "+2.00"},
        {"model": "weather", "city": "Denver", "total_usd": "-0.50"},
        {"model": "sports", "city": "", "total_usd": "+1.00"},
    ]
    out = kr.finalize([dict(r) for r in rows])
    detail = [r for r in out if r.get("status") != "TOTAL"]
    # per-row denormalized totals
    assert detail[0]["categoryTotal_usd"] == "+1.50"   # weather 2.00-0.50
    assert detail[0]["cityTotal_usd"] == "+1.50"       # Denver 2.00-0.50
    assert detail[2]["categoryTotal_usd"] == "+1.00"   # sports
    assert detail[2]["cityTotal_usd"] == ""            # no city
    # summary rows: grand + per category + per city
    totals = {r["market"]: r["total_usd"] for r in out
              if r.get("status") == "TOTAL"}
    assert totals["═══ GRAND TOTAL (sum of positions) ═══"] == "+2.50"
    assert totals["TOTAL · category: weather"] == "+1.50"
    assert totals["TOTAL · category: sports"] == "+1.00"
    assert totals["TOTAL · weather city: Denver"] == "+1.50"
