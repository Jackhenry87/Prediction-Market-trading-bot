"""Web-layer tests for the dashboard. Like the paperbook suite these skip
cleanly when the optional web deps (dashboard/requirements.txt) are absent."""

import pytest

pytest.importorskip("fastapi", reason="dashboard deps not installed "
                    "(pip install -r dashboard/requirements.txt)")

from fastapi.testclient import TestClient  # noqa: E402

import dashboard.app as app_mod  # noqa: E402


@pytest.fixture()
def client():
    # No lifespan: don't start pollers/replayers in tests.
    return TestClient(app_mod.app)


def test_snapshot_shape(client, monkeypatch):
    monkeypatch.setitem(app_mod.state, "trades", [])
    r = client.get("/api/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"mode", "stats", "feed", "positions", "balance_usd"}
    assert body["stats"]["trades"] == 0


def test_index_serves_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Autotrader" in r.text and "/ws" in r.text


def test_password_gate(client, monkeypatch):
    monkeypatch.setattr(app_mod.settings, "password", "hunter2")
    assert client.get("/api/snapshot").status_code == 401
    assert client.get("/").status_code == 401
    assert client.post("/login", data={"password": "wrong"}).status_code == 401
    ok = client.post("/login", data={"password": "hunter2"},
                     follow_redirects=False)
    assert ok.status_code == 303
    client.cookies.set("dash_key", "hunter2")
    assert client.get("/api/snapshot").status_code == 200


def test_readonly_by_construction():
    """The dashboard module must hold no reference to order placement."""
    import inspect
    src = inspect.getsource(app_mod) + inspect.getsource(app_mod.data)
    assert "create_limit_order" not in src
    assert "cancel_order" not in src
