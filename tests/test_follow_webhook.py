"""The notification receiver.

A POST here can end in a live order, so the auth behaviour is a safety
property, not a convenience. These tests pin it.
"""

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_key(monkeypatch, tmp_path):
    monkeypatch.setenv("FOLLOW_WEBHOOK_KEY", "secret-key")
    monkeypatch.setenv("FOLLOW_STORE", str(tmp_path / "inbox.jsonl"))
    import follow_webhook
    importlib.reload(follow_webhook)
    return follow_webhook


@pytest.fixture
def client(app_with_key):
    return TestClient(app_with_key.app)


def test_accepts_a_notification_with_the_key(client):
    r = client.post("/follow/notify", json={"text": "he bought something"},
                    headers={"X-API-Key": "secret-key"})
    assert r.status_code == 202
    assert r.json()["accepted"] is True


def test_rejects_a_missing_key(client):
    r = client.post("/follow/notify", json={"text": "x"})
    assert r.status_code == 401


def test_rejects_a_wrong_key(client):
    r = client.post("/follow/notify", json={"text": "x"},
                    headers={"X-API-Key": "not-the-key"})
    assert r.status_code == 401


def test_refuses_to_serve_when_no_key_is_configured(monkeypatch, tmp_path):
    """An unauthenticated trade trigger is worse than a broken one: it would
    let anyone on the internet make this account place orders."""
    monkeypatch.delenv("FOLLOW_WEBHOOK_KEY", raising=False)
    monkeypatch.setenv("FOLLOW_STORE", str(tmp_path / "inbox.jsonl"))
    import follow_webhook
    importlib.reload(follow_webhook)
    c = TestClient(follow_webhook.app)
    assert c.post("/follow/notify", json={"text": "x"},
                  headers={"X-API-Key": "anything"}).status_code == 503


def test_pending_returns_what_was_posted(client):
    client.post("/follow/notify", json={"text": "first"},
                headers={"X-API-Key": "secret-key"})
    client.post("/follow/notify", json={"text": "second"},
                headers={"X-API-Key": "secret-key"})
    r = client.get("/follow/pending", params={"since": 0},
                   headers={"X-API-Key": "secret-key"})
    events = r.json()["events"]
    assert [e["text"] for e in events] == ["first", "second"]


def test_cursor_filters_already_seen_events(client):
    client.post("/follow/notify", json={"text": "first"},
                headers={"X-API-Key": "secret-key"})
    client.post("/follow/notify", json={"text": "second"},
                headers={"X-API-Key": "secret-key"})
    r = client.get("/follow/pending", params={"since": 1},
                   headers={"X-API-Key": "secret-key"})
    assert [e["text"] for e in r.json()["events"]] == ["second"]


def test_title_and_body_are_both_kept(client):
    """iOS sends the notification's title and body separately, and the market
    name usually lives in the title."""
    client.post("/follow/notify",
                json={"title": "Los Angeles D vs Chicago C",
                      "body": "blushing.wildebeest7119 bought Yes at 42c"},
                headers={"X-API-Key": "secret-key"})
    text = client.get("/follow/pending",
                      headers={"X-API-Key": "secret-key"}).json()["events"][0]["text"]
    assert "Los Angeles D vs Chicago C" in text
    assert "blushing.wildebeest7119" in text


def test_plain_text_body_is_accepted(client):
    """Not every Shortcut configuration sends JSON."""
    r = client.post("/follow/notify", content=b"raw notification text",
                    headers={"X-API-Key": "secret-key"})
    assert r.status_code == 202


def test_empty_notification_is_rejected(client):
    r = client.post("/follow/notify", json={"text": "   "},
                    headers={"X-API-Key": "secret-key"})
    assert r.status_code == 400


def test_oversized_body_is_rejected(client):
    r = client.post("/follow/notify", content=b"x" * 9000,
                    headers={"X-API-Key": "secret-key"})
    assert r.status_code == 413


def test_health_needs_no_key_and_leaks_nothing(client):
    r = client.get("/follow/health")
    assert r.status_code == 200
    assert set(r.json()) == {"ok", "configured"}
