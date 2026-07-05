"""Tests for Kalshi history backfill. Run: pytest tests/"""

import backfill_history as bh


def test_money_prefers_dollars():
    assert bh._money({"revenue_dollars": "3.50", "revenue": 999}, "revenue") == 3.5
    assert bh._money({"yes_price": 42}, "yes_price") == 0.42   # cents/100
    assert bh._money({}, "revenue") is None


def test_fill_price_uses_traded_side():
    assert bh._fill_price_usd({"side": "yes", "yes_price_dollars": "0.65"}) == 0.65
    assert bh._fill_price_usd({"side": "no", "no_price_dollars": "0.30"}) == 0.30


def test_settlement_pnl():
    # bought for $2.00 total, market paid $5.00, $0.05 fee -> +2.95
    s = {"market_result": "yes", "revenue_dollars": "5.00",
         "yes_total_cost_dollars": "2.00", "no_total_cost_dollars": "0",
         "fee_cost_dollars": "0.05"}
    result, pnl = bh.settlement_pnl(s)
    assert result == "yes" and abs(pnl - 2.95) < 1e-9


def test_build_writes_history(tmp_path, monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k): pass
        def get_fills(self, min_ts=None):
            return [{"created_time": "2026-07-02T17:01:03+00:00",
                     "ticker": "KXHIGHNY-26JUL02-B99.5", "action": "buy",
                     "side": "no", "count": 4, "no_price_dollars": "0.56",
                     "fee_cost_dollars": "0.02"}]
        def get_settlements(self, min_ts=None):
            return [{"ticker": "KXHIGHNY-26JUL02-B99.5", "market_result": "no",
                     "revenue_dollars": "4.00", "no_total_cost_dollars": "2.24",
                     "yes_total_cost_dollars": "0", "fee_cost_dollars": "0.02"}]

    monkeypatch.setattr(bh, "KalshiClient", FakeClient)
    monkeypatch.setattr(bh, "CSV_OUT", tmp_path / "trade_history.csv")
    monkeypatch.setattr(bh, "MD_OUT", tmp_path / "HISTORY.md")

    class S:
        kalshi_api_key_id = "k"; kalshi_private_key_path = "p"; kalshi_env = "prod"
    monkeypatch.setattr(bh, "load_kalshi_settings", lambda **k: S())

    assert bh.build("2026-07-01") == 0
    csv_text = (tmp_path / "trade_history.csv").read_text()
    assert "KXHIGHNY-26JUL02-B99.5" in csv_text and "2026-07-02" in csv_text
    md = (tmp_path / "HISTORY.md").read_text()
    assert "## 2026-07-02" in md and "Realized P&L" in md
