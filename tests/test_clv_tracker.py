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


def test_scoreboard_verdict_thresholds():
    # <30 settled -> "need more"; else sign of mean decides edge/no-edge
    small = {f"o{i}": {"status": "closed", "clv_cents": "5"} for i in range(3)}
    assert "need" in clv.scoreboard(small).lower()
    winners = {f"o{i}": {"status": "closed", "clv_cents": "4"} for i in range(30)}
    assert "genuine edge" in clv.scoreboard(winners)
    losers = {f"o{i}": {"status": "closed", "clv_cents": "-4"} for i in range(30)}
    assert "no edge" in clv.scoreboard(losers)


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
