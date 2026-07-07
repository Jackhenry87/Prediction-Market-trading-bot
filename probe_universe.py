"""Probe: the full placeable universe for the copier.

A. Kalshi: every open series whose events look like head-to-head games
   ('X vs Y' titles) or binary questions, with market structure samples —
   the inventory for a GENERIC discovery mapper (all sports, not
   hand-coded ones).
B. Polymarket: what the sharps actually trade, bucketed by slug prefix,
   so expansion effort follows their money.
Read-only, no keys. Results committed back by CI. Delete after.
"""

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
HEADERS = {"User-Agent": "Mozilla/5.0"}
BASE = "https://api.elections.kalshi.com/trade-api/v2"
VS_RE = re.compile(r"\bvs\.?\b", re.I)


def main() -> int:
    out = {}

    # A. Kalshi: series whose open events have "vs" titles
    vs_series = defaultdict(list)
    cursor = ""
    for _ in range(15):
        time.sleep(1.2)
        params = dict(status="open", limit=200, with_nested_markets="true")
        if cursor:
            params["cursor"] = cursor
        r = requests.get(f"{BASE}/events", params=params, timeout=30,
                         headers=HEADERS)
        if r.status_code != 200:
            out.setdefault("errors", []).append(r.status_code)
            break
        data = r.json()
        for ev in data.get("events", []):
            title = ev.get("title") or ""
            if VS_RE.search(title):
                s = ev.get("series_ticker") or ""
                if len(vs_series[s]) < 2:
                    mks = [{
                        "ticker": m.get("ticker"),
                        "sub": m.get("yes_sub_title"),
                        "ask": m.get("yes_ask"),
                    } for m in (ev.get("markets") or [])[:4]]
                    vs_series[s].append({
                        "event": ev.get("event_ticker"),
                        "title": title[:60], "markets": mks})
                else:
                    vs_series[s].append(None)  # just count
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    out["kalshi_vs_series"] = {
        s: {"open_events": len(v),
            "samples": [x for x in v if x][:2]}
        for s, v in sorted(vs_series.items(),
                           key=lambda kv: -len(kv[1]))}

    # B. Polymarket: sharp-relevant slug prefixes on the live tape
    prefix = Counter()
    samples = {}
    for page in range(6):
        try:
            trades = requests.get(
                "https://data-api.polymarket.com/trades",
                params={"limit": 500, "offset": page * 500,
                        "filterType": "CASH", "filterAmount": 100},
                timeout=30, headers=HEADERS).json()
        except Exception as exc:
            out.setdefault("tape_errors", []).append(str(exc))
            continue
        for tr in trades:
            slug = tr.get("slug") or ""
            p = slug.split("-")[0] if slug else "?"
            prefix[p] += 1
            samples.setdefault(p, {"slug": slug[:70],
                                   "title": (tr.get("title") or "")[:70]})
    out["poly_prefixes"] = [
        {"prefix": p, "trades": n, **samples.get(p, {})}
        for p, n in prefix.most_common(30)]

    OUT.mkdir(exist_ok=True)
    (OUT / "universe_probe.json").write_text(
        json.dumps(out, indent=2)[:70000])
    print("kalshi vs-series:", len(vs_series),
          "| poly prefixes:", len(prefix))
    return 0


if __name__ == "__main__":
    main()
