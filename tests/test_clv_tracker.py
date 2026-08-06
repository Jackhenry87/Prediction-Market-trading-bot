"""CLV tracker: it must snapshot the OPEN price, freeze the last open price as
the closing line when the market settles, and compute CLV = closing - entry.
Getting this wrong (e.g. using the 0/100 settled price as 'closing') would make
the edge signal meaningless, so it's tested tightly."""

import clv_tracker as clv


class _Client:
    """Returns a scripted sequence of market states per get_market call."""
    def __init__(self, states):
        self.states = list(states)
        self.i = -1

    def get_market(self, ticker):
        self.i = min(self.i + 1, len(self.states) - 1)
        return self.states[self.i]


def _fill(oid="o1", side="yes", price="40"):
    return dict(order_id=oid, ticker="KXMLBGAME-X", model="sports",
                side=side, price_cents=price, placed_at_utc="t0")


def test_side_value_yes_and_no():
    m = {"yes_bid": 38, "yes_ask": 42}          # yes mid = 40
    assert clv.side_value_cents(m, "yes") == 40
    assert clv.side_value_cents(m, "no") == 60


def test_side_value_reads_dollars_vintage():
    # Kalshi's newer fields — the raw-key reader returned None here and silently
    # killed CLV (no snapshot -> no closing line). price_cents must handle it.
    m = {"yes_bid_dollars": "0.38", "yes_ask_dollars": "0.42"}   # yes mid = 40
    assert round(clv.side_value_cents(m, "yes"), 1) == 40.0
    assert round(clv.side_value_cents(m, "no"), 1) == 60.0
    # falls back to last_price_dollars when no book quote
    assert round(clv.side_value_cents({"last_price_dollars": "0.55"}, "yes"), 1) == 55.0


def test_snapshot_then_freeze_gives_clv():
    # entry 40c on YES. While open the yes mid rises to 55 (market moved our
    # way). Then it settles YES. Closing line must be the last OPEN price (55),
    # NOT the 100 settled price -> CLV = 55 - 40 = +15c.
    client = _Client([
        {"status": "active", "yes_bid": 53, "yes_ask": 57},   # open snapshot -> 55
        {"status": "settled", "result": "yes", "last_price": 100},
    ])
    rows = clv.update({}, [_fill(price="40")], client)   # adds + first snapshot
    assert rows["o1"]["last_price"] == "55.0" and rows["o1"]["status"] == "open"
    rows = clv.update(rows, [_fill(price="40")], client)  # sees settled -> freeze
    r = rows["o1"]
    assert r["status"] == "closed"
    assert r["closing_price"] == "55.0"
    assert r["clv_cents"] == "15.0"        # positive CLV = beat the close
    assert r["result"] == "yes"


def test_no_side_clv():
    # entry 45c on NO; market drifts so yes mid = 40 -> no value 60 while open,
    # then settles. CLV(no) = 60 - 45 = +15.
    client = _Client([
        {"status": "active", "yes_bid": 38, "yes_ask": 42},   # no value = 60
        {"status": "finalized", "result": "no"},
    ])
    rows = clv.update({}, [_fill(side="no", price="45")], client)
    rows = clv.update(rows, [_fill(side="no", price="45")], client)
    assert rows["o1"]["clv_cents"] == "15.0"


def _scored(n, clv_cents):
    return {f"o{i}": {"status": "closed", "clv_cents": clv_cents,
                      "fill_status": "filled"} for i in range(n)}


def test_scoreboard_verdict_thresholds():
    # under the sample target -> "not enough"; past it, the sign of the mean
    # decides. The target is 100, not 30: the owner's rule is no real money
    # until 100+ scored samples say the edge is real.
    assert clv.SAMPLE_TARGET == 100
    assert "not enough" in clv.scoreboard(_scored(3, "5")).lower()
    assert "genuine edge" in clv.scoreboard(_scored(clv.SAMPLE_TARGET, "4"))
    assert "no edge" in clv.scoreboard(_scored(clv.SAMPLE_TARGET, "-4"))


def test_only_sports_fills_tracked(tmp_path, monkeypatch):
    # a weather fill in the same ledger must be ignored
    csv_text = ("placed_at_utc,model,ticker,side,count,price_cents,cost_usd,order_id,outcome\n"
                "t,weather,KXHIGHCHI-X,no,1,64,0.64,w1,\n"
                "t,sports,KXMLBGAME-Y,yes,1,40,0.40,s1,\n")
    p = tmp_path / "executed_trades.csv"
    p.write_text(csv_text)
    monkeypatch.setattr(clv, "EXECUTED", p)
    fills = clv.load_sports_fills()
    assert [f["order_id"] for f in fills] == ["s1"]


# --- the failures that produced zero usable samples for a month -------------

def _paper(oid="p1", side="yes", price="89"):
    return dict(order_id=oid, ticker="KXTEST-A", model="favorite", side=side,
                price_cents=price, placed_at_utc="t0", fill_status="resting")


def test_resting_maker_order_is_not_a_fill_until_the_ask_reaches_it():
    # Our resting bid is 89. While the ask sits at 92 nobody has sold to us,
    # so the row must stay `resting` — counting it as a bet would be the
    # backtest lie this model exists to avoid.
    client = _Client([{"status": "active", "yes_bid": 88, "yes_ask": 92}])
    rows = clv.update({}, [_paper()], client, now="t1")
    assert rows["p1"]["fill_status"] == "resting"
    assert rows["p1"]["last_price"] == "90.0"      # still snapshots the price


def test_ask_dropping_to_our_price_fills_the_maker_order():
    client = _Client([{"status": "active", "yes_bid": 86, "yes_ask": 89}])
    rows = clv.update({}, [_paper()], client, now="t1")
    assert rows["p1"]["fill_status"] == "filled" and rows["p1"]["fill_ts"] == "t1"


def test_no_side_fill_uses_the_mirrored_ask():
    # NO ask = 100 - yes_bid. yes_bid 11 -> no ask 89 -> our 89 bid is hit.
    client = _Client([{"status": "active", "yes_bid": 11, "yes_ask": 15}])
    rows = clv.update({}, [_paper(side="no")], client, now="t1")
    assert rows["p1"]["fill_status"] == "filled"


def test_unfilled_at_close_is_not_scored():
    client = _Client([
        {"status": "active", "yes_bid": 88, "yes_ask": 92},   # never hit
        {"status": "settled", "result": "yes"},
    ])
    rows = clv.update({}, [_paper()], client)
    rows = clv.update(rows, [_paper()], client)
    assert rows["p1"]["fill_status"] == "unfilled"
    assert rows["p1"]["clv_cents"] == ""
    assert "**Scored samples:** 0" in clv.scoreboard(rows)
    assert "expired unfilled (never hit — correctly NOT scored): 1" in \
        clv.scoreboard(rows)


def test_closed_before_any_snapshot_is_reported_not_dropped():
    # THE original bug: the tracker first saw these markets after they closed,
    # wrote status=closed with a blank CLV, and the scoreboard filtered them
    # out — four bets became "0 settled bets" with nothing saying why.
    client = _Client([{"status": "settled", "result": "yes"}])
    rows = clv.update({}, [_fill(price="40")], client)
    assert rows["o1"]["status"] == "unscored"
    board = clv.scoreboard(rows)
    assert "unscored (closed before any open snapshot — capture gap): 1" in board
    assert "measurement failure" in board


def test_filled_paper_row_scores_like_a_real_bet():
    client = _Client([
        {"status": "active", "yes_bid": 86, "yes_ask": 89},   # fills at 89
        {"status": "active", "yes_bid": 93, "yes_ask": 95},   # drifts to 94
        {"status": "settled", "result": "yes"},
    ])
    rows = clv.update({}, [_paper()], client)
    rows = clv.update(rows, [_paper()], client)
    rows = clv.update(rows, [_paper()], client)
    r = rows["p1"]
    assert r["fill_status"] == "filled" and r["status"] == "closed"
    assert r["closing_price"] == "94.0" and r["clv_cents"] == "5.0"
    assert "**Scored samples:** 1" in clv.scoreboard(rows)


def test_paper_signals_load_from_the_ledger(tmp_path, monkeypatch):
    p = tmp_path / "paper_trades_favorite.csv"
    p.write_text("scanned_at_utc,event,ticker,side,price_cents,model_prob,"
                 "ev_cents,outcome,clv_cents\n"
                 "2026-08-06T12:00:00+00:00,2026-08-06,KXA-1,yes,89,0.90,4.1,,\n")
    monkeypatch.setattr(clv, "PAPER_LEDGER", p)
    sigs = clv.load_paper_signals()
    assert len(sigs) == 1
    s = sigs[0]
    assert s["fill_status"] == "resting" and s["price_cents"] == "89"
    # keyed by ticker+timestamp so re-scanning the same market never enrols twice
    assert s["order_id"] == "paper:KXA-1:2026-08-06T12:00:00+00:00"
    rows = clv._enroll({}, sigs)
    assert clv._enroll(rows, clv.load_paper_signals()).keys() == rows.keys()


# --- settlement record without placing an order -----------------------------

def test_settlement_pnl_arithmetic():
    # bought YES at 89: wins 11c if it settles yes, loses 89c if no
    assert clv.settlement_pnl_cents("yes", 89.0, "yes") == 11.0
    assert clv.settlement_pnl_cents("yes", 89.0, "no") == -89.0
    assert clv.settlement_pnl_cents("no", 89.0, "no") == 11.0
    # unsettled or unknown -> no P&L, never a guess
    assert clv.settlement_pnl_cents("yes", 89.0, "") is None
    assert clv.settlement_pnl_cents("yes", None, "yes") is None


def test_paper_signal_gets_a_real_win_loss_record():
    # The whole point: a signal never sent to Kalshi still gets scored
    # against Kalshi's official result.
    client = _Client([
        {"status": "active", "yes_bid": 86, "yes_ask": 89},   # fills at 89
        {"status": "settled", "result": "yes"},
    ])
    rows = clv.update({}, [_paper()], client)
    rows = clv.update(rows, [_paper()], client)
    assert rows["p1"]["result"] == "yes"
    assert rows["p1"]["pnl_cents"] == "+11"
    board = clv.scoreboard(rows)
    assert "**1W – 0L** over 1 settled contracts" in board
    assert "**Net +11c** on 89c staked" in board
    assert "+12.4%" in board                      # 11/89


def test_losing_paper_signal_is_recorded_as_a_loss():
    client = _Client([
        {"status": "active", "yes_bid": 86, "yes_ask": 89},
        {"status": "settled", "result": "no"},
    ])
    rows = clv.update({}, [_paper()], client)
    rows = clv.update(rows, [_paper()], client)
    assert rows["p1"]["pnl_cents"] == "-89"
    assert "**0W – 1L**" in clv.scoreboard(rows)


def test_unfilled_signal_never_counts_as_a_win():
    # The failure mode that makes a paper record worthless: claiming a win on
    # an order the book never reached.
    client = _Client([
        {"status": "active", "yes_bid": 80, "yes_ask": 95},   # never hit 89
        {"status": "settled", "result": "yes"},
    ])
    rows = clv.update({}, [_paper()], client)
    rows = clv.update(rows, [_paper()], client)
    assert rows["p1"]["fill_status"] == "unfilled"
    assert rows["p1"]["pnl_cents"] == ""
    assert "**0W – 0L** over 0 settled contracts" in clv.scoreboard(rows)
