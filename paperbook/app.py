"""Paper sportsbook — a play-money betting site with a web UI for people
and a JSON API for bots. NO real money. NO real wagering.

    uvicorn paperbook.app:app --reload      # local dev

Web:  /            games + leaderboard
      /signup /login /logout
      /bet         place a paper bet (form)
      /mybets      your open/settled bets + API key
API (header  X-API-Key: <your key>):
      GET  /api/games          open games with odds
      GET  /api/me             balance + bets
      POST /api/bets           {game_id, side, stake_cents}
"""

import os
from pathlib import Path

import bcrypt
from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

from . import db

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
signer = URLSafeSerializer(os.getenv("SECRET_KEY", "dev-secret-change-me"), "sess")


def hash_pw(password: str) -> str:
    # bcrypt caps at 72 bytes; truncate deterministically
    return bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt()).decode()


def verify_pw(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode()[:72], hashed.encode())
    except ValueError:
        return False

app = FastAPI(title="Paper Sportsbook")
db.init_db()


# ---------- auth helpers ----------
def current_user(request: Request):
    tok = request.cookies.get("session")
    if not tok:
        return None
    try:
        return db.get_user(signer.loads(tok)["uid"])
    except (BadSignature, KeyError, TypeError):
        return None


def api_user(x_api_key: str = Header(default="")):
    user = db.get_user_by_key(x_api_key) if x_api_key else None
    if not user:
        raise HTTPException(401, "invalid or missing X-API-Key")
    return user


def _dollars(cents) -> str:
    return f"${cents/100:,.2f}"


templates.env.filters["dollars"] = _dollars


# ---------- web UI ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "user": current_user(request),
        "games": db.open_games(), "leaders": db.leaderboard()})


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {})


@app.post("/signup")
def signup(username: str = Form(...), email: str = Form(""),
           password: str = Form(...)):
    username = username.strip()
    if len(username) < 3 or len(password) < 6:
        raise HTTPException(400, "username >=3 and password >=6 chars")
    if db.get_user_by_name(username):
        raise HTTPException(400, "username taken")
    user = db.create_user(username, email.strip(), hash_pw(password))
    resp = RedirectResponse("/mybets", status_code=303)
    resp.set_cookie("session", signer.dumps({"uid": user["id"]}), httponly=True)
    return resp


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {})


@app.post("/login")
def login(username: str = Form(...), password: str = Form(...)):
    user = db.get_user_by_name(username.strip())
    if not user or not verify_pw(password, user["pw_hash"]):
        raise HTTPException(401, "wrong username or password")
    resp = RedirectResponse("/mybets", status_code=303)
    resp.set_cookie("session", signer.dumps({"uid": user["id"]}), httponly=True)
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("session")
    return resp


@app.post("/bet")
def web_bet(request: Request, game_id: str = Form(...), side: str = Form(...),
            stake_dollars: float = Form(...)):
    user = current_user(request)
    if not user:
        raise HTTPException(401, "log in first")
    try:
        db.place_bet(user["id"], game_id, side, int(round(stake_dollars * 100)))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return RedirectResponse("/mybets", status_code=303)


@app.get("/mybets", response_class=HTMLResponse)
def mybets(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "mybets.html", {
        "user": user, "bets": db.user_bets(user["id"])})


# ---------- JSON API (for bots) ----------
@app.get("/api/games")
def api_games(user=Depends(api_user)):
    return {"games": [dict(g) for g in db.open_games()]}


@app.get("/api/me")
def api_me(user=Depends(api_user)):
    return {"username": user["username"], "balance_cents": user["balance_cents"],
            "bets": [dict(b) for b in db.user_bets(user["id"])]}


@app.post("/api/bets")
async def api_place_bet(request: Request, user=Depends(api_user)):
    body = await request.json()
    try:
        bet = db.place_bet(user["id"], body["game_id"], body["side"],
                           int(body["stake_cents"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "bet": bet,
            "balance_cents": db.get_user(user["id"])["balance_cents"]}


@app.get("/healthz")
def healthz():
    return {"ok": True}
