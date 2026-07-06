"""World Cup probe round 3: KXFIFAGAME market structure.

KXFIFAGAME is Kalshi's live per-game World Cup series. Dump its open
events WITH nested markets (tickers, labels, prices, strike fields) so
the smart-money mapper can be built against the real shape — especially
how draws are represented, which decides whether Polymarket match-winner
and/or team-to-advance markets map safely. Read-only, no keys.
"""

import json
import time
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEEP = ("ticker", "yes_sub_title", "subtitle", "title", "yes_ask", "yes_bid",
        "status", "floor_strike", "cap_strike", "expected_expiration_time")


def main() -> int:
    out = {}
    # no status filter: round 3 showed zero open/unopened events, yet an
    # unfiltered call sees them — read the shape off recent (closed) games
    for status in ("any",):
        time.sleep(2)   # round 2 saw 429s — be polite
        resp = requests.get(
            f"{BASE}/events",
            params={"series_ticker": "KXFIFAGAME",
                    "with_nested_markets": "true", "limit": 15},
            timeout=30, headers=HEADERS)
        body = resp.json() if resp.status_code == 200 else resp.text[:300]
        if isinstance(body, dict):
            for ev in body.get("events", []):
                ev["markets"] = [
                    {k: m.get(k) for k in KEEP if k in m}
                    for m in (ev.get("markets") or [])]
                for k in list(ev):
                    if k not in ("event_ticker", "title", "sub_title",
                                 "markets", "mutually_exclusive"):
                        ev.pop(k, None)
        out[status] = {"status": resp.status_code, "body": body}

    OUT.mkdir(exist_ok=True)
    (OUT / "wc_probe.json").write_text(json.dumps(out, indent=2)[:60000])
    print("done")
    return 0


if __name__ == "__main__":
    main()
