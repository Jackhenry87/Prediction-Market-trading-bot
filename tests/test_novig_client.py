"""Tests for the Novig client's OAuth flow, request plumbing and order build.
Endpoint PATHS are provisional (pending the live spec); these lock the auth,
token-refresh, paging and payload logic, which don't change when paths do."""

import pytest

import novig_client
from novig_client import NovigClient, NovigError


class FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = b"x" if json_data is not None else b""

    def json(self):
        return self._json


def test_token_fetched_once_and_cached(monkeypatch):
    c = NovigClient("id", "secret")
    calls = {"token": 0, "req": 0}

    def fake_post(url, data=None, timeout=None):
        calls["token"] += 1
        assert data["grant_type"] == "client_credentials"
        return FakeResp(200, {"access_token": "tok-abc", "expires_in": 3600})

    def fake_request(method, url, params=None, json=None, headers=None,
                     timeout=None):
        calls["req"] += 1
        assert headers["Authorization"] == "Bearer tok-abc"
        return FakeResp(200, {"balance_cents": 4200})

    monkeypatch.setattr(novig_client.requests, "post", fake_post)
    monkeypatch.setattr(novig_client.requests, "request", fake_request)
    assert c.get_balance_cents() == 4200
    assert c.get_balance_cents() == 4200
    assert calls["token"] == 1 and calls["req"] == 2   # token reused


def test_token_refreshes_when_expired(monkeypatch):
    c = NovigClient("id", "secret")
    tokens = iter(["tok-1", "tok-2"])

    monkeypatch.setattr(novig_client.requests, "post",
                        lambda url, data=None, timeout=None:
                        FakeResp(200, {"access_token": next(tokens),
                                       "expires_in": 3600}))
    monkeypatch.setattr(novig_client.requests, "request",
                        lambda *a, **k: FakeResp(200, {"balance_cents": 1}))
    c.get_balance_cents()
    assert c._token == "tok-1"
    c._token_expiry = 0            # force expiry
    c.get_balance_cents()
    assert c._token == "tok-2"


def test_401_triggers_one_reauth(monkeypatch):
    c = NovigClient("id", "secret")
    monkeypatch.setattr(novig_client.requests, "post",
                        lambda url, data=None, timeout=None:
                        FakeResp(200, {"access_token": "t", "expires_in": 3600}))
    seq = iter([FakeResp(401, text="expired"),
                FakeResp(200, {"balance_cents": 99})])
    monkeypatch.setattr(novig_client.requests, "request",
                        lambda *a, **k: next(seq))
    assert c.get_balance_cents() == 99      # recovered after re-auth


def test_missing_credentials_raises(monkeypatch):
    monkeypatch.delenv("NOVIG_CLIENT_ID", raising=False)
    monkeypatch.delenv("NOVIG_CLIENT_SECRET", raising=False)
    c = NovigClient()
    with pytest.raises(NovigError):
        c._access_token()


def test_balance_dollars_fallback(monkeypatch):
    c = NovigClient("id", "secret")
    c._token, c._token_expiry = "t", 1e18
    monkeypatch.setattr(novig_client.requests, "request",
                        lambda *a, **k: FakeResp(200, {"balance": "42.50"}))
    assert c.get_balance_cents() == 4250


def test_paging_follows_cursor(monkeypatch):
    c = NovigClient("id", "secret")
    c._token, c._token_expiry = "t", 1e18
    pages = iter([
        FakeResp(200, {"fills": [{"id": 1}], "cursor": "p2"}),
        FakeResp(200, {"fills": [{"id": 2}], "cursor": None}),
    ])
    monkeypatch.setattr(novig_client.requests, "request",
                        lambda *a, **k: next(pages))
    fills = c.get_fills()
    assert [f["id"] for f in fills] == [1, 2]


def test_create_order_payload(monkeypatch):
    c = NovigClient("id", "secret")
    c._token, c._token_expiry = "t", 1e18
    captured = {}

    def fake_request(method, url, params=None, json=None, headers=None,
                     timeout=None):
        captured.update(method=method, url=url, body=json)
        return FakeResp(200, {"order_id": "o1"})

    monkeypatch.setattr(novig_client.requests, "request", fake_request)
    c.create_limit_order("MKT-1", "yes", "buy", 10, 37)
    assert captured["method"] == "POST"
    assert captured["body"]["market_id"] == "MKT-1"
    assert captured["body"]["price"] == "0.3700" and captured["body"]["count"] == 10
    # out-of-range price fails closed
    with pytest.raises(NovigError):
        c.create_limit_order("MKT-1", "yes", "buy", 10, 0)
