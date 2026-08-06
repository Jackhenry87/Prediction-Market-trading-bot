"""Live executor: maker discipline and the daily circuit breakers.

These are the tests that stand between a sized model and a bad day. The
per-order and total-exposure caps bound instantaneous risk; nothing bounded
how much could be committed in a single day until MAX_ORDERS_PER_DAY and
MAX_DAILY_DEPLOY_USD existed.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import favorite_runner as fr


@dataclass
class FakeSettings:
    kill_switch: bool = False
    dry_run: bool = False
    max_order_size: float = 5.0
    max_total_exposure: float = 45.0


def _book(yes_bid=88, yes_ask=92):
    return {"ticker": "KXA-26AUG06-1", "yes_bid": yes_bid, "yes_ask": yes_ask,
            "volume": 500, "status": "active"}


def _signal(ticker="KXA-26AUG06-1", price=89, contracts=2, edge=2.0):
    return dict(ticker=ticker, side="yes", price_cents=price,
                contracts=contracts, ev_cents=edge, sizing_reason="test")


class _Client:
    def __init__(self, market=None, fail=False):
        self.market = market or _book()
        self.fail = fail
        self.orders = []

    def get_market(self, ticker):
        if self.fail:
            raise RuntimeError("boom")
        return dict(self.market, ticker=ticker)

    def create_limit_order(self, ticker, side, action, count, price):
        self.orders.append(dict(ticker=ticker, side=side, action=action,
                                count=count, price=price))
        return {"order": {"order_id": f"ord-{len(self.orders)}"}}


# --- maker discipline -------------------------------------------------------

def test_price_is_kept_strictly_below_the_live_ask():
    # signal said 89 but the ask has fallen to 89 — resting there would CROSS
    # and pay the taker fee, destroying the edge the order exists to capture.
    assert fr.maker_price_now(_book(85, 89), "yes", 89) == 88


def test_order_is_skipped_when_the_book_moves_past_us():
    # ask collapsed to 86: bid+1 would be below the new bid, no maker price
    assert fr.maker_price_now(_book(70, 74), "yes", 89) is None


def test_order_is_skipped_when_the_favourite_flips_sides():
    # our signal was YES but the market is now a NO favourite
    assert fr.maker_price_now(_book(8, 12), "yes", 89) is None


def test_unchanged_book_keeps_the_signal_price():
    assert fr.maker_price_now(_book(88, 92), "yes", 89) == 89


# --- daily circuit breakers -------------------------------------------------

def _ledger(tmp_path, rows):
    p = tmp_path / "executed_trades.csv"
    lines = ["placed_at_utc,model,ticker,side,count,price_cents,cost_usd,"
             "order_id,outcome"]
    lines += rows
    p.write_text("\n".join(lines) + "\n")
    return p


NOW = datetime(2026, 8, 6, 18, 0, tzinfo=timezone.utc)


def test_placed_today_counts_only_today_and_only_this_model(tmp_path):
    p = _ledger(tmp_path, [
        "2026-08-06T01:00:00+00:00,favorite,KXA,yes,2,89,1.78,o1,",
        "2026-08-06T02:00:00+00:00,favorite,KXB,yes,1,90,0.90,o2,",
        "2026-08-05T23:00:00+00:00,favorite,KXC,yes,2,89,1.78,o3,",  # yesterday
        "2026-08-06T03:00:00+00:00,sports,KXD,yes,1,50,0.50,o4,",     # not ours
    ])
    count, usd, themes = fr.placed_today(p, NOW)
    assert count == 2 and round(usd, 2) == 2.68


def test_missing_ledger_is_a_clean_zero(tmp_path):
    count, usd, themes = fr.placed_today(tmp_path / "nope.csv", NOW)
    assert (count, usd, themes) == (0, 0.0, {})


def test_daily_order_cap_stops_placing(monkeypatch, tmp_path):
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_DAY", 2)
    monkeypatch.setattr(fr, "MAX_DAILY_DEPLOY_USD", 100.0)
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    monkeypatch.setattr(fr, "log_execution", lambda *a, **k: None)
    client = _Client()
    sigs = [_signal(ticker=f"KX{i}-26AUG06-1") for i in range(6)]   # 6 events
    placed = fr.place_signals(client, FakeSettings(), sigs, 50.0, 0.0, set())
    assert placed == 2 and len(client.orders) == 2


def test_daily_deploy_cap_stops_placing(monkeypatch):
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_DAY", 99)
    monkeypatch.setattr(fr, "MAX_DAILY_DEPLOY_USD", 4.0)
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    monkeypatch.setattr(fr, "log_execution", lambda *a, **k: None)
    client = _Client()
    # each order is 2 x 89c = $1.78, so only two fit under a $4 cap
    sigs = [_signal(ticker=f"KX{i}-26AUG06-1") for i in range(6)]   # 6 events
    fr.place_signals(client, FakeSettings(), sigs, 50.0, 0.0, set())
    assert sum(o["count"] * o["price"] / 100 for o in client.orders) <= 4.0


def test_caps_carry_across_runs(monkeypatch):
    # the scheduler starts a fresh process every 20 minutes, so the cap has to
    # come off the ledger, not an in-process counter
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_DAY", 3)
    monkeypatch.setattr(fr, "MAX_DAILY_DEPLOY_USD", 100.0)
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (3, 5.0, {}))
    monkeypatch.setattr(fr, "log_execution", lambda *a, **k: None)
    client = _Client()
    placed = fr.place_signals(client, FakeSettings(), [_signal()], 50.0, 0.0,
                              set())
    assert placed == 0 and client.orders == []


# --- other refusals ---------------------------------------------------------

def test_held_ticker_is_not_stacked(monkeypatch):
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    client = _Client()
    fr.place_signals(client, FakeSettings(), [_signal()], 50.0, 0.0,
                     {"KXA-26AUG06-1"})
    assert client.orders == []


def test_one_order_per_event_per_run(monkeypatch):
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    monkeypatch.setattr(fr, "log_execution", lambda *a, **k: None)
    client = _Client()
    sigs = [_signal(ticker="KXA-26AUG06-1"), _signal(ticker="KXA-26AUG06-2")]
    placed = fr.place_signals(client, FakeSettings(), sigs, 50.0, 0.0, set())
    assert placed == 1


def test_kill_switch_blocks_every_order(monkeypatch):
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    client = _Client()
    placed = fr.place_signals(client, FakeSettings(kill_switch=True),
                              [_signal()], 50.0, 0.0, set())
    assert placed == 0 and client.orders == []


def test_dry_run_sends_nothing(monkeypatch):
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    client = _Client()
    placed = fr.place_signals(client, FakeSettings(dry_run=True), [_signal()],
                              50.0, 0.0, set())
    assert placed == 0 and client.orders == []


def test_unfetchable_market_is_skipped_not_placed_blind(monkeypatch):
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    client = _Client(fail=True)
    placed = fr.place_signals(client, FakeSettings(), [_signal()], 50.0, 0.0,
                              set())
    assert placed == 0 and client.orders == []


# --- correlated-theme cap across the day ------------------------------------
# strategy_favorite caps themes within a scan, but the scheduler runs ~70 scans
# a day. Two inflation bets per scan would still fill the entire 8-order daily
# budget with a single bet on a single CPI print, which is the concentration
# the per-scan cap was meant to prevent.

def test_placed_today_groups_tickers_into_themes(tmp_path):
    p = _ledger(tmp_path, [
        "2026-08-06T01:00:00+00:00,favorite,KXUSEDCARCPI-26AUG12-T1,yes,1,86,0.86,o1,",
        "2026-08-06T02:00:00+00:00,favorite,KXSHELTERCPI-26AUG12-T4,yes,1,88,0.88,o2,",
        "2026-08-06T03:00:00+00:00,favorite,KXALLSVENSKANGAME-X-BRO,no,1,91,0.91,o3,",
    ])
    count, usd, themes = fr.placed_today(p, NOW)
    assert count == 3
    assert themes["inflation"] == 2          # the two CPI markets are one bet
    assert themes["KXALLSVENSKANGAME"] == 1


def test_theme_cap_stops_a_day_of_one_correlated_bet(monkeypatch):
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_DAY", 8)
    monkeypatch.setattr(fr, "MAX_DAILY_DEPLOY_USD", 100.0)
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_THEME_PER_DAY", 3)
    monkeypatch.setattr(fr, "placed_today", lambda *a, **k: (0, 0.0, {}))
    monkeypatch.setattr(fr, "log_execution", lambda *a, **k: None)
    client = _Client()
    # eight distinct CPI markets, all resolving off one release
    sigs = [_signal(ticker=f"KXCPI{i}-26AUG12-T1") for i in range(8)]
    placed = fr.place_signals(client, FakeSettings(), sigs, 50.0, 0.0, set())
    assert placed == 3, "one theme must not consume the whole daily budget"


def test_theme_cap_carries_across_runs(monkeypatch):
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_THEME_PER_DAY", 3)
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_DAY", 8)
    monkeypatch.setattr(fr, "MAX_DAILY_DEPLOY_USD", 100.0)
    # earlier runs today already placed three inflation bets
    monkeypatch.setattr(fr, "placed_today",
                        lambda *a, **k: (3, 5.0, {"inflation": 3}))
    monkeypatch.setattr(fr, "log_execution", lambda *a, **k: None)
    client = _Client()
    placed = fr.place_signals(client, FakeSettings(),
                              [_signal(ticker="KXCPIX-26AUG12-T1")],
                              50.0, 0.0, set())
    assert placed == 0 and client.orders == []


def test_other_themes_still_trade_when_one_is_capped(monkeypatch):
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_THEME_PER_DAY", 3)
    monkeypatch.setattr(fr, "MAX_ORDERS_PER_DAY", 8)
    monkeypatch.setattr(fr, "MAX_DAILY_DEPLOY_USD", 100.0)
    monkeypatch.setattr(fr, "placed_today",
                        lambda *a, **k: (3, 5.0, {"inflation": 3}))
    monkeypatch.setattr(fr, "log_execution", lambda *a, **k: None)
    client = _Client()
    sigs = [_signal(ticker="KXCPIX-26AUG12-T1"),
            _signal(ticker="KXSOCCER-26AUG10-BRO")]
    placed = fr.place_signals(client, FakeSettings(), sigs, 50.0, 0.0, set())
    assert placed == 1
    assert client.orders[0]["ticker"] == "KXSOCCER-26AUG10-BRO"


# --- resting-order lifecycle ------------------------------------------------
# A maker bid with no expiry is a free option written to the rest of the
# market. Markets here close up to a week out, so an 86c bid could sit while
# the price walked to 60c — and the only traders who take it are the ones who
# know why it should not be there. The runner had NO cancellation at all.

from datetime import timedelta

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)


def _resting(price=86, side="yes", age_h=1.0, ticker="KXA-26AUG06-1",
             oid="ord-1"):
    return {"order_id": oid, "ticker": ticker, "side": side, "action": "buy",
            "yes_price": price, "remaining_count": 2,
            "created_time": (T0 - timedelta(hours=age_h)).isoformat()}


def test_bid_left_above_the_mid_is_cancelled():
    # we bid 86; the market fell to 70/80 (mid 75). Our order now offers to
    # overpay by 11c and will be hit by whoever wants out.
    r = fr.cancel_reason(_resting(price=86), _book(70, 80), T0)
    assert r and "above the mid" in r


def test_bid_still_below_the_mid_is_left_alone():
    r = fr.cancel_reason(_resting(price=86), _book(85, 95), T0)   # mid 90
    assert r is None


def test_favourite_flip_cancels():
    # our YES bid when the market now favours NO: we hold the longshot
    r = fr.cancel_reason(_resting(price=86, side="yes"), _book(8, 12), T0)
    assert r and "flipped" in r


def test_ttl_expires_an_otherwise_fine_bid(monkeypatch):
    monkeypatch.setattr(fr, "RESTING_TTL_H", 6)
    fine = fr.cancel_reason(_resting(price=86, age_h=1), _book(85, 95), T0)
    stale = fr.cancel_reason(_resting(price=86, age_h=7), _book(85, 95), T0)
    assert fine is None
    assert stale and "TTL" in stale


def test_unreadable_order_still_expires_on_age(monkeypatch):
    monkeypatch.setattr(fr, "RESTING_TTL_H", 6)
    o = {"order_id": "x", "ticker": "T", "action": "buy", "remaining_count": 1,
         "created_time": (T0 - timedelta(hours=9)).isoformat()}
    assert "unreadable" in fr.cancel_reason(o, _book(85, 95), T0)


def test_no_quote_means_leave_it_rather_than_cancel_blind():
    assert fr.cancel_reason(_resting(), {"ticker": "T"}, T0) is None


class _CancelClient(_Client):
    def __init__(self, market=None):
        super().__init__(market)
        self.cancelled = []

    def cancel_order(self, oid):
        self.cancelled.append(oid)
        return {"ok": True}


def test_a_bid_that_drifts_out_of_the_band_is_cancelled():
    # book fell to 70/80: our 70c bid is below the mid, but we only want to
    # hold favourites in the 85-97 band, so we should not want this fill.
    r = fr.cancel_reason(_resting(price=70), _book(70, 80), T0)
    assert r and "outside the trading band" in r


def test_sweep_cancels_only_the_stale_ones():
    client = _CancelClient(_book(85, 95))          # mid 90
    orders = [_resting(price=92, oid="stale"),     # above mid -> cancel
              _resting(price=86, oid="ok")]        # below mid, in band -> keep
    n = fr.cancel_stale_resting(client, orders, T0)
    assert n == 1 and client.cancelled == ["stale"]


def test_sweep_ignores_sell_orders():
    client = _CancelClient(_book(70, 80))
    o = dict(_resting(price=86), action="sell")
    assert fr.cancel_stale_resting(client, [o], T0) == 0
    assert client.cancelled == []


def test_cancel_failure_does_not_stop_the_sweep():
    class _Flaky(_CancelClient):
        def cancel_order(self, oid):
            if oid == "bad":
                raise RuntimeError("500")
            return super().cancel_order(oid)

    client = _Flaky(_book(70, 80))
    orders = [_resting(price=86, oid="bad"), _resting(price=86, oid="good")]
    n = fr.cancel_stale_resting(client, orders, T0)
    assert n == 1 and client.cancelled == ["good"]
