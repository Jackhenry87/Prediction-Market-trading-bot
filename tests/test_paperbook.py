"""Tests for the paper sportsbook. Run: pytest tests/"""

import pytest


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRET_KEY", "test")
    import paperbook.db as db
    db.DB_PATH = tmp_path / "t.db"
    db.init_db()
    from fastapi.testclient import TestClient
    import importlib
    import paperbook.app as app_mod
    importlib.reload(app_mod)
    return TestClient(app_mod.app), db


def test_no_known_default_secret_blocks_forgery(monkeypatch, tmp_path):
    # With SECRET_KEY unset the app must NOT fall back to a public constant.
    # A cookie forged with the old default key must be rejected (no auth bypass).
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("PAPERBOOK_INSECURE_COOKIES", "1")
    import importlib
    import paperbook.db as db
    db.DB_PATH = tmp_path / "t.db"
    db.init_db()
    import paperbook.app as app_mod
    importlib.reload(app_mod)
    from fastapi.testclient import TestClient
    from itsdangerous import URLSafeSerializer
    c = TestClient(app_mod.app)
    forged = URLSafeSerializer("dev-secret-change-me", "sess").dumps({"uid": 1})
    r = c.get("/mybets", cookies={"session": forged}, follow_redirects=False)
    assert r.status_code == 303 and "/login" in r.headers["location"]


def test_session_cookie_is_hardened(client):
    c, _ = client
    r = c.post("/signup", data={"username": "amy", "password": "secret1"},
               follow_redirects=False)
    setc = r.headers.get("set-cookie", "").lower()
    assert "httponly" in setc and "samesite=lax" in setc and "secure" in setc


def test_signup_login_bet_settle(client):
    c, db = client
    assert c.post("/signup", data={"username": "bob", "password": "secret1"},
                  follow_redirects=False).status_code == 303
    u = db.get_user_by_name("bob")
    assert u["balance_cents"] == 100000
    key = u["api_key"]

    from paperbook.loader import load
    load("")  # demo games
    games = c.get("/api/games", headers={"X-API-Key": key}).json()["games"]
    gid, odds = games[0]["id"], games[0]["home_odds"]

    r = c.post("/api/bets", headers={"X-API-Key": key},
               json={"game_id": gid, "side": "home", "stake_cents": 5000})
    assert r.status_code == 200 and r.json()["balance_cents"] == 95000
    db.settle_game(gid, "home")
    assert db.get_user_by_name("bob")["balance_cents"] == 95000 + round(5000 * odds)


def test_auth_and_limits(client):
    c, db = client
    c.post("/signup", data={"username": "amy", "password": "secret1"})
    key = db.get_user_by_name("amy")["api_key"]
    from paperbook.loader import load
    load("")
    gid = c.get("/api/games", headers={"X-API-Key": key}).json()["games"][0]["id"]
    # bad key
    assert c.get("/api/me", headers={"X-API-Key": "nope"}).status_code == 401
    # over balance
    assert c.post("/api/bets", headers={"X-API-Key": key},
                  json={"game_id": gid, "side": "home",
                        "stake_cents": 99999999}).status_code == 400
    # wrong password
    assert c.post("/login", data={"username": "amy", "password": "x"},
                  follow_redirects=False).status_code == 401


def test_duplicate_username_rejected(client):
    c, _ = client
    c.post("/signup", data={"username": "dup", "password": "secret1"})
    assert c.post("/signup", data={"username": "dup", "password": "secret1"}
                  ).status_code == 400
