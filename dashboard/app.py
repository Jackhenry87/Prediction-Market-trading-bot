"""Read-only live dashboard — a personal "business page" for the bot.

    uvicorn dashboard.app:app --port 8000    # http://localhost:8000

Two modes, picked automatically at startup:

  LIVE   — Kalshi credentials found in .env: seeds from trade_history.csv,
           then polls the account read-only (balance, fills, settlements,
           positions, quotes on held markets) and pushes every new event
           to the page over a WebSocket.
  REPLAY — no credentials: replays trade_history.csv on a loop so the page
           is fully animated with the account's real past trades.

READ-ONLY BY CONSTRUCTION: nothing here imports or calls order placement,
and the page has no control that could change what the bot does. Optional
login gate via DASHBOARD_PASSWORD (see config.load_dashboard_settings).
"""

import asyncio
import hmac
import random
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from config import load_dashboard_settings
from dashboard import data
from kalshi_client import KalshiClient, KalshiError
from trade_logger import get_logger, setup_logging

log = get_logger("dashboard")

BASE = Path(__file__).resolve().parent
INDEX_HTML = BASE / "static" / "index.html"
FEED_LIMIT = 120          # trades kept in the snapshot feed
QUOTE_POSITIONS = 10      # max held markets to poll public quotes for

settings = load_dashboard_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()  # mirror to stdout so hosted logs show mode & poll errors
    state["trades"] = data.load_history()
    state["updated_at"] = data.now_iso()
    client = None
    has_key = bool(settings.kalshi_private_key_pem
                   or settings.kalshi_private_key_path)
    if settings.kalshi_api_key_id and has_key:
        try:
            client = KalshiClient(settings.kalshi_api_key_id,
                                  settings.kalshi_private_key_path or None,
                                  settings.kalshi_env,
                                  private_key_pem=(
                                      settings.kalshi_private_key_pem or None))
        except Exception as exc:  # bad key must not take the site down
            pem = settings.kalshi_private_key_pem
            if pem and "-----BEGIN" not in pem:
                why = ("the pasted value has no '-----BEGIN ... KEY-----' "
                       "first line — copy the WHOLE .pem file, not just the "
                       "middle part")
            elif pem and "-----END" not in pem:
                why = ("the pasted value is cut off (no '-----END ... "
                       "KEY-----' last line) — re-copy the WHOLE .pem file")
            else:
                why = (f"{type(exc).__name__}: {exc} — re-paste the full "
                       f".pem into KALSHI_PRIVATE_KEY_PEM")
            state["live_error"] = f"private key rejected: {why}"
    elif settings.kalshi_api_key_id:
        state["live_error"] = ("KALSHI_API_KEY_ID is set but no private key "
                               "found — set KALSHI_PRIVATE_KEY_PEM")
    elif has_key:
        state["live_error"] = ("private key found but KALSHI_API_KEY_ID is "
                               "empty — set it to the key's ID from Kalshi")
    if state["live_error"]:
        log.error("%s — starting in REPLAY mode instead", state["live_error"])
    if client:
        state["mode"] = "live"
        task = asyncio.create_task(live_poller(client))
        log.info("dashboard LIVE (%s), polling every %ss",
                 settings.kalshi_env, settings.poll_seconds)
    else:
        state["mode"] = "replay"
        task = asyncio.create_task(replayer())
        log.info("dashboard REPLAY mode (no usable Kalshi credentials)")
    yield
    task.cancel()


app = FastAPI(title="Kalshi bot dashboard", docs_url=None, redoc_url=None,
              lifespan=lifespan)

state = {
    "mode": "replay",
    "env": settings.kalshi_env,
    "balance_usd": None,
    "trades": [],            # uniform dicts (data.py), oldest first
    "positions": [],         # [{ticker, theme, position, exposure_usd, last_price_cents}]
    "resting_orders": 0,
    "exchange_active": None,
    "updated_at": None,
    "live_error": None,   # why we're in replay mode, shown on the page
}
sockets: set = set()


async def broadcast(event: dict) -> None:
    dead = []
    for ws in sockets:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        sockets.discard(ws)


def snapshot() -> dict:
    return {
        "mode": state["mode"],
        "env": state["env"],
        "balance_usd": state["balance_usd"],
        "stats": data.compute_stats(state["trades"]),
        "feed": state["trades"][-FEED_LIMIT:][::-1],  # newest first
        "positions": state["positions"],
        "resting_orders": state["resting_orders"],
        "exchange_active": state["exchange_active"],
        "updated_at": state["updated_at"],
        "live_error": state["live_error"],
    }


# ---------- live mode ----------

def _fill_key(fill: dict) -> str:
    return fill.get("trade_id") or (
        f"{fill.get('created_time')}|{fill.get('ticker')}|{fill.get('count')}"
    )


def _settle_key(s: dict) -> str:
    return f"{s.get('ticker')}|{s.get('settled_time')}"


def _positions_view(raw: dict, quotes: dict) -> list:
    out = []
    for p in raw.get("market_positions", []):
        qty = float(p.get("position", 0) or 0)
        if qty == 0:
            continue
        ticker = p.get("ticker", "")
        exposure = abs(float(p.get("market_exposure", 0) or 0)) / 100.0
        out.append({
            "ticker": ticker,
            "theme": data.theme_of(ticker),
            "position": qty,
            "exposure_usd": round(exposure, 2),
            "last_price_cents": quotes.get(ticker),
        })
    out.sort(key=lambda p: -p["exposure_usd"])
    return out


async def live_poller(client: KalshiClient) -> None:
    seen_fills, seen_settles = set(), set()
    # Everything already in the CSV seed is history, not a fresh event.
    # Keep retrying the seed — a failure here (bad creds, network blip at
    # boot) must not silently kill the poller task.
    start_ts = max([t["ts"] for t in state["trades"]], default=0)
    while True:
        try:
            for f in await asyncio.to_thread(client.get_fills,
                                             start_ts or None):
                seen_fills.add(_fill_key(f))
            for s in await asyncio.to_thread(client.get_settlements,
                                             start_ts or None):
                seen_settles.add(_settle_key(s))
            break
        except (KalshiError, OSError) as exc:
            log.warning("live seed failed, retrying in %ss: %s",
                        settings.poll_seconds, exc)
            await asyncio.sleep(settings.poll_seconds)

    quotes: dict = {}
    while True:
        try:
            balance = await asyncio.to_thread(client.get_balance_cents)
            new_balance = round(balance / 100.0, 2)
            if new_balance != state["balance_usd"]:
                state["balance_usd"] = new_balance
                await broadcast({"type": "balance", "balance_usd": new_balance})

            for f in await asyncio.to_thread(client.get_fills, start_ts or None):
                key = _fill_key(f)
                if key in seen_fills:
                    continue
                seen_fills.add(key)
                trade = data.fill_to_trade(f)
                state["trades"].append(trade)
                await broadcast({"type": "trade", "trade": trade,
                                 "stats": data.compute_stats(state["trades"])})

            for s in await asyncio.to_thread(client.get_settlements,
                                             start_ts or None):
                key = _settle_key(s)
                if key in seen_settles:
                    continue
                seen_settles.add(key)
                await broadcast({"type": "settle",
                                 "ticker": s.get("ticker", ""),
                                 "result": s.get("market_result", ""),
                                 "revenue_usd": float(
                                     s.get("revenue_dollars")
                                     or (s.get("revenue", 0) or 0) / 100.0)})

            raw_positions = await asyncio.to_thread(client.get_positions)
            resting = await asyncio.to_thread(client.get_resting_orders)
            state["resting_orders"] = len(resting)

            held = [p.get("ticker") for p in
                    raw_positions.get("market_positions", [])
                    if float(p.get("position", 0) or 0) != 0][:QUOTE_POSITIONS]
            for ticker in held:
                try:  # public endpoint; a dead market must not kill the loop
                    market = await asyncio.to_thread(client.get_market, ticker)
                    price = market.get("last_price")
                    if price is not None and quotes.get(ticker) != price:
                        quotes[ticker] = price
                        await broadcast({"type": "quote", "ticker": ticker,
                                         "last_price_cents": price})
                except KalshiError:
                    pass
            state["positions"] = _positions_view(raw_positions, quotes)

            status = await asyncio.to_thread(client.get_exchange_status)
            state["exchange_active"] = bool(status.get("trading_active"))
            state["updated_at"] = data.now_iso()
            await broadcast({"type": "heartbeat",
                             "updated_at": state["updated_at"],
                             "exchange_active": state["exchange_active"],
                             "resting_orders": state["resting_orders"],
                             "positions": state["positions"]})
        except (KalshiError, OSError) as exc:
            log.warning("poll failed, retrying next cycle: %s", exc)
        await asyncio.sleep(settings.poll_seconds)


# ---------- replay mode ----------

async def replayer() -> None:
    """Loop the real CSV history as a live-looking stream. Payloads carry
    replay=True and the page shows a REPLAY badge — never fake 'live'."""
    history = data.load_history()
    if not history:
        log.warning("trade_history.csv empty — replay has nothing to show")
        return

    def walk_steps():
        """Cosmetic balance deltas: cash out per fill, each market's
        settlement P&L credited once (the CSV repeats it per fill)."""
        settled = set()
        for trade in history:
            delta = -trade["cost_usd"]
            if trade["pnl_usd"] is not None and trade["ticker"] not in settled:
                settled.add(trade["ticker"])
                delta += trade["cost_usd"] + trade["pnl_usd"]
            yield trade, round(delta, 2)

    # Start high enough that the walk never dips below zero.
    low, running = 0.0, 0.0
    for _, delta in walk_steps():
        running += delta
        low = min(low, running)
    start_balance = round(20.0 - low, 2)

    while True:
        state["trades"] = []
        balance = start_balance
        for trade, delta in walk_steps():
            await asyncio.sleep(random.uniform(2.0, 5.0))
            state["trades"].append(trade)
            balance = round(balance + delta, 2)
            state["balance_usd"] = balance
            state["updated_at"] = data.now_iso()
            await broadcast({"type": "trade", "trade": trade, "replay": True,
                             "stats": data.compute_stats(state["trades"]),
                             "balance_usd": balance})
            if trade["settlement"]:
                await asyncio.sleep(random.uniform(0.6, 1.4))
                await broadcast({"type": "settle", "replay": True,
                                 "ticker": trade["ticker"],
                                 "result": trade["settlement"],
                                 "pnl_usd": trade["pnl_usd"]})
        await asyncio.sleep(6)


# ---------- wiring ----------

def _authed(request) -> bool:
    if not settings.password:
        return True
    cookie = request.cookies.get("dash_key", "")
    return hmac.compare_digest(cookie, settings.password)


LOGIN_HTML = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<body style="background:#0d0d0d;color:#fff;font-family:system-ui;display:grid;place-items:center;height:100vh;margin:0">
<form method=post action=/login style="text-align:center">
<p style="color:#c3c2b7">Autotrader dashboard</p>
<input type=password name=password autofocus style="padding:10px;border-radius:8px;border:1px solid #383835;background:#1a1a19;color:#fff">
<button style="padding:10px 16px;border-radius:8px;border:0;background:#3987e5;color:#fff;margin-left:6px">Enter</button>
</form></body>"""


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _authed(request):
        return HTMLResponse(LOGIN_HTML, status_code=401)
    return FileResponse(INDEX_HTML)


@app.post("/login")
async def login(password: str = Form("")):
    if not settings.password or not hmac.compare_digest(password,
                                                        settings.password):
        return HTMLResponse(LOGIN_HTML, status_code=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("dash_key", password, httponly=True, samesite="lax")
    return resp


@app.get("/api/snapshot")
async def api_snapshot(request: Request):
    if not _authed(request):
        return JSONResponse({"error": "auth required"}, status_code=401)
    return snapshot()


@app.get("/api/history")
async def api_history(request: Request):
    """Every fill, oldest first — feeds the History and Models tabs.
    In live mode that's the CSV seed plus everything seen since; in
    replay mode the full CSV (not just the part replayed so far)."""
    if not _authed(request):
        return JSONResponse({"error": "auth required"}, status_code=401)
    trades = (state["trades"] if state["mode"] == "live"
              else data.load_history())
    return {"mode": state["mode"], "trades": trades}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if settings.password and not hmac.compare_digest(
            ws.cookies.get("dash_key", ""), settings.password):
        await ws.close(code=4401)
        return
    await ws.accept()
    sockets.add(ws)
    try:
        await ws.send_json({"type": "snapshot", **snapshot()})
        while True:
            await ws.receive_text()  # keepalive pings from the page
    except WebSocketDisconnect:
        pass
    finally:
        sockets.discard(ws)
