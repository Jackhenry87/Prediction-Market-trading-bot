# Release-time runner on a $5 VPS (Option 2B)

The macro resolution-lag edge needs a process that fires exactly at release
times and polls fast — GitHub cron is too imprecise. This runs it on a
small always-on server.

## What you need
- A cheap VPS (DigitalOcean/Hetzner/Fly, ~$4–6/mo).
- Your Kalshi API key + `.pem`, plus a free FRED API key
  (https://fred.stlouisfed.org/docs/api/api_key.html).

## Setup (Ubuntu VPS)
```bash
sudo git clone <your repo> /opt/trading-bot && cd /opt/trading-bot
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env      # then edit .env (see below)
# put your kalshi_prod.pem on the box too
```
Edit `.env`:
```
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY_PATH=/opt/trading-bot/kalshi_prod.pem
KALSHI_ENV=prod
FRED_API_KEY=...
ENABLED_MODELS=macro
DRY_RUN=true          # PAPER FIRST — verify the ticker/series mappings
MAX_ORDER_SIZE=2
MAX_TOTAL_EXPOSURE=20
```
Install the service:
```bash
sudo cp deploy/release-runner.service /etc/systemd/system/
sudo systemctl enable --now release-runner
journalctl -u release-runner -f     # watch it
```

Or with Docker:
```bash
docker build -f deploy/Dockerfile -t trading-bot .
docker run --env-file .env -v $PWD/kalshi_prod.pem:/app/kalshi_prod.pem trading-bot
```

## First: prove it in paper
Keep `DRY_RUN=true` through at least one jobless-claims Thursday and one
CPI/jobs print. Watch the log: it should detect the fresh FRED value and
log resolution-lag signals. Confirm the Kalshi `KX...` series tickers in
`strategy_macro.py` actually return markets (fix any that don't) and that
the signals point at the correct side. Only then set `DRY_RUN=false`.

## Honest limits
- You won't beat co-located HFT on watched prints (CPI/NFP). The realistic
  edge is thinner, less-watched releases (e.g. jobless claims) where the
  book reprices slowly.
- FRED updates shortly *after* the official release, not at the instant.
- The bot's key lives on the VPS — harden it (SSH keys, firewall, no root
  login).
