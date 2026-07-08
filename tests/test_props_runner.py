"""Props paper runner: ledgering, dedup, and end-to-end auto-placement onto
a live paperbook TestClient."""

import props_paper_runner as runner


PICK = dict(player="Aaron Judge", display_stat="Total Bases",
            market="batter_total_bases", line=1.5, side="over",
            decimal=1.80, sharp_prob=0.65, edge_pct=17.0, books=3,
            title="Aaron Judge Total Bases O/U", source="underdog")


def test_market_id_stable_and_sanitized():
    mid = runner.market_id(PICK)
    assert mid == "underdog_batter_total_bases_aaron_judge_1.5"
    assert runner.market_id(PICK) == mid  # deterministic


def test_ledger_and_dedup(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "PAPER_LOG", tmp_path / "props.csv")
    monkeypatch.setattr(runner.props_model, "scan", lambda *a, **k: [PICK])
    # no site configured -> ledger-only
    monkeypatch.setattr(runner, "PAPERBOOK_URL", "")
    session = set()
    assert runner.props_pass("k", session) == 0        # placed on site = 0
    assert (tmp_path / "props.csv").exists()
    body = (tmp_path / "props.csv").read_text()
    assert "Aaron Judge" in body and "over" in body
    lines_after_first = body.count("\n")
    # second pass: same pick is deduped, nothing re-ledgered
    runner.props_pass("k", session)
    assert (tmp_path / "props.csv").read_text().count("\n") == lines_after_first


def test_auto_places_on_paperbook(monkeypatch, tmp_path):
    # stand up a real paperbook instance and route the runner's HTTP POSTs
    # through its TestClient, proving a bet actually lands on the site.
    monkeypatch.setenv("SECRET_KEY", "test")
    import paperbook.db as db
    db.DB_PATH = tmp_path / "pb.db"
    db.init_db()
    import importlib
    import paperbook.app as app_mod
    importlib.reload(app_mod)
    from fastapi.testclient import TestClient
    c = TestClient(app_mod.app)
    c.post("/signup", data={"username": "botuser", "password": "secret1"})
    key = db.get_user_by_name("botuser")["api_key"]

    def fake_post(url, json, timeout, headers):
        path = url.replace("http://site", "")
        return c.post(path, json=json, headers=headers)

    monkeypatch.setattr(runner, "PAPER_LOG", tmp_path / "props.csv")
    monkeypatch.setattr(runner, "PAPERBOOK_URL", "http://site")
    monkeypatch.setattr(runner, "PAPERBOOK_API_KEY", key)
    monkeypatch.setattr(runner, "STAKE_DOLLARS", 50)
    monkeypatch.setattr(runner.requests, "post", fake_post)
    monkeypatch.setattr(runner.props_model, "scan", lambda *a, **k: [PICK])

    placed = runner.props_pass("k", set())
    assert placed == 1
    # the bet is really on the site: balance down $50, market listed
    assert db.get_user_by_name("botuser")["balance_cents"] == 100000 - 5000
    props = c.get("/api/props", headers={"X-API-Key": key}).json()["props"]
    assert len(props) == 1 and props[0]["player"] == "Aaron Judge"
