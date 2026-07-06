"""One-shot probe: World Cup market shapes on Polymarket vs Kalshi.

Goal: extend the smart-money mapper beyond US moneylines to the World Cup,
where the sharp flow is concentrated right now. Samples the public tape
for fifwc-* slugs (their taxonomy) and pulls Kalshi's KXMENWORLDCUP open
events (market structure/labels). Read-only, no keys; results committed
back to the branch by CI. Delete after the mapper ships.
"""

import json
from collections import Counter
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def main() -> int:
    results = {}

    # 1. Polymarket slug taxonomy: sample the recent tape, bucket fifwc slugs
    slugs = Counter()
    samples = {}
    for page in range(4):
        try:
            trades = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"limit": 500, "offset": page * 500},
                timeout=30, headers=HEADERS).json()
        except Exception as exc:
            results.setdefault("tape_errors", []).append(str(exc))
            continue
        for tr in trades:
            slug = tr.get("slug", "")
            if slug.startswith("fifwc"):
                slugs[slug] += 1
                samples.setdefault(slug, {
                    "title": tr.get("title"),
                    "outcome": tr.get("outcome"),
                    "price": tr.get("price")})
    results["fifwc_slugs"] = [
        {"slug": s, "trades": n, **samples[s]}
        for s, n in slugs.most_common(40)]

    # 2. Kalshi World Cup structure (public market data, no auth for GETs)
    for series in ("KXMENWORLDCUP", "KXMENWORLDCUPGAME", "KXWORLDCUP"):
        try:
            resp = requests.get(
                "https://api.elections.kalshi.com/trade-api/v2/events",
                params={"series_ticker": series, "status": "open",
                        "with_nested_markets": "true", "limit": 5},
                timeout=30, headers=HEADERS)
            body = resp.json() if resp.status_code == 200 else resp.text[:300]
            if isinstance(body, dict):
                for ev in body.get("events", []):
                    for mk in ev.get("markets", [])[:3]:
                        for k in list(mk):
                            if k not in ("ticker", "yes_sub_title", "subtitle",
                                         "title", "yes_ask", "yes_bid",
                                         "status", "floor_strike",
                                         "cap_strike"):
                                mk.pop(k, None)
                    ev["markets"] = (ev.get("markets") or [])[:3]
            results[f"kalshi_{series}"] = {"status": resp.status_code,
                                           "body": body}
        except Exception as exc:
            results[f"kalshi_{series}"] = {"error": str(exc)}

    OUT.mkdir(exist_ok=True)
    (OUT / "wc_probe.json").write_text(json.dumps(results, indent=2)[:60000])
    print("slugs found:", len(results.get("fifwc_slugs", [])))
    return 0


if __name__ == "__main__":
    main()
