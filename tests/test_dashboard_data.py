"""Data-shaping tests for the dashboard. These need no web dependencies,
so they run with only the root requirements installed."""

from pathlib import Path

import auto_trade
import config
from dashboard import data


def test_normalize_pem_repairs_mangled_newlines():
    """Env-var editors often flatten a pasted .pem to one line; the
    dashboard must still load it. Valid PEM passes through unchanged."""
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7" * 8
    good = ("-----BEGIN PRIVATE KEY-----\n"
            + "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
            + "\n-----END PRIVATE KEY-----\n")
    flattened = good.replace("\n", " ")
    assert config._normalize_pem(flattened) == good
    assert config._normalize_pem(good) == good
    assert config._normalize_pem('"' + flattened + '"') == good  # quoted paste
    assert config._normalize_pem("not a pem") == "not a pem"


def test_theme_prefixes_stay_in_sync_with_auto_trade():
    """The dashboard's classifier is a superset of the risk cap's — every
    prefix auto_trade knows must classify identically here, so the page
    never labels a market differently than the bot's own theme cap."""
    for prefix, theme in auto_trade._THEME_PREFIXES:
        sample = prefix + "XX-26JUL06-B50.5"
        assert data.theme_of(sample) == theme == auto_trade.theme_of(sample)


def test_theme_of_macro_and_unknown():
    assert data.theme_of("KXPAYROLLS-26JUL-T0") == "macro"
    assert data.theme_of("KXMENWORLDCUP-26-FR") == "sports"
    assert data.theme_of("SOMETHINGELSE") == "other"
    assert data.theme_of("") == "other"


def _csv(tmp_path: Path, rows: str) -> Path:
    p = tmp_path / "hist.csv"
    p.write_text(
        "datetime_utc,date,ticker,action,side,count,price_cents,cost_usd,"
        "fee_usd,settlement,pnl_usd\n" + rows
    )
    return p


def test_load_history_parses_and_sorts(tmp_path):
    p = _csv(tmp_path, (
        "2026-07-02T10:00:00Z,2026-07-02,KXBTCD-X-T1,BUY,YES,2,40,0.80,0,no,-0.80\n"
        "2026-07-01T10:00:00Z,2026-07-01,KXHIGHNY-X-B9,BUY,NO,1,70,0.70,0,yes,0.30\n"
        "2026-07-03T10:00:00Z,2026-07-03,KXMLBGAME-X-Y,SELL,NO,1,50,0.50,0,,\n"
    ))
    trades = data.load_history(p)
    assert [t["ticker"][:6] for t in trades] == ["KXHIGH", "KXBTCD", "KXMLBG"]
    assert trades[0]["pnl_usd"] == 0.30
    assert trades[2]["pnl_usd"] is None and trades[2]["settlement"] == ""
    assert trades[0]["theme"] == "weather"


def test_load_history_missing_file_and_bad_row(tmp_path):
    assert data.load_history(tmp_path / "nope.csv") == []
    p = _csv(tmp_path, "garbage,x,K,BUY,YES,notanumber,1,1,0,,\n"
                       "2026-07-01T10:00:00Z,d,KXHIGHX-A-B,BUY,YES,1,50,0.50,0,,\n")
    trades = data.load_history(p)
    assert len(trades) == 1  # malformed row skipped, not fatal


def test_compute_stats_counts_pnl_once_per_market(tmp_path):
    """trade_history.csv repeats the market P&L on every fill row of the
    market — stats must count it once per ticker, matching HISTORY.md."""
    p = _csv(tmp_path, (
        "2026-07-01T10:00:00Z,d,KXHIGHNY-X-B9,BUY,NO,1,70,0.70,0,yes,0.30\n"
        # two fills, same market, same repeated market-level pnl:
        "2026-07-02T10:00:00Z,d,KXBTCD-X-T1,BUY,YES,2,40,0.80,0,no,-0.80\n"
        "2026-07-02T11:00:00Z,d,KXBTCD-X-T1,BUY,YES,1,45,0.45,0,no,-0.80\n"
        "2026-07-03T10:00:00Z,d,KXMLBGAME-X-Y,SELL,NO,1,50,0.50,0,,\n"
    ))
    s = data.compute_stats(data.load_history(p))
    assert s["trades"] == 4                    # fills
    assert s["deployed_usd"] == 2.45           # costs are per fill
    assert s["realized_pnl_usd"] == -0.50      # -0.80 counted once, not twice
    assert (s["wins"], s["losses"], s["pending"]) == (1, 1, 1)  # markets
    assert s["win_rate"] == 50.0
    assert s["pnl_series"][-1][1] == -0.50     # cumulative, per market
    assert len(s["pnl_series"]) == 2           # one point per settled market
    themes = {t["theme"]: t for t in s["themes"]}
    assert themes["weather"]["pnl_usd"] == 0.30
    assert themes["crypto"]["pnl_usd"] == -0.80
    assert themes["crypto"]["losses"] == 1
    assert themes["crypto"]["trades"] == 2
    assert themes["sports"]["pending"] == 1


def test_stats_agree_with_history_md_on_real_data():
    """Guard against re-introducing per-fill double counting: on the real
    CSV the realized total must equal the per-market sum, which HISTORY.md
    prints as the headline P&L."""
    trades = data.load_history()
    if not trades:  # fresh clone with an empty ledger
        return
    per_market = {}
    for t in trades:
        if t["pnl_usd"] is not None:
            per_market[t["ticker"]] = t["pnl_usd"]
    s = data.compute_stats(trades)
    assert s["realized_pnl_usd"] == round(sum(per_market.values()), 2)


def test_compute_stats_empty():
    s = data.compute_stats([])
    assert s["trades"] == 0 and s["win_rate"] is None and s["pnl_series"] == []


def test_fill_to_trade_maps_live_fill():
    fill = {"trade_id": "abc", "ticker": "KXHIGHCHI-26JUL06-B78.5",
            "side": "no", "action": "buy", "count": 3,
            "no_price": 65, "created_time": "2026-07-06T15:00:00Z"}
    t = data.fill_to_trade(fill)
    assert t["theme"] == "weather" and t["action"] == "BUY"
    assert t["price_cents"] == 65.0 and t["cost_usd"] == 1.95
    assert t["settlement"] == "" and t["pnl_usd"] is None
