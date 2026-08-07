"""The trade feed.

Two things here are safety properties rather than features:

  * the COLD-START guard. He has 123 trades on record. If the first poll
    returned them as "new", arming the copier would fire a hundred-plus
    orders into markets that mostly settled weeks ago.
  * FeedError on a dead feed. These are undocumented endpoints; if one closes,
    an empty list would look exactly like "he has not traded today", and the
    copier would sit there looking healthy while permanently blind.
"""

import json

import pytest

import follow_feed as ff


class FakeClient:
    """Stands in for KalshiClient. Records the paths it was asked for."""

    root = "https://api.example.com"
    private_key = object()

    def __init__(self, payload=None, status=200):
        self.payload, self.status, self.paths = payload, status, []

    def _headers(self, method, path):
        self.paths.append(path)
        return {"KALSHI-ACCESS-KEY": "k"}


def _resp(payload, status=200):
    class R:
        status_code = status
        text = json.dumps(payload)

        def json(self):
            return payload
    return R()


def _wire(monkeypatch, payload, status=200):
    monkeypatch.setattr(ff.requests, "get",
                        lambda *a, **k: _resp(payload, status))


def _trade(i, ticker="KXMLBGAME-26AUG06LADCHC-CHC", price=42):
    return {"trade_id": f"t{i}", "ticker": ticker, "side": "yes",
            "price": price, "count": 5000, "created_time": f"2026-08-06T0{i}:00:00Z"}


# --- the cold-start guard ------------------------------------------------

def test_cold_start_places_nothing(monkeypatch, tmp_path):
    """First ever poll: everything already on his record is marked seen."""
    _wire(monkeypatch, {"trades": [_trade(i) for i in range(5)]})
    state_file = tmp_path / "seen.json"

    assert ff.poll(FakeClient(), path=state_file) == []

    saved = json.loads(state_file.read_text())
    assert saved["seeded"] is True
    assert len(saved["seen"]) == 5


def test_a_restart_does_not_replay(monkeypatch, tmp_path):
    """The state file survives a restart, so trades are never re-copied."""
    trades = [_trade(i) for i in range(5)]
    _wire(monkeypatch, {"trades": trades})
    state_file = tmp_path / "seen.json"

    ff.poll(FakeClient(), path=state_file)              # cold start
    assert ff.poll(FakeClient(), path=state_file) == []  # restart, same data


def test_only_the_new_trade_comes_back(monkeypatch, tmp_path):
    trades = [_trade(i) for i in range(3)]
    state_file = tmp_path / "seen.json"
    _wire(monkeypatch, {"trades": list(trades)})
    ff.poll(FakeClient(), path=state_file)              # seed

    trades.append(_trade(99, ticker="KXMLBGAME-26AUG06NYMCLE-NYM"))
    _wire(monkeypatch, {"trades": trades})
    fresh = ff.poll(FakeClient(), path=state_file)

    assert len(fresh) == 1
    assert fresh[0]["ticker"] == "KXMLBGAME-26AUG06NYMCLE-NYM"


def test_the_same_new_trade_is_not_returned_twice(monkeypatch, tmp_path):
    trades = [_trade(1)]
    state_file = tmp_path / "seen.json"
    _wire(monkeypatch, {"trades": list(trades)})
    ff.poll(FakeClient(), path=state_file)

    trades.append(_trade(2))
    _wire(monkeypatch, {"trades": trades})
    assert len(ff.poll(FakeClient(), path=state_file)) == 1
    assert ff.poll(FakeClient(), path=state_file) == []


# --- failing loudly ------------------------------------------------------

def test_dead_feed_raises_rather_than_looking_quiet(monkeypatch, tmp_path):
    """401 must not read as 'he has not traded'."""
    _wire(monkeypatch, {"error": "token_authentication_failure"}, status=401)
    with pytest.raises(ff.FeedError) as exc:
        ff.poll(FakeClient(), path=tmp_path / "seen.json")
    assert "BLIND" in str(exc.value)


def test_expired_token_message_names_the_fix(monkeypatch, tmp_path):
    monkeypatch.setattr(ff, "SESSION_TOKEN", "stale-token")
    _wire(monkeypatch, {}, status=401)
    with pytest.raises(ff.FeedError) as exc:
        ff.poll(FakeClient(), path=tmp_path / "seen.json")
    assert "FOLLOW_SESSION_TOKEN" in str(exc.value)


def test_api_key_is_tried_before_the_session_token(monkeypatch, tmp_path):
    """The key never expires, so it must win when both are available."""
    monkeypatch.setattr(ff, "SESSION_TOKEN", "tok")
    used = []
    monkeypatch.setattr(ff, "_signed_get",
                        lambda c, p, params=None: used.append("api_key")
                        or _resp({"trades": []}))
    monkeypatch.setattr(ff, "_token_get",
                        lambda c, p, params=None, scheme="Bearer":
                        used.append("session") or _resp({"trades": []}))
    ff.fetch(FakeClient(), "/v1/users/x/trades")
    assert used == ["api_key"]


# --- normalising an unknown shape ---------------------------------------

def test_price_handles_cents_and_dollar_strings():
    assert ff.price_to_cents(42) == 42
    assert ff.price_to_cents("42") == 42
    assert ff.price_to_cents("0.4200") == 42      # the v2 tape's format
    assert ff.price_to_cents(None) is None
    assert ff.price_to_cents("garbage") is None


def test_side_normalises_across_field_spellings():
    assert ff.normalise_side("yes") == "yes"
    assert ff.normalise_side("NO") == "no"
    assert ff.normalise_side("") is None
    assert ff.normalise_side("maybe") is None


def test_normalise_reads_alternative_field_names():
    """The response shape is unknown until --probe runs against a real
    account, so the reader accepts the plausible spellings."""
    sig = ff.normalise_trade({"market_ticker": "KX-A", "taker_side": "no",
                              "yes_price_dollars": "0.6100", "quantity": 12,
                              "id": "abc"})
    assert sig["ticker"] == "KX-A"
    assert sig["side"] == "no"
    assert sig["price_cents"] == 61
    assert sig["contracts"] == 12
    assert sig["trade_id"] == "abc"


def test_unreadable_record_yields_no_side():
    """Junk must produce a signal the runner will skip, not a bad trade."""
    assert ff.normalise_trade({"nonsense": 1})["side"] is None
    assert ff.normalise_trade("not a dict") == {}


def test_records_are_found_in_any_envelope():
    for env in ({"trades": [{"a": 1}]}, {"fills": [{"a": 1}]},
                {"results": [{"a": 1}]}, [{"a": 1}]):
        assert ff.extract_records(env) == [{"a": 1}]
    assert ff.extract_records({"unexpected": "shape"}) == []


def test_trade_key_falls_back_when_there_is_no_id():
    """Two distinct fills must stay distinguishable without an API id."""
    a = ff.normalise_trade({"ticker": "KX-A", "side": "yes", "price": 42,
                            "count": 10, "created_time": "T1"})
    b = ff.normalise_trade({"ticker": "KX-A", "side": "yes", "price": 42,
                            "count": 10, "created_time": "T2"})
    assert ff.trade_key(a) != ff.trade_key(b)


def test_seen_list_is_bounded(monkeypatch, tmp_path):
    """The state file must not grow forever."""
    monkeypatch.setattr(ff, "MAX_SEEN", 10)
    state = {"seen": [str(i) for i in range(50)], "seeded": True}
    ff.save_state(state, tmp_path / "seen.json")
    assert len(json.loads((tmp_path / "seen.json").read_text())["seen"]) == 10
