"""One-shot schema probe of Polymarket's PUBLIC read-only APIs (round 2).

Round 1 found: data-api /trades and gamma /markets are open; the lb-api
/leaderboard path 404s. This round probes per-wallet endpoints (using a
real wallet pulled live from the trade tape), leaderboard path variants,
the per-user PnL API that powers profile charts, and /trades pagination —
everything needed to either use a leaderboard or build our own from the
tape. Read-only, no keys, no orders.
"""

import json
from pathlib import Path

import requests

OUT = Path(__file__).resolve().parent / "probe_results"
TRUNC = 1500
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(url: str) -> dict:
    try:
        resp = requests.get(url, timeout=30, headers=HEADERS)
        return dict(status=resp.status_code, body=resp.text[:TRUNC], url=url)
    except Exception as exc:
        return dict(status=None, error=str(exc), url=url)


def main() -> int:
    results = {}

    # a real, currently-active wallet straight from the public tape
    tape = fetch("https://data-api.polymarket.com/trades?limit=1")
    results["tape_head"] = tape
    wallet = ""
    try:
        wallet = json.loads(tape["body"])[0]["proxyWallet"]
    except Exception:
        pass

    candidates = [
        # leaderboard path variants
        ("lb_rankings", "https://lb-api.polymarket.com/rankings?window=1w&limit=3"),
        ("lb_profit", "https://lb-api.polymarket.com/profit?window=1w&limit=3"),
        ("lb_plain", "https://lb-api.polymarket.com/leaderboard"),
        ("data_lb_v2", "https://data-api.polymarket.com/v2/leaderboard?window=1w&limit=3"),
        # per-user PnL (powers the profile page chart)
        ("user_pnl",
         f"https://user-pnl-api.polymarket.com/user-pnl?user_address={wallet}&interval=1m&fidelity=1d"),
        # per-wallet reads on data-api
        ("wallet_activity",
         f"https://data-api.polymarket.com/activity?user={wallet}&limit=2"),
        ("wallet_positions",
         f"https://data-api.polymarket.com/positions?user={wallet}&limit=2"),
        ("wallet_trades",
         f"https://data-api.polymarket.com/trades?user={wallet}&limit=2"),
        ("wallet_value",
         f"https://data-api.polymarket.com/value?user={wallet}"),
        # tape pagination + time filtering
        ("tape_offset", "https://data-api.polymarket.com/trades?limit=2&offset=500"),
        ("tape_filter",
         "https://data-api.polymarket.com/trades?limit=2&filterType=CASH&filterAmount=500"),
    ]
    for name, url in candidates:
        results[name] = fetch(url)

    OUT.mkdir(exist_ok=True)
    (OUT / "polymarket_probe.json").write_text(json.dumps(results, indent=2))
    print(json.dumps({k: v.get("status") for k, v in results.items()},
                     indent=2))
    return 0


if __name__ == "__main__":
    main()
