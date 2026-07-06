"""Tests for the shared paper-trade ledger. Run: pytest tests/"""

from pathlib import Path

import ledger


def _result(ticker, price, side="yes"):
    return {"date": "EVT", "signals": [
        {"ticker": ticker, "side": side, "price_cents": price,
         "model_prob": 0.7, "ev_cents": 8.0, "subtitle": ""}]}


def test_apply_price_band():
    res = [_result("A", 55), _result("B", 70), _result("C", 95)]
    kept = ledger.apply_price_band(res, 60, 90)
    tickers = [r["signals"][0]["ticker"] for r in kept]
    assert tickers == ["B"]          # 55 and 95 excluded, 70 kept


def test_log_signals_dedupes_by_ticker(tmp_path):
    path = tmp_path / "p.csv"
    assert ledger.log_signals([_result("A", 70), _result("B", 80)], path) == 2
    # re-logging the same tickers writes nothing (no hourly inflation)
    assert ledger.log_signals([_result("A", 70), _result("B", 80)], path) == 0
    # a genuinely new market is added
    assert ledger.log_signals([_result("C", 65)], path) == 1

    rows = path.read_text().strip().splitlines()
    assert rows[0].startswith("scanned_at_utc")
    assert len(rows) == 4            # header + A + B + C


def test_ledger_columns_match_scoreboard_reader(tmp_path):
    import strategy_weather as sw
    path = tmp_path / "p.csv"
    ledger.log_signals([_result("A", 70)], path)
    # score_pending_paper_trades must be able to read the ledger schema
    header = path.read_text().splitlines()[0].split(",")
    for col in ("scanned_at_utc", "ticker", "side", "price_cents", "outcome"):
        assert col in header


def test_log_execution_audit_trail(tmp_path):
    import ledger
    path = tmp_path / "exec.csv"
    ledger.log_execution("weather", "KXHIGHNY-X", "no", 3, 72,
                         "ord-1", path=path)
    ledger.log_execution("sports", "KXMLBGAME-Y", "yes", 2, 65,
                         "ord-2", path=path)
    rows = list(__import__("csv").reader(open(path)))
    assert rows[0] == ledger.EXEC_COLUMNS
    assert rows[1][1] == "weather" and rows[1][6] == "2.16"   # 72*3/100
    assert rows[2][1] == "sports" and rows[2][6] == "1.30"    # 65*2/100


def test_upgrade_exec_columns_migrates_legacy_file(tmp_path):
    path = tmp_path / "exec.csv"
    path.write_text("placed_at_utc,model,ticker,side,count,price_cents,"
                    "cost_usd,order_id\n"
                    "2026-07-05T14:00:00+00:00,weather,KXHIGHNY-X,no,1,72,"
                    "0.72,abc\n")
    ledger.upgrade_exec_columns(path)
    rows = path.read_text().strip().splitlines()
    assert rows[0].endswith(",outcome")
    assert rows[1].endswith(",")            # empty outcome added
    before = path.read_text()
    ledger.upgrade_exec_columns(path)       # idempotent
    assert path.read_text() == before


class _FakeClient:
    def __init__(self, fills):
        self._fills = fills

    def get_fills(self, min_ts=None):
        return self._fills


def test_reconcile_fills_recovers_missing_orders(tmp_path):
    path = tmp_path / "exec.csv"
    ledger.log_execution("weather", "KXHIGHLAX-B76.5", "no", 1, 78,
                         "known-1", path)
    fills = [
        # already in the ledger -> skipped
        dict(order_id="known-1", action="buy", side="no", count=1,
             no_price=78, ticker="KXHIGHLAX-B76.5",
             created_time="2026-07-06T05:32:00Z"),
        # yesterday's order the CSV lost: two partial fills, one order
        dict(order_id="lost-2", action="buy", side="yes", count=2,
             yes_price=60, ticker="KXMLBGAME-26JUL05PHIKC-PHI",
             created_time="2026-07-05T16:00:00Z"),
        dict(order_id="lost-2", action="buy", side="yes", count=1,
             yes_price=63, ticker="KXMLBGAME-26JUL05PHIKC-PHI",
             created_time="2026-07-05T16:00:05Z"),
        # a sell is an exit, not a bet -> ignored
        dict(order_id="sell-3", action="sell", side="yes", count=5,
             yes_price=90, ticker="KXWHATEVER",
             created_time="2026-07-05T17:00:00Z"),
    ]
    assert ledger.reconcile_fills(_FakeClient(fills), path=path) == 1
    rows = path.read_text().strip().splitlines()
    assert len(rows) == 3                   # header + known + recovered
    rec = rows[-1].split(",")
    idx = {h: i for i, h in enumerate(ledger.EXEC_COLUMNS)}
    assert rec[idx["model"]] == "untracked"
    assert rec[idx["order_id"]] == "lost-2"
    assert rec[idx["count"]] == "3"
    assert rec[idx["price_cents"]] == "61"  # (2*60+1*63)/3 = 61
    assert rec[idx["cost_usd"]] == "1.83"
    # re-running adds nothing (dedup by order_id)
    assert ledger.reconcile_fills(_FakeClient(fills), path=path) == 0


def test_no_price_derived_from_yes_price():
    assert ledger._fill_price_cents(dict(side="no", yes_price=78)) == 22.0
    assert ledger._fill_price_cents(dict(side="no", no_price=30)) == 30.0
    assert ledger._fill_price_cents(dict(side="yes", yes_price=61)) == 61.0


def test_scoreboard_shows_executed_outcomes(tmp_path, monkeypatch):
    import scoreboard
    path = tmp_path / "executed_trades.csv"
    path.write_text(
        ",".join(ledger.EXEC_COLUMNS) + "\n"
        "2026-07-05T16:00:00+00:00,weather,KXHIGHNY-B90,no,3,72,2.16,a1,"
        "win (+28c)\n"
        "2026-07-06T05:32:00+00:00,untracked,KXMLB-X,yes,2,60,1.20,b2,"
        "loss (-60c)\n"
        "2026-07-06T06:00:00+00:00,sports,KXMLB-Y,yes,1,65,0.65,c3,\n")
    monkeypatch.setattr(scoreboard, "ROOT", tmp_path)
    monkeypatch.setattr(scoreboard, "SOURCES", [])
    out = tmp_path / "SCOREBOARD.md"
    scoreboard.build(out)
    text = out.read_text()
    # header rollup: 3 orders, 1W-1L, realized 3*28c - 2*60c = -$0.36
    assert "3 orders" in text
    assert "1 W — 1 L" in text and "-$0.36" in text
    # per-row results incl. count-scaled dollars and the open row
    assert "🟢 win (+$0.84)" in text
    assert "🔴 loss (-$1.20)" in text
    assert "⏳ open" in text
