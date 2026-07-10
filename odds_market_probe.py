"""One-off probe: does the-odds-api plan behind ODDS_API_KEY actually expose
FIRST-INNING (NRFI/YRFI) markets for MLB? Read-only, a couple of credits.

The NRFI/YRFI model needs a sharp first-inning line to devig against Kalshi's
KXMLBRFI. the-odds-api gates inning markets as paid "additional markets" fetched
per-event, so we must confirm they're reachable on THIS key before building.

Reports: key works?, which first-inning market keys return quotes, sample
prices, and remaining request credits.

    ODDS_API_KEY=... python odds_market_probe.py
"""

import os
import sys

import requests

from trade_logger import get_logger, setup_logging

log = get_logger("odds_market_probe")
BASE = "https://api.the-odds-api.com/v4/sports/baseball_mlb"
# candidate first-inning market keys (the-odds-api naming); we try each and see
FIRST_INNING_MARKETS = ["totals_1st_1_innings", "h2h_1st_1_innings",
                        "spreads_1st_1_innings"]


def _get(url, params):
    r = requests.get(url, params=params, timeout=25)
    rem = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    return r, rem, used


def main() -> int:
    setup_logging()
    key = os.getenv("ODDS_API_KEY", "").strip()
    if not key:
        log.error("ODDS_API_KEY not set."); return 1

    # 1) upcoming events (free — no credit cost)
    r, rem, used = _get(f"{BASE}/events", {"apiKey": key})
    if r.status_code != 200:
        log.error("events call failed %s: %s", r.status_code, r.text[:300])
        return 1
    events = r.json()
    log.info("MLB upcoming events: %d (credits remaining=%s used=%s)",
             len(events), rem, used)
    if not events:
        log.warning("No upcoming MLB events to probe."); return 0
    ev = events[0]
    eid = ev["id"]
    log.info("Probing event %s: %s @ %s (%s)", eid,
             ev.get("away_team"), ev.get("home_team"), ev.get("commence_time"))

    # 2) baseline: does the key work for standard markets on the event endpoint?
    r, rem, used = _get(f"{BASE}/events/{eid}/odds",
                        {"apiKey": key, "regions": "us", "markets": "totals",
                         "oddsFormat": "decimal"})
    log.info("baseline totals -> HTTP %s (remaining=%s)", r.status_code, rem)
    if r.status_code == 200:
        bk = r.json().get("bookmakers", [])
        log.info("  books quoting game totals: %d", len(bk))

    # 3) the real question: first-inning markets
    found = False
    for mkt in FIRST_INNING_MARKETS:
        r, rem, used = _get(f"{BASE}/events/{eid}/odds",
                            {"apiKey": key, "regions": "us", "markets": mkt,
                             "oddsFormat": "decimal"})
        if r.status_code == 200:
            books = r.json().get("bookmakers", [])
            quotes = sum(len(m.get("outcomes", []))
                         for b in books for m in b.get("markets", []))
            log.info("✓ %s -> HTTP 200, %d book(s), %d outcome(s) (remaining=%s)",
                     mkt, len(books), quotes, rem)
            if books:
                found = True
                sample = next((m for b in books for m in b.get("markets", [])),
                              None)
                if sample:
                    log.info("   sample: %s", str(sample)[:400])
        else:
            log.info("✗ %s -> HTTP %s: %s", mkt, r.status_code, r.text[:200])

    log.info("=" * 60)
    if found:
        log.info("RESULT: first-inning odds ARE available on this key — the "
                 "sharp-vs-Kalshi NRFI model is buildable.")
    else:
        log.info("RESULT: NO first-inning odds on this key/plan. The edge-based "
                 "NRFI build needs a paid tier or a different data source.")
    log.info("Credits remaining: %s", rem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
