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
