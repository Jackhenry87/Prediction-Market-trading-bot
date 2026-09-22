"""Scores the in-play win-expectancy recorder against real settlements.

    python live_score.py

WHY THIS EXISTS
---------------
live_scan.py has been recording model-vs-market comparisons since 2026-08-10 and
its docstring promised the pass that would judge them:

    "Does our edge, when we claim one, predict the price MOVING our way? The
    last question is the one that matters... A follow-up pass can score whether
    the price moved toward the model."

That pass was never written, so 1,804 comparisons accumulated over six weeks
without anyone knowing whether the model was any good. live_scan.csv carries no
result column, which made the recording unscoreable by its own design — the same
defect that left the sports CLV file unable to test the claims made for it.

This closes it. Every row carries a ticker, and settled markets carry a result,
so the exchange supplies the ground truth: what one contract taken at the
recorded price would actually have paid, taker fee included.

WHAT IT FOUND (2026-09-22, 1,726 of 1,804 comparisons settled)
--------------------------------------------------------------
    group                        n   mean c/contract     ROI
    ALL comparisons           1726        -2.10        -4.1%
    model edge > 0 after fees  685        -3.89        -8.2%
    tradeable (>=7c gate)      220        -7.13       -16.1%
    edge >= 10c                170        -8.33       -20.9%
    edge >= 15c                 82       -14.72       -41.7%

    Brier score:  model 0.2259   market 0.2037

The loss is MONOTONIC in the claimed edge. That is the finding, and it is much
stronger than "unprofitable": if the model carried no information at all the
subsets would all lose about the taker fee. Instead every increase in claimed
edge makes the result worse, which means the edge IS the error — the model's
disagreement with the price measures how wrong the model is, not how wrong the
price is. The Brier scores say the same thing directly: the market is the better
forecaster, so the 7c gate is not a filter for opportunity, it is a filter that
selects the model's largest mistakes.

This is the third time this project has found the same shape. SPORTS_TOTAL_SIGMA
manufactured edges that grew with distance from the book line; full Kelly then
sized them in proportion to the error. The in-play model is that pattern again,
caught this time before any money moved — which is the entire reason live_scan
was built as a recorder with no order path.

CONSEQUENCE
-----------
Do not wire strategy_live to an order path. Not at a higher gate either: the
higher the gate, the worse the measured result. Anyone revisiting this should
re-run the scorer first and needs to explain a NEGATIVE monotone curve before
proposing to trade against it.
"""

import csv
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from kalshi_client import KalshiClient
from trade_logger import get_logger, setup_logging

log = get_logger("live_score")

ROOT = Path(__file__).resolve().parent
SCAN_LOG = ROOT / "live_scan.csv"
SCOREBOARD = ROOT / "LIVE_SCOREBOARD.md"
RESULT_CACHE = ROOT / "live_results.json"

# Settlement lookups are one request per market and markets never un-settle, so
# a resolved outcome is cached forever. Only the unresolved ones are re-fetched.
WORKERS = int(os.getenv("LIVE_SCORE_WORKERS", "8"))

# The buckets the decision actually turns on. A model with real edge gets BETTER
# as the threshold rises; one whose edge is error gets worse. Reporting the
# gradient is the point — a single pooled number hides the sign of the slope.
BUCKETS = (
    ("all comparisons", lambda e: True),
    ("edge > 0 after fees", lambda e: e > 0),
    ("edge >= 5c", lambda e: e >= 5),
    ("edge >= 7c (trade gate)", lambda e: e >= 7),
    ("edge >= 10c", lambda e: e >= 10),
    ("edge >= 15c", lambda e: e >= 15),
)


def taker_fee_cents(price_cents: float) -> float:
    """Kalshi's per-contract taker fee: 0.07 * p * (1-p), in cents."""
    p = price_cents / 100.0
    return 0.07 * p * (1.0 - p) * 100.0


def load_results(path: Path = None) -> dict:
    """Cached {ticker: 'yes'|'no'|''} from previous runs."""
    path = path or RESULT_CACHE
    if not path.exists():
        return {}
    try:
        import json
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_results(results: dict, path: Path = None) -> None:
    import json
    path = path or RESULT_CACHE
    with open(path, "w") as fh:
        json.dump(results, fh)


def fetch_results(client, tickers, cached: dict = None) -> dict:
    """Resolve each ticker's settlement, reusing anything already known.

    Fails soft per ticker: an unreachable or still-open market stays unscored
    rather than being counted as a loss, because a missing outcome and a losing
    one are not the same thing and pooling them would bias the verdict.
    """
    out = dict(cached or {})
    todo = [t for t in tickers if not out.get(t)]
    if not todo:
        return out

    def one(ticker):
        try:
            market = client._request("GET", f"/markets/{ticker}")["market"]
            return ticker, (market.get("result") or "")
        except Exception:
            return ticker, ""

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for ticker, result in pool.map(one, todo):
            out[ticker] = result
    return out


def pnl_cents(price_cents: float, result: str) -> float:
    """Cents per contract from TAKING the yes side at this price, net of fee."""
    gross = (100.0 - price_cents) if result == "yes" else -price_cents
    return gross - taker_fee_cents(price_cents)


def score(rows: list, results: dict) -> list:
    """One summary dict per bucket. Rows whose market has not settled are
    excluded, never counted as losses."""
    scored = []
    for r in rows:
        outcome = results.get(r.get("ticker", ""), "")
        if outcome not in ("yes", "no"):
            continue
        try:
            price = float(r["price_cents"])
            edge = float(r["edge_cents"])
            prob = float(r["model_prob"])
        except (KeyError, TypeError, ValueError):
            continue
        hit = 1.0 if outcome == "yes" else 0.0
        scored.append(dict(edge=edge, price=price,
                           pnl=pnl_cents(price, outcome),
                           brier_model=(prob - hit) ** 2,
                           brier_market=(price / 100.0 - hit) ** 2))
    out = []
    for label, keep in BUCKETS:
        sub = [s for s in scored if keep(s["edge"])]
        if not sub:
            out.append(dict(label=label, n=0))
            continue
        pnls = [s["pnl"] for s in sub]
        staked = sum(s["price"] for s in sub)
        out.append(dict(
            label=label, n=len(sub), mean_cents=statistics.mean(pnls),
            total_usd=sum(pnls) / 100.0,
            win_pct=100.0 * sum(1 for x in pnls if x > 0) / len(pnls),
            roi_pct=(100.0 * sum(pnls) / staked) if staked else 0.0,
            brier_model=statistics.mean(s["brier_model"] for s in sub),
            brier_market=statistics.mean(s["brier_market"] for s in sub)))
    return out


def verdict(buckets: list) -> str:
    """The one sentence a reader needs. A model with edge improves as the gate
    rises; one whose edge is error degrades. Report which, and say so plainly."""
    graded = [b for b in buckets if b.get("n")]
    if len(graded) < 2:
        return "Not enough settled comparisons to judge."
    first, last = graded[0]["mean_cents"], graded[-1]["mean_cents"]
    base = graded[0]
    better = base["brier_model"] < base["brier_market"]
    if last < first:
        return ("**The claimed edge is anti-predictive.** Results get WORSE as "
                "the edge threshold rises, so the model's disagreement with the "
                "price measures the model's error, not the market's. "
                f"The {'model' if better else 'market'} is the better "
                "calibrated forecaster. Do not trade this.")
    if last > 0:
        return ("Results improve with the edge threshold and the strongest "
                "bucket is profitable after fees. Worth a small live test.")
    return ("Results improve with the edge threshold but the strongest bucket "
            "still loses after fees. Keep recording; do not trade.")


def render(buckets: list, unresolved: int = 0) -> str:
    lines = ["# In-play win-expectancy scoreboard", "",
             "What one contract TAKEN at the recorded price would actually "
             "have paid, taker fee included. Settled markets only.", "",
             "| bucket | n | mean c/contract | total $ | win % | ROI |",
             "|---|---:|---:|---:|---:|---:|"]
    for b in buckets:
        if not b.get("n"):
            lines.append(f"| {b['label']} | 0 | — | — | — | — |")
            continue
        lines.append(f"| {b['label']} | {b['n']} | {b['mean_cents']:+.2f} | "
                     f"{b['total_usd']:+.2f} | {b['win_pct']:.0f}% | "
                     f"{b['roi_pct']:+.1f}% |")
    graded = [b for b in buckets if b.get("n")]
    if graded:
        b = graded[0]
        lines += ["", f"Brier score (lower is better) — model "
                      f"{b['brier_model']:.4f} vs market "
                      f"{b['brier_market']:.4f}."]
    if unresolved:
        lines.append(f"\n{unresolved} comparison(s) not yet settled and "
                     f"excluded rather than counted as losses.")
    lines += ["", verdict(buckets), ""]
    return "\n".join(lines)


def main() -> int:
    setup_logging()
    if not SCAN_LOG.exists():
        log.info("No live_scan.csv yet — nothing to score.")
        return 0
    with open(SCAN_LOG, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        log.info("live_scan.csv is empty — nothing to score.")
        return 0
    # No credentials: scoring reads public market data and must not be able to
    # place an order even by accident, exactly like the recorder it grades.
    client = KalshiClient(env=os.getenv("KALSHI_ENV", "prod"))
    tickers = sorted({r.get("ticker", "") for r in rows if r.get("ticker")})
    try:
        results = fetch_results(client, tickers, load_results())
    except Exception as exc:
        log.error("Settlement lookup failed: %s", exc)
        return 1
    try:
        save_results(results)
    except OSError as exc:
        log.warning("Could not cache settlements: %s", exc)

    buckets = score(rows, results)
    unresolved = sum(1 for r in rows
                     if results.get(r.get("ticker", ""), "") not in ("yes", "no"))
    report = render(buckets, unresolved)
    try:
        SCOREBOARD.write_text(report)
    except OSError as exc:
        log.warning("Could not write scoreboard: %s", exc)
    for b in buckets:
        if b.get("n"):
            log.info("%-26s n=%4d  mean %+6.2fc  ROI %+6.1f%%",
                     b["label"], b["n"], b["mean_cents"], b["roi_pct"])
    log.info("%s", verdict(buckets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
