# 🎲 Paper Sportsbook

A **play-money** sports-betting website. Users sign up, get $1,000 in fake
money, and bet on real games across **all in-season sports**. Bets settle
automatically against real results. There's a public leaderboard, and a
JSON API so a bot can place paper bets too. **No real money, ever.**

## Run locally

```bash
pip install -r paperbook/requirements.txt
python -m paperbook.loader load        # pull games + odds (needs ODDS_API_KEY)
uvicorn paperbook.app:app --reload     # open http://127.0.0.1:8000
```

Without `ODDS_API_KEY` the loader seeds a couple of demo games so you can
click around.

## Put it online (free tier)

The repo includes `paperbook/render.yaml` for **Render.com**:

1. Push this repo to GitHub (already done).
2. On render.com → New → Blueprint → point it at this repo.
3. It reads `render.yaml`, builds, and gives you a public URL.
4. Set the `ODDS_API_KEY` env var in the Render dashboard (from
   the-odds-api.com). `SECRET_KEY` is auto-generated.
5. Add a Render **Cron Job** running `python -m paperbook.loader load`
   (e.g. hourly) and `python -m paperbook.loader settle` (e.g. every few
   hours) so games stay fresh and bets settle.

Railway / Fly.io work the same way with the `Procfile`.

## Bot / API access

Every user has an API key (shown on their **My Bets** page). The bot sends
it as `X-API-Key`:

```
GET  /api/games                 open games with odds
GET  /api/me                    balance + bet history
POST /api/bets  {game_id, side, stake_cents}   side = "home" | "away"
```

`paperbook_client.py` (repo root) wraps this — set `PAPERBOOK_URL` and
`PAPERBOOK_KEY` and the trading bot can place paper bets on your site the
same way it trades Kalshi.

## Files

- `app.py` — FastAPI web UI + JSON API (bcrypt passwords, signed-cookie
  sessions).
- `db.py` — SQLite users / games / bets, bet placement + settlement.
- `loader.py` — load all in-season sports' games+odds; settle by score.
- `templates/` — server-rendered pages.

## Honest caveats

- **Play money only.** A real-money sportsbook is a licensed, regulated
  business — this is a fantasy/simulation game.
- Loading **all** sports every hour uses The Odds API quota quickly; the
  free tier will run out. Trim sports or use a paid tier if needed.
- MVP covers **moneyline** (win/lose) bets. Spreads and totals are a
  natural next step.
- SQLite is fine for a small site; move to Postgres if it grows.
- You are responsible for your users' data and any terms of service.
