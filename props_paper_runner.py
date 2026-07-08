"""Player-props PAPER runner: auto-place the model's value picks onto the
paperbook site AND record them to a committed ledger for scoring.

This never touches Kalshi or real money. Each pass:
  1. props_model.scan -> the day's best DFS-vs-sharp value picks
  2. post each as a prop market on paperbook (the "book") + place a paper bet
     the value side, via the JSON API  (only if PAPERBOOK_URL is configured)
  3. always append the pick to paper_trades_props.csv with its edge-vs-sharp,
     so we can measure hit-rate and CLV even with no site hosted yet

Why both: the committed CSV persists in the repo like every other model's
ledger (works headless on GitHub Actions, where the site's SQLite is
ephemeral); the paperbook POST is what makes the pick show up on the actual
website once you host it.

    python props_paper_runner.py --once     # single scan+place pass
    python props_paper_runner.py            # loop for PROPS_RUN_MINUTES
"""

import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

import props_model
from trade_logger import get_logger, setup_logging

log = get_logger("props_paper_runner")

PAPER_LOG = props_model.PAPER_LOG
PAPERBOOK_URL = os.getenv("PAPERBOOK_URL", "").strip().rstrip("/")
PAPERBOOK_API_KEY = os.getenv("PAPERBOOK_API_KEY", "").strip()
STAKE_DOLLARS = float(os.getenv("PROPS_STAKE_DOLLARS", "50"))
POLL_SECONDS = int(os.getenv("PROPS_POLL_SECONDS", "3600"))   # props move slowly
RUN_MINUTES = float(os.getenv("PROPS_RUN_MINUTES", "0"))       # 0 = single pass
SPORT = os.getenv("PROPS_SPORT", "MLB")


def market_id(pick: dict) -> str:
    """Deterministic id so re-posting refreshes odds instead of duplicating."""
    who = props_model.norm_name(pick["player"])
    raw = f"{pick['source']}_{pick['market']}_{who}_{pick['line']}"
    return re.sub(r"[^a-z0-9_.]+", "_", raw.lower()).strip("_")


def _post(path: str, body: dict):
    r = requests.post(f"{PAPERBOOK_URL}{path}", json=body, timeout=15,
                      headers={"X-API-Key": PAPERBOOK_API_KEY})
    r.raise_for_status()
    return r.json()


def place_on_paperbook(pick: dict, mid: str) -> bool:
    """Post the market + place the paper bet on the value side. Returns True on
    success. No-op (False) if the site isn't configured."""
    if not (PAPERBOOK_URL and PAPERBOOK_API_KEY):
        return False
    decimal = pick["decimal"]
    _post("/api/props", {
        "id": mid, "sport": SPORT, "player": pick["player"],
        "stat": pick["display_stat"], "line": pick["line"],
        # the offered side gets its real price; the other side is nominal
        "over_odds": decimal if pick["side"] == "over" else 1.90,
        "under_odds": decimal if pick["side"] == "under" else 1.90,
        "commence_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    res = _post("/api/prop_bets", {
        "market_id": mid, "side": pick["side"],
        "stake_cents": int(round(STAKE_DOLLARS * 100))})
    log.info("paperbook: bet %s on %s (balance now $%.2f)", pick["side"], mid,
             res.get("balance_cents", 0) / 100.0)
    return True


def append_ledger(picks: list) -> None:
    new = not PAPER_LOG.exists()
    with open(PAPER_LOG, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["scanned_at_utc", "source", "player", "stat", "line",
                        "side", "dfs_decimal", "sharp_prob", "edge_pct",
                        "books", "market_id", "outcome", "clv_pct"])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for p in picks:
            w.writerow([now, p["source"], p["player"], p["display_stat"],
                        p["line"], p["side"], f"{p['decimal']:.2f}",
                        f"{p['sharp_prob']:.3f}", f"{p['edge_pct']:.1f}",
                        p["books"], market_id(p), "", ""])


def props_pass(api_key: str, session: set) -> int:
    try:
        picks = props_model.scan(api_key)
    except Exception as exc:
        log.error("Props scan failed: %s", exc)
        return 0
    fresh = [p for p in picks if market_id(p) not in session]
    if not fresh:
        log.info("No new value picks this pass.")
        return 0
    append_ledger(fresh)
    placed = 0
    for p in fresh:
        mid = market_id(p)
        log.info("VALUE: %s %s %.1f %s @ %.2f | sharp %.0f%% | +%.1f%% edge",
                 p["player"], p["display_stat"], p["line"], p["side"].upper(),
                 p["decimal"], 100 * p["sharp_prob"], p["edge_pct"])
        try:
            if place_on_paperbook(p, mid):
                placed += 1
        except Exception as exc:
            log.warning("paperbook place failed for %s: %s (ledgered anyway)",
                        mid, exc)
        session.add(mid)
    return placed


def main() -> int:
    setup_logging()
    api_key = os.getenv("ODDSBLAZE_KEY", "").strip()
    if not api_key:
        log.error("ODDSBLAZE_KEY not set. Add your OddsBlaze key to repo secrets.")
        return 1
    if PAPERBOOK_URL and PAPERBOOK_API_KEY:
        log.info("PROPS PAPER: posting to %s + ledger %s", PAPERBOOK_URL,
                 PAPER_LOG.name)
    else:
        log.info("PROPS PAPER: no PAPERBOOK_URL set — ledger-only (%s). Set "
                 "PAPERBOOK_URL + PAPERBOOK_API_KEY to auto-place on the site.",
                 PAPER_LOG.name)

    session: set = set()
    once = "--once" in sys.argv or RUN_MINUTES <= 0
    deadline = time.time() + RUN_MINUTES * 60
    total = 0
    while True:
        total += props_pass(api_key, session)
        if once or time.time() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    log.info("Session done: %d pick(s) placed on the site, %d ledgered.",
             total, len(session))
    return 0


if __name__ == "__main__":
    sys.exit(main())
