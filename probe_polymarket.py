"""One-shot schema probe of Polymarket's PUBLIC read-only APIs.

Run from CI (sandbox networks block polymarket.com). Hits the leaderboard,
data, and gamma endpoints that power the site's public profile/leaderboard
pages, and writes truncated samples to probe_results/polymarket_probe.json
so the smart-money tracker can be built against real response shapes.
Read-only, no keys, no orders.
"""

import json
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
TRUNC = 2000

CANDIDATES = [
    ("leaderboard_1w",
     "https://lb-api.polymarket.com/leaderboard?window=1w&limit=5"),
    ("leaderboard_1m_profit",
     "https://lb-api.polymarket.com/leaderboard?window=1m&rankType=profit&limit=5"),
    ("data_leaderboard",
     "https://data-api.polymarket.com/leaderboard?window=1w&limit=5"),
    ("data_trades",
     "https://data-api.polymarket.com/trades?limit=3"),
    ("gamma_markets",
     "https://gamma-api.polymarket.com/markets?limit=1&closed=false"),
]


def fetch(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=30,
                            headers={"User-Agent": "Mozilla/5.0"})
        body = resp.text[:TRUNC]
        return dict(status=resp.status_code, body=body)
    except Exception as exc:
        return dict(status=None, error=str(exc))


def main() -> int:
    results = {name: fetch(url) | {"url": url} for name, url in CANDIDATES}

    # follow-up: if any leaderboard variant returned wallets, probe the
    # per-wallet endpoints with a real address
    wallet = None
    for name in ("leaderboard_1w", "leaderboard_1m_profit",
                 "data_leaderboard"):
        try:
            data = json.loads(results[name]["body"])
            first = data[0] if isinstance(data, list) else None
            wallet = (first or {}).get("proxyWallet") or (first or {}).get(
                "wallet") or (first or {}).get("address")
            if wallet:
                break
        except Exception:
            continue
    if wallet:
        for name, url in [
            ("wallet_activity",
             f"https://data-api.polymarket.com/activity?user={wallet}&limit=3"),
            ("wallet_positions",
             f"https://data-api.polymarket.com/positions?user={wallet}&limit=3"),
            ("wallet_trades",
             f"https://data-api.polymarket.com/trades?user={wallet}&limit=3"),
        ]:
            results[name] = fetch(url) | {"url": url}

    OUT.mkdir(exist_ok=True)
    (OUT / "polymarket_probe.json").write_text(json.dumps(results, indent=2))
    print(json.dumps({k: v.get("status") for k, v in results.items()},
                     indent=2))
    return 0


if __name__ == "__main__":
    main()
