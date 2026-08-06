"""HTTPS receiver for followed-trader push notifications.

    uvicorn follow_webhook:app --host 0.0.0.0 --port $PORT

THE ONE THING KALSHI WON'T TELL US
----------------------------------
No Kalshi API attributes a trade to an account: the public tape has no user
field, `/users`, `/leaderboard` and `/social/*` are 404, and the web app's
`/v1/users/...` answers only to a browser session token. The single source
that knows "blushing.wildebeest7119 just bought X" is the push notification
on the owner's phone.

So an iOS Shortcut (Personal Automation -> "When I get a notification from
Kalshi") POSTs the notification text here within seconds, and
`follow_runner` drains it.

    POST /follow/notify     X-API-Key: <FOLLOW_WEBHOOK_KEY>
                            {"text": "<notification body>"}   (or raw text)
    GET  /follow/pending?since=<id>     X-API-Key: <same key>
    GET  /follow/health                 no auth, no data

THIS ENDPOINT SPENDS MONEY
--------------------------
A POST here can end in a live order, so:
  * the key is compared with `secrets.compare_digest` — a plain `==` on a
    secret leaks its prefix to a patient attacker through timing;
  * the app REFUSES TO START without FOLLOW_WEBHOOK_KEY set, rather than
    falling back to a default that would make the trigger world-writable;
  * the body is length-capped, so a notification cannot become a memory bomb;
  * nothing here parses, resolves or trades. This process only takes
    delivery. Every decision happens in the runner, behind the safety gate.

It is deliberately a SEPARATE app object from paperbook's: a public
play-money sportsbook and a live-money trigger must not share a request path
or an auth surface, even when they share a deploy.
"""

import json
import os
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request

MAX_BODY_BYTES = 8192
STORE = Path(os.getenv("FOLLOW_STORE",
                       Path(__file__).resolve().parent / "follow_inbox.jsonl"))

API_KEY = os.getenv("FOLLOW_WEBHOOK_KEY", "").strip()

app = FastAPI(title="follow-webhook", docs_url=None, redoc_url=None)

# In-process mirror of the store so the hot path (GET /follow/pending every
# few seconds) never re-reads the file.
_events = []
_next_id = 1


def _load() -> None:
    """Replay the store on boot so a restart mid-signal loses nothing."""
    global _next_id
    if not STORE.exists():
        return
    for line in STORE.read_text().splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        _events.append(rec)
        _next_id = max(_next_id, int(rec.get("id", 0)) + 1)


_load()


def _auth(key: str) -> None:
    """Constant-time key check. Fails closed when unconfigured."""
    if not API_KEY:
        raise HTTPException(
            503, "FOLLOW_WEBHOOK_KEY is not set — refusing to accept "
                 "trade triggers on an unauthenticated endpoint")
    if not key or not secrets.compare_digest(key, API_KEY):
        raise HTTPException(401, "invalid or missing X-API-Key")


@app.get("/follow/health")
def health():
    """Liveness only — no counts, no bodies, nothing that leaks his picks."""
    return {"ok": True, "configured": bool(API_KEY)}


@app.post("/follow/notify", status_code=202)
async def notify(request: Request, x_api_key: str = Header(default="")):
    """Take delivery of one notification. Returns immediately: the phone
    should never wait on our market lookups."""
    _auth(x_api_key)

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "notification body too large")

    text = ""
    try:
        payload = json.loads(raw or b"{}")
        if isinstance(payload, dict):
            # iOS Shortcuts can send title and body as separate fields; keep
            # both, since the market name often lives in the title.
            text = "\n".join(str(payload.get(k) or "") for k in
                             ("title", "subtitle", "body", "text")).strip()
        elif isinstance(payload, str):
            text = payload
    except ValueError:
        text = raw.decode("utf-8", "replace")

    if not text.strip():
        raise HTTPException(400, "empty notification")

    global _next_id
    rec = {"id": _next_id, "received_at": time.time(), "text": text}
    _next_id += 1
    _events.append(rec)
    try:
        with open(STORE, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass          # the in-process mirror still serves the runner

    return {"accepted": True, "id": rec["id"]}


@app.get("/follow/pending")
def pending(since: int = 0, x_api_key: str = Header(default="")):
    """Every notification newer than `since`. The runner tracks its own
    cursor, so a slow pass never drops a signal."""
    _auth(x_api_key)
    out = [e for e in _events if int(e.get("id", 0)) > int(since)]
    return {"events": out, "cursor": _next_id - 1}
