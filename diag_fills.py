"""Diagnostic: what do Kalshi's portfolio APIs actually return right now?

The reconciler got zero buy fills for the last 7 days even though the
balance moved ~$10 today with no open positions. Print counts and samples
from fills/settlements/orders so we can see which assumption is wrong
(param name, response shape, action/side casing, or the orders never
filling). Read-only; runs on CI where the credentials live.
"""

import json
import time

from config import load_kalshi_settings
from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("diag_fills")


def show(name, rows, keys):
    print(f"== {name}: {len(rows)} row(s)")
    for r in rows[:5]:
        print("  ", json.dumps({k: r.get(k) for k in keys if k in r}))


def main() -> int:
    setup_logging()
    s = load_kalshi_settings(require_market=False)
    client = KalshiClient(s.kalshi_api_key_id, s.kalshi_private_key_path,
                          s.kalshi_env)
    print("balance_cents:", client.get_balance_cents())

    week_ago = int(time.time() - 7 * 86400)
    fills_7d = client.get_fills(week_ago)
    print("== RAW first buy fill and first sell fill (all keys):")
    for want in ("buy", "sell"):
        for f in fills_7d:
            if (f.get("action") or "").lower() == want:
                print(json.dumps(f, indent=1, default=str))
                break

    setts = client.get_settlements(week_ago)
    show("settlements(7d)", setts,
         ["ticker", "market_result", "yes_count", "no_count", "revenue",
          "settled_time"])

    try:
        orders = client.get_resting_orders()
        show("resting_orders", orders,
             ["ticker", "action", "side", "status", "remaining_count",
              "created_time", "order_id"])
    except Exception as exc:
        print("resting orders failed:", exc)
    return 0


if __name__ == "__main__":
    main()
