"""Smart-money model: follow Polymarket's consistently profitable wallets.

Polymarket's public read-only APIs expose the full trade tape, every
wallet's positions, and each wallet's daily PnL curve (the profile chart).
Polymarket geoblocks US order placement, so we use it purely as a SIGNAL
source and EXECUTE on Kalshi, where the same games trade as moneylines.

Pipeline per run:
  1. Sample the tape for recent big-money trades (public, no key) and
     rank the wallets behind them by their measured 2-WEEK PnL — profits
     over both the fortnight and the last week, so one lucky bet doesn't
     qualify. Top SHARP_N survivors are "sharp".
  2. Collect each sharp wallet's fresh BUYs (last CONS_WINDOW_H hours).
     Only a market+outcome bought by >= MIN_WALLETS DISTINCT sharps with
     real stakes counts — one whale is noise, several sharps agreeing is
     the signal.
  3. Map consensus onto Kalshi: big-league moneylines (MLB/NBA/NFL/NHL/
     WNBA -> KX*GAME series) and World Cup regulation match-winner/draw
     markets (fifwc slugs -> KXFIFAGAME). Our fair prob is the sharps'
     stake-weighted entry plus a small premium, so EV math refuses to
     chase a line that has already run away from their entry.

Non-mappable consensus (crypto 15-minute markets, totals/props/exact
scores, team-to-advance — a different bet than regulation winner — and
venues without a Kalshi twin) is logged for visibility and skipped.

    python strategy_smartmoney.py    # read-only scan, no orders
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from strategy_sports import SERIES, _words
from strategy_weather import (_close_cents, price_cents,
                              score_pending_paper_trades, taker_fee_cents)
from trade_logger import get_logger, setup_logging

log = get_logger("strategy_smartmoney")

TAPE_URL = "https://data-api.polymarket.com/trades"
PNL_URL = "https://user-pnl-api.polymarket.com/user-pnl"
PAPER_LOG = Path(__file__).resolve().parent / "paper_trades_smartmoney.csv"
# who drove each copy + how their picks settled -> wallets whose copied
# picks LOSE get blacklisted from the sharp set (follow winners only)
WALLET_LOG = Path(__file__).resolve().parent / "smartmoney_wallets_log.csv"
BLACKLIST_PATH = Path(__file__).resolve().parent / "smartmoney_blacklist.json"
# a wallet sharp in politics is usually noise in tennis: bar a wallet from
# a CATEGORY where its copied picks have lost, without blacklisting it
# everywhere. Keyed "wallet|category".
CAT_BARS_PATH = Path(__file__).resolve().parent / "smartmoney_category_bars.json"
WALLET_LOG_COLUMNS = ["ts", "ticker", "side", "price_cents", "wallet",
                      "outcome"]
BLACKLIST_MIN_SETTLED = int(os.getenv("SM_BLACKLIST_MIN", "4"))
CAT_BLACKLIST_MIN = int(os.getenv("SM_CAT_BLACKLIST_MIN", "4"))
# hours after entry before we sample the market price to score CLV — long
# enough for the line to react, short enough to read before settlement
CLV_LAG_H = float(os.getenv("SM_CLV_LAG_H", "3"))


def category_of(ticker: str) -> str:
    """Coarse bucket for a Kalshi ticker, so per-wallet edge is judged
    within a domain rather than pooled across unrelated sports."""
    t = (ticker or "").upper()
    if t.startswith(("KXMLBGAME", "KXNBA", "KXNFL", "KXNHL", "KXWNBA")):
        return "usleague"
    if t.startswith(("KXATPMATCH", "KXWTAMATCH")):
        return "tennis"
    if t.startswith(("KXFIFAGAME", "KXMENWORLDCUP", "KXWOMENWORLDCUP")):
        return "soccer"
    return "other"          # politics + discovered head-to-heads


def load_category_bars() -> set:
    try:
        import json
        return set(json.loads(CAT_BARS_PATH.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def category_allowed(wallet: str, category: str, bars: set = None) -> bool:
    bars = load_category_bars() if bars is None else bars
    return f"{wallet}|{category}" not in bars

# --- sharp-wallet discovery ---
TAPE_MIN_USDC = float(os.getenv("SM_TAPE_MIN_USDC", "250"))   # big trades only
TAPE_PAGES = int(os.getenv("SM_TAPE_PAGES", "3"))             # x500 trades
CANDIDATES_MAX = int(os.getenv("SM_CANDIDATES_MAX", "40"))    # PnL lookups cap
SHARP_MIN_PNL_2W = float(os.getenv("SM_MIN_PNL_2W", "500"))   # $ over 2 weeks
SHARP_N = int(os.getenv("SM_SHARP_N", "15"))                  # wallets tracked
# QUALITY over size: rank sharps by RISK-ADJUSTED return (steady earners),
# not raw dollar PnL (which just finds whales turning over size at a
# coin-flip win rate), and reject wallets whose whole month is one lucky
# day. All measured from the same 1-month PnL curve we already fetch.
MIN_UPDAY_FRAC = float(os.getenv("SM_MIN_UPDAY_FRAC", "0.45"))  # up-days share
MAX_DAY_SHARE = float(os.getenv("SM_MAX_DAY_SHARE", "0.80"))    # no 1-day spike
MIN_CURVE_PTS = int(os.getenv("SM_MIN_CURVE_PTS", "6"))         # judge shape

# --- consensus rule (the point of the model) ---
CONS_WINDOW_H = float(os.getenv("SM_CONS_WINDOW_H", "36"))
MIN_WALLETS = int(os.getenv("SM_MIN_WALLETS", "3"))           # distinct sharps
MIN_STAKE_USDC = float(os.getenv("SM_MIN_STAKE", "50"))       # per-sharp stake

# --- pricing ---
# Sharps demand an edge, so fair value sits above their entry: prob =
# entry + PREMIUM. With MIN_EDGE below, the net effect is "take the trade
# only within ~PREMIUM-MIN_EDGE-fee cents of the sharps' entry" — copy
# them, never chase a line that already ran.
PREMIUM_PTS = float(os.getenv("SM_PREMIUM_PTS", "8"))
MAX_PROB = 0.97
MIN_EDGE_CENTS = float(os.getenv("SM_MIN_EDGE_CENTS", "3"))
# This model is EXEMPT from the global 60-90c band (that band exists
# because OUR models are least calibrated at extreme prices; on copies
# the calibration is the sharps' own track record). Its own floor still
# refuses longshot lottery tickets.
SM_MIN_PRICE_CENTS = float(os.getenv("SM_MIN_PRICE", "25"))

MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
# plain moneyline slugs look like mlb-phi-kc-2026-07-06; spread/total/prop
# slugs carry suffixes (-spread-away-1pt5, ...) and are rejected
MONEYLINE_RE = re.compile(
    r"^(mlb|nba|nfl|nhl|wnba)-[a-z0-9]+-[a-z0-9]+-(\d{4})-(\d{2})-(\d{2})$")
LEAGUE_SERIES = {c["sport"].split("_")[-1]: c["series"] for c in SERIES}
LEAGUE_SERIES.update({"mlb": "KXMLBGAME", "nba": "KXNBA", "nfl": "KXNFLGAME",
                      "nhl": "KXNHLGAME", "wnba": "KXWNBA"})

# World Cup: Polymarket fifwc-prt-esp-2026-07-06-esp is the regulation
# match-winner ("Will Spain win on ...?") and -draw the tie. Kalshi's game
# series uses the SAME FIFA trigrams in its event tickers
# (KXFIFAGAME-26JUL06PRTESP, probe-verified). team-to-advance is a
# DIFFERENT bet (draw -> penalties still advances someone) with no
# verified Kalshi twin, so it is deliberately NOT mapped; likewise all
# totals/exact-score/player props.
WC_SERIES = "KXFIFAGAME"
WC_RE = re.compile(
    r"^fifwc-([a-z]{3})-([a-z]{3})-(\d{4})-(\d{2})-(\d{2})-([a-z]{3}|draw)$")

# Team-to-advance — the sharps' favorite knockout bet. Kalshi's venue for
# tournament games isn't a fixed series (KXFIFAGAME held only qualifiers),
# so advance consensus is mapped by DISCOVERY: search all open events for
# the two team names, then let the market STRUCTURE decide the semantics.
# A knockout game listed with exactly two team markets and no TIE must
# settle on who advances (a 2-outcome market can't leave a draw
# unresolved) -> safe to place. A TIE market present means regulation
# settlement -> a DIFFERENT bet than the sharps made -> refused.
WC_ADV_RE = re.compile(
    r"^fifwc-[a-z]{3}-[a-z]{3}-\d{4}-\d{2}-\d{2}-team-to-advance$")

# Tennis: binary by nature (no draws), so Polymarket match winners map
# 1:1 onto Kalshi's per-match series. Routed by TITLE (Polymarket tennis
# slugs don't follow the team-sport pattern):
#   "Wimbledon ATP: Jiri Lehecka vs Alexander Zverev" -> KXATPMATCH
TENNIS_TITLE_RE = re.compile(
    r"\b(ATP|WTA)\b[^:]*:\s*(.+?)\s+vs\.?\s+(.+?)\s*$", re.I)
TENNIS_SERIES = {"ATP": "KXATPMATCH", "WTA": "KXWTAMATCH"}


def _surname_words(name: str) -> set:
    """Distinctive words of a player/team name for matching (>=3 chars)."""
    return {w for w in re.split(r"[^A-Za-z]+", (name or "").upper())
            if len(w) >= 3}


def _vs_teams(title: str) -> tuple:
    """('United States', 'Belgium') from 'United States vs. Belgium: ...'"""
    head = (title or "").split(":")[0]
    parts = re.split(r"\s+vs\.?\s+", head, flags=re.I)
    return (parts[0].strip(), parts[1].strip()) if len(parts) == 2 \
        else (None, None)


def _get(url: str, **params) -> list:
    resp = requests.get(url, params=params, timeout=30,
                        headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.json()


def fetch_big_trades() -> list:
    """Recent tape entries above TAPE_MIN_USDC, a few pages deep."""
    out = []
    for page in range(TAPE_PAGES):
        out.extend(_get(TAPE_URL, limit=500, offset=page * 500,
                        filterType="CASH", filterAmount=int(TAPE_MIN_USDC)))
    return out


def fetch_pnl_curve(wallet: str) -> list:
    """[(unix_ts, cumulative_pnl_$), ...] oldest->newest for the last month
    — the same data behind the polymarket.com profile chart."""
    data = _get(PNL_URL, user_address=wallet, interval="1m", fidelity="1d")
    return [(d["t"], d["p"]) for d in data]


def pnl_change(curve: list, days: float, now_ts: float = None) -> float:
    """PnL over the trailing `days`: last point minus the closest point at
    or before now-days. Uses the curve's start when history is shorter."""
    if not curve:
        return 0.0
    cutoff = (now_ts or datetime.now(timezone.utc).timestamp()) - days * 86400
    base = curve[0][1]
    for t, p in curve:
        if t <= cutoff:
            base = p
        else:
            break
    return curve[-1][1] - base


def _daily_deltas(curve: list, days: float = 30,
                  now_ts: float = None) -> list:
    """Day-over-day PnL changes within the trailing window, using the last
    point before the window as the baseline so the first change is real."""
    if not curve:
        return []
    cutoff = (now_ts or datetime.now(timezone.utc).timestamp()) - days * 86400
    base, inwin = None, []
    for t, p in curve:
        if t < cutoff:
            base = p
        else:
            inwin.append(p)
    seq = ([base] if base is not None else []) + inwin
    return [seq[i] - seq[i - 1] for i in range(1, len(seq))]


def curve_quality(curve: list, now_ts: float = None) -> dict:
    """Shape metrics that separate skill from luck: what fraction of days
    are up, how concentrated the gains are in a single day, and a
    Sharpe-like risk-adjusted return (mean daily PnL / its volatility)."""
    deltas = _daily_deltas(curve, 30, now_ts)
    n = len(deltas)
    gains = [d for d in deltas if d > 0]
    up_frac = (sum(1 for d in deltas if d >= 0) / n) if n else 0.0
    max_share = (max(gains) / sum(gains)) if gains else 1.0
    if n >= 2:
        mean = sum(deltas) / n
        std = (sum((d - mean) ** 2 for d in deltas) / n) ** 0.5
        sharpe = mean / (std + 1e-6)
    else:
        sharpe = 0.0
    return dict(pts=n, up_frac=up_frac, max_share=max_share, sharpe=sharpe)


def select_sharp_wallets() -> dict:
    """wallet -> 2-week PnL for the top wallets behind recent big-money
    flow, RANKED BY RISK-ADJUSTED RETURN (not raw dollars, which just finds
    whales) and filtered for consistency (up over the week, fortnight AND
    month, steady rather than one lucky day)."""
    stake = {}
    for tr in fetch_big_trades():
        w = tr.get("proxyWallet")
        usdc = float(tr.get("size", 0)) * float(tr.get("price", 0))
        if w:
            stake[w] = stake.get(w, 0.0) + usdc
    blacklist = load_blacklist()
    candidates = [w for w in sorted(stake, key=stake.get, reverse=True)
                  if w not in blacklist][:CANDIDATES_MAX]
    if blacklist:
        log.info("Sharp selection excludes %d graded-out wallet(s)",
                 len(blacklist))

    sharp, rank = {}, {}
    for w in candidates:
        try:
            curve = fetch_pnl_curve(w)
        except Exception as exc:
            log.debug("PnL fetch failed for %s (%s)", w, exc)
            continue
        pnl2w = pnl_change(curve, 14)
        pnl1w = pnl_change(curve, 7)
        pnl30 = pnl_change(curve, 30)
        # profitable across the week, fortnight AND month — one lucky
        # streak alone no longer qualifies a wallet
        if not (pnl2w >= SHARP_MIN_PNL_2W and pnl1w > 0 and pnl30 > 0):
            continue
        q = curve_quality(curve)
        # when the curve is long enough to judge, demand a STEADY earner:
        # enough up-days and no single day carrying most of the profit
        if q["pts"] >= MIN_CURVE_PTS and (q["up_frac"] < MIN_UPDAY_FRAC
                                          or q["max_share"] > MAX_DAY_SHARE):
            log.debug("Skip %s: shape up=%.2f 1-day=%.0f%%", w,
                      q["up_frac"], 100 * q["max_share"])
            continue
        sharp[w] = pnl2w
        rank[w] = q["sharpe"]        # risk-adjusted, not size
    top = dict(sorted(sharp.items(), key=lambda kv: -rank[kv[0]])[:SHARP_N])
    log.info("Sharp wallets: %d of %d candidates (ranked by risk-adjusted "
             "return; 2w PnL $%.0f..$%.0f)", len(top), len(candidates),
             min(top.values(), default=0), max(top.values(), default=0))
    return top


def load_blacklist() -> set:
    try:
        import json
        return set(json.loads(BLACKLIST_PATH.read_text()))
    except (FileNotFoundError, ValueError):
        return set()


def log_copy_wallets(ticker: str, side: str, price_cents: float,
                     wallet_ids: list, path: Path = WALLET_LOG) -> None:
    """One row per backing wallet per copy — the attribution that makes
    per-wallet grading possible."""
    import csv
    from datetime import datetime, timezone
    new_file = not path.exists()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(path, "a", newline="") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(WALLET_LOG_COLUMNS)
        for w in wallet_ids or []:
            writer.writerow([now, ticker, side, f"{price_cents:.0f}", w, ""])


def grade_wallets(client, path: Path = WALLET_LOG,
                  bl_path: Path = BLACKLIST_PATH) -> set:
    """Score each backing wallet by how its copied picks settled; wallets
    with >= BLACKLIST_MIN_SETTLED settled copies and NEGATIVE net P&L get
    blacklisted from future sharp selection. Returns the blacklist."""
    import csv
    import json
    if not path.exists():
        return load_blacklist()
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return load_blacklist()
    header, body = rows[0], rows[1:]
    idx = {h: i for i, h in enumerate(header)}
    market_cache, changed = {}, False
    for row in body:
        if row[idx["outcome"]]:
            continue
        ticker = row[idx["ticker"]]
        if ticker not in market_cache:
            try:
                market_cache[ticker] = client.get_market(ticker)
            except Exception:
                market_cache[ticker] = {}
        result = market_cache[ticker].get("result")
        if result not in ("yes", "no"):
            continue
        won = result == row[idx["side"]]
        price = float(row[idx["price_cents"]])
        pnl = (100.0 - price) if won else -price
        row[idx["outcome"]] = f"{'win' if won else 'loss'} ({pnl:+.0f}c)"
        changed = True
    if changed:
        with open(path, "w", newline="") as fh:
            csv.writer(fh).writerows([header] + body)

    stats, cat_stats = {}, {}
    for row in body:
        out = row[idx["outcome"]]
        if not out or "(" not in out:
            continue
        w = row[idx["wallet"]]
        net = float(out.split("(")[1].rstrip("c)"))
        s = stats.setdefault(w, dict(n=0, net=0.0))
        s["n"] += 1
        s["net"] += net
        cat = category_of(row[idx["ticker"]])
        cs = cat_stats.setdefault((w, cat), dict(n=0, net=0.0))
        cs["n"] += 1
        cs["net"] += net
    blacklist = {w for w, s in stats.items()
                 if s["n"] >= BLACKLIST_MIN_SETTLED and s["net"] < 0}
    if blacklist != load_blacklist():
        bl_path.write_text(json.dumps(sorted(blacklist)))
        log.info("Wallet grades updated: %d wallet(s) blacklisted (lost "
                 "money over >=%d settled copies)", len(blacklist),
                 BLACKLIST_MIN_SETTLED)
    # per-category bars: a wallet net-negative in ONE domain is barred from
    # that domain only (it may still be sharp elsewhere)
    cat_bars = {f"{w}|{c}" for (w, c), s in cat_stats.items()
                if s["n"] >= CAT_BLACKLIST_MIN and s["net"] < 0}
    if cat_bars != load_category_bars():
        CAT_BARS_PATH.write_text(json.dumps(sorted(cat_bars)))
        log.info("Category grades updated: %d wallet-category pair(s) barred",
                 len(cat_bars))
    return blacklist


def score_copier_clv(path: Path = PAPER_LOG, now_ts: float = None) -> None:
    """Closing-line value for copies: compare our entry to the Kalshi price
    a few HOURS later while the market is STILL TRADING — the honest test of
    whether the copier is EARLY (line moves our way after we buy, +CLV) or
    late (edge already priced in, ~0/-CLV). Filled once per row and never
    overwritten, so this early reading — not the settlement price — is what
    the scoreboard reports. Rows that settle before the lag elapses are
    left for the outcome scorer."""
    import csv
    from datetime import datetime
    if not path.exists():
        return
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return
    header, body = rows[0], rows[1:]
    if "clv_cents" not in header:
        if "model_prob" not in header:
            return
        header.append("clv_cents")
    for row in body:                      # pad legacy/short rows
        while len(row) < len(header):
            row.append("")
    idx = {name: i for i, name in enumerate(header)}
    now = now_ts or datetime.now(timezone.utc).timestamp()
    from kalshi_client import KalshiClient
    client = KalshiClient(env="prod")
    scored = 0
    for row in body:
        if row[idx["clv_cents"]] or row[idx["outcome"]]:
            continue
        try:
            entered = datetime.fromisoformat(
                row[idx["scanned_at_utc"]]).timestamp()
        except (ValueError, KeyError, IndexError):
            continue
        if now - entered < CLV_LAG_H * 3600:      # give the line time to move
            continue
        try:
            market = client.get_market(row[idx["ticker"]])
        except Exception:
            continue
        if market.get("result") in ("yes", "no"):
            continue                              # settled before we sampled
        close = _close_cents(market)
        if close is None:
            continue
        price = float(row[idx["price_cents"]])
        clv = (close - price) if row[idx["side"]] == "yes" \
            else ((100.0 - close) - price)
        row[idx["clv_cents"]] = f"{clv:+.0f}"
        scored += 1
    if scored:
        with open(path, "w", newline="") as fh:
            csv.writer(fh).writerows([header] + body)
        log.info("Copier CLV: sampled %d fresh copy(ies) (early line read)",
                 scored)


def fetch_wallet_buys(wallet: str, window_h: float,
                      now_ts: float = None) -> list:
    """This wallet's BUY entries within the window, meaningful stakes only."""
    now = now_ts or datetime.now(timezone.utc).timestamp()
    out = []
    try:
        trades = _get(TAPE_URL, user=wallet, limit=100)
    except Exception as exc:
        log.debug("trades fetch failed for %s (%s)", wallet, exc)
        return out
    for tr in trades:
        usdc = float(tr.get("size", 0)) * float(tr.get("price", 0))
        if (tr.get("side") == "BUY"
                and now - float(tr.get("timestamp", 0)) <= window_h * 3600
                and usdc >= MIN_STAKE_USDC):
            out.append(tr)
    return out


def build_consensus(sharp_wallets: dict, now_ts: float = None) -> list:
    """Markets where >= MIN_WALLETS distinct sharps bought the SAME outcome
    recently. Returns [{slug, title, outcome, wallets, stake, avg_price}]."""
    groups = {}
    for w in sharp_wallets:
        for tr in fetch_wallet_buys(w, CONS_WINDOW_H, now_ts):
            key = (tr.get("slug") or tr.get("conditionId") or "",
                   tr.get("outcome") or str(tr.get("outcomeIndex")))
            g = groups.setdefault(key, dict(wallets=set(), stake=0.0,
                                            weighted=0.0,
                                            title=tr.get("title", "")))
            usdc = float(tr.get("size", 0)) * float(tr.get("price", 0))
            g["wallets"].add(w)
            g["stake"] += usdc
            g["weighted"] += usdc * float(tr.get("price", 0))
    out = []
    for (slug, outcome), g in groups.items():
        if len(g["wallets"]) >= MIN_WALLETS and g["stake"] > 0:
            out.append(dict(slug=slug, outcome=outcome, title=g["title"],
                            wallets=len(g["wallets"]), stake=g["stake"],
                            wallet_ids=sorted(g["wallets"]),
                            avg_price=g["weighted"] / g["stake"]))
    out.sort(key=lambda c: -c["stake"])
    return out


def kalshi_date_token(y: str, m: str, d: str) -> str:
    """('2026','07','06') -> '26JUL06' as embedded in Kalshi game tickers."""
    return f"{y[2:]}{MONTHS[int(m) - 1]}{d}"


def _priced_signal(cons: dict, market: dict, event: dict) -> dict:
    """YES signal on this Kalshi market at the sharps' price + premium, or
    None when the ask already ran past their entry (no chasing)."""
    p = min(cons["avg_price"] + PREMIUM_PTS / 100.0, MAX_PROB)
    label = (market.get("yes_sub_title") or market.get("subtitle")
             or market.get("title") or "")
    yes_ask = price_cents(market, "yes_ask")
    if not yes_ask or not 0 < yes_ask < 100:
        return None
    if yes_ask < SM_MIN_PRICE_CENTS:   # own floor: no lottery tickets
        log.info("Below floor: %s ask %.0fc < %.0fc", label, yes_ask,
                 SM_MIN_PRICE_CENTS)
        return None
    ev = 100.0 * p - yes_ask - taker_fee_cents(yes_ask)
    if ev < MIN_EDGE_CENTS:   # line already ran past the sharps
        log.info("No chase: %s ask %.0fc vs sharp entry %.0fc",
                 label, yes_ask, 100 * cons["avg_price"])
        return None
    return dict(side="yes", price_cents=yes_ask, model_prob=p,
                ev_cents=ev, ticker=market.get("ticker"),
                wallets=cons["wallets"], stake=cons["stake"],
                wallet_ids=cons.get("wallet_ids", []),
                subtitle=f"{label} ({cons['wallets']} sharps, "
                         f"${cons['stake']:.0f})",
                event_ticker=event.get("event_ticker")
                or event.get("ticker") or "",
                event_title=event.get("title", ""))


def consensus_signal(cons: dict, events: list) -> dict:
    """Map one US-league moneyline consensus onto a Kalshi game market.
    Returns a signal dict (plus event_ticker/title) or None."""
    m = MONEYLINE_RE.match(cons["slug"])
    if not m:
        return None
    token = kalshi_date_token(m.group(2), m.group(3), m.group(4))
    team_words = _words(cons["outcome"])
    if not team_words:
        return None
    for event in events:
        event_ticker = event.get("event_ticker") or event.get("ticker") or ""
        if token not in event_ticker:
            continue
        for market in event.get("markets") or []:
            if market.get("status") not in (None, "active", "open"):
                continue
            label = (market.get("yes_sub_title") or market.get("subtitle")
                     or market.get("title") or "")
            if team_words <= _words(label):
                return _priced_signal(cons, market, event)
    return None


def wc_signal(cons: dict, events: list) -> dict:
    """Map a World Cup match-winner (or draw) consensus onto the matching
    KXFIFAGAME market. Every uncertainty fails CLOSED: unknown suffix, no
    matching event, or no matching side market -> no trade."""
    m = WC_RE.match(cons["slug"])
    if not m:
        return None
    a, b, suffix = m.group(1), m.group(2), m.group(6)
    if suffix not in (a, b, "draw"):     # a prop that looks like a trigram
        return None
    token = kalshi_date_token(m.group(3), m.group(4), m.group(5))
    tails = (token + a.upper() + b.upper(), token + b.upper() + a.upper())
    want = ("-TIE", "-DRAW") if suffix == "draw" \
        else (f"-{suffix.upper()}",)
    for event in events:
        event_ticker = event.get("event_ticker") or event.get("ticker") or ""
        if not event_ticker.endswith(tails[0]) \
                and not event_ticker.endswith(tails[1]):
            continue
        for market in event.get("markets") or []:
            if market.get("status") not in (None, "active", "open"):
                continue
            if any(str(market.get("ticker", "")).endswith(w) for w in want):
                return _priced_signal(cons, market, event)
    return None


def _match_side_market(event: dict, outcome_words: set):
    """The open market in this event whose label matches the outcome."""
    for market in event.get("markets") or []:
        if market.get("status") not in (None, "active", "open"):
            continue
        label = (market.get("yes_sub_title") or market.get("subtitle")
                 or market.get("title") or "")
        if outcome_words and outcome_words <= _surname_words(label):
            return market
    return None


def tennis_signal(cons: dict, events: list) -> dict:
    """Map a tennis match-winner consensus onto KX{ATP,WTA}MATCH. Both
    surnames must match exactly ONE open event; ambiguity fails closed."""
    m = TENNIS_TITLE_RE.search(cons.get("title") or "")
    if not m:
        return None
    s1 = _surname_words(m.group(2).split()[-1])
    s2 = _surname_words(m.group(3).split()[-1])
    if not s1 or not s2:
        return None
    hits = [ev for ev in events
            if s1 <= _surname_words(ev.get("title"))
            and s2 <= _surname_words(ev.get("title"))]
    if len(hits) != 1:
        return None
    market = _match_side_market(
        hits[0], _surname_words((cons.get("outcome") or "").split()[-1]))
    return _priced_signal(cons, market, hits[0]) if market else None


def _binary_h2h_signal(cons: dict, open_events: list,
                       name_a: str, name_b: str) -> dict:
    """Shared discovery core: map a two-sided consensus onto a DISCOVERED
    binary head-to-head market. Fails closed on: zero or multiple title
    matches, a TIE/DRAW market present (a different bet than a binary
    winner), a non-binary structure, or no matching side market."""
    wa, wb = _surname_words(name_a), _surname_words(name_b)
    if not wa or not wb:
        return None
    hits = []
    for ev in open_events:
        tw = _surname_words(ev.get("title"))
        if wa <= tw and wb <= tw:
            hits.append(ev)
    if len(hits) != 1:
        return None
    event = hits[0]
    markets = [mk for mk in event.get("markets") or []
               if mk.get("status") in (None, "active", "open")]
    for mk in markets:
        blob = (f"{mk.get('ticker', '')} {mk.get('yes_sub_title', '')} "
                f"{mk.get('title', '')}").upper()
        if "TIE" in _surname_words(blob) or "DRAW" in _surname_words(blob):
            log.info("Discovery refused (venue has TIE — different bet): %s",
                     event.get("event_ticker"))
            return None
    if len(markets) != 2:      # not a clean binary head-to-head listing
        return None
    market = _match_side_market(event, _surname_words(cons.get("outcome")))
    return _priced_signal(cons, market, event) if market else None


def advance_signal(cons: dict, open_events: list) -> dict:
    """Team-to-advance consensus -> discovered binary knockout market."""
    team_a, team_b = _vs_teams(cons.get("title"))
    if not team_a or not team_b:
        return None
    return _binary_h2h_signal(cons, open_events, team_a, team_b)


# Any other head-to-head the sharps bet (UFC, golf match play, soccer
# clubs, whatever Kalshi lists as a game): the title must reduce to a
# clean "A vs B" with NO prop markers — set/game/map winners, O/U, spreads
# and segment props are DIFFERENT bets and are rejected before discovery.
_PROP_MARKERS = re.compile(
    r"\bO/U\b|\bset\b|\bgame \d|\bmap\b|\bspread\b|\bhalf\b|1st|2nd|"
    r"\bcorner|\bexact\b|\bscore\b|\bbtts\b|\btotal\b|\(BO\d\)", re.I)


def _generic_vs_names(title: str) -> tuple:
    """('A', 'B') when the title is a clean match-winner question."""
    for part in (title or "").split(":"):
        part = part.strip()
        if _PROP_MARKERS.search(part) or " - " in part:
            continue
        m = re.fullmatch(r"(.{2,40}?)\s+vs\.?\s+(.{2,40})", part)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None, None


def generic_vs_signal(cons: dict, open_events: list) -> dict:
    """Discovery route for any clean head-to-head consensus. The outcome
    must be one of the two named sides; everything else fails closed."""
    if _PROP_MARKERS.search(cons.get("title") or ""):
        return None
    name_a, name_b = _generic_vs_names(cons.get("title"))
    if not name_a or not name_b:
        return None
    ow = _surname_words(cons.get("outcome"))
    if not (ow <= _surname_words(name_a) or ow <= _surname_words(name_b)):
        return None                    # outcome isn't a side -> a prop
    return _binary_h2h_signal(cons, open_events, name_a, name_b)


# Politics — done THE RIGHT WAY or not at all. Cross-venue election
# markets are notorious for resolution mismatches: a primary is not the
# general, "wins the race" is not "wins the popular vote", and margin/
# turnout props are different bets entirely. So: only clean
# "Will <candidate> win <race>?" questions; QUALIFIERS (primary/special/
# runoff/nomination) must agree EXACTLY between the Polymarket title and
# the Kalshi event; candidate + race words must match exactly ONE open
# Kalshi event; margin-style props are rejected outright.
POLITICS_RE = re.compile(
    r"^will (.+?) win (?:the )?(.*?(?:senate|house|governor|president"
    r"|congression|district|primary|election|race|seat|nomination|mayor)"
    r".*?)\??$", re.I)
_POLITICS_PROPS = re.compile(
    r"margin|popular vote|turnout|by \d|electoral college|electoral votes"
    r"|approval|debate|cabinet|running mate|endorse|spread|points", re.I)
_QUALIFIERS = ("PRIMARY", "SPECIAL", "RUNOFF", "NOMINATION", "NOMINEE",
               "CAUCUS")


def _qualifier_set(text: str) -> set:
    words = _surname_words(text)
    quals = {q for q in _QUALIFIERS if q in words}
    # nominee == nomination for matching purposes
    if "NOMINEE" in quals:
        quals.discard("NOMINEE")
        quals.add("NOMINATION")
    return quals


def politics_signal(cons: dict, open_events: list) -> dict:
    """Map an election-winner consensus onto the exact Kalshi race."""
    title = (cons.get("title") or "").strip()
    if _POLITICS_PROPS.search(title):
        return None
    m = POLITICS_RE.match(title)
    if not m:
        return None
    candidate = _surname_words(m.group(1))
    race = _surname_words(m.group(2)) - {"THE", "WIN", "RACE", "SEAT",
                                         "ELECTION", "WILL"}
    quals = _qualifier_set(title)
    if not candidate or not race:
        return None
    hits = []
    for ev in open_events:
        blob = f"{ev.get('title', '')} {ev.get('sub_title', '')}"
        labels = " ".join((mk.get("yes_sub_title") or "")
                          for mk in ev.get("markets") or [])
        ew = _surname_words(blob + " " + labels)
        if candidate <= ew and race <= _surname_words(blob) \
                and _qualifier_set(blob) == quals:
            hits.append(ev)
    if len(hits) != 1:
        return None
    market = _match_side_market(hits[0], candidate)
    return _priced_signal(cons, market, hits[0]) if market else None


# Tournament-winner futures: identical semantics on both venues.
# "Will England win the 2026 FIFA World Cup?" <-> KXMENWORLDCUP-26-EN.
WC_WINNER_SERIES = "KXMENWORLDCUP"
WC_WINNER_RE = re.compile(
    r"^will (.+?) win the \d{4} (?:fifa|men'?s)? ?world cup\??$", re.I)


def wc_winner_signal(cons: dict, events: list) -> dict:
    """Map a World Cup WINNER futures consensus onto KXMENWORLDCUP."""
    m = WC_WINNER_RE.match((cons.get("title") or "").strip())
    if not m:
        return None
    team = _surname_words(m.group(1))
    if not team:
        return None
    for event in events:
        market = _match_side_market(event, team)
        if market:
            return _priced_signal(cons, market, event)
    return None


def scan() -> list:
    """Standard model result shape; 'date' carries the Kalshi event ticker
    so one-bet-per-event grouping and the sports theme cap apply."""
    from kalshi_client import KalshiClient
    sharps = select_sharp_wallets()
    if not sharps:
        log.info("No wallets passed the sharp filter this run.")
        return []
    consensus = build_consensus(sharps)
    log.info("Consensus markets (>=%d sharps agreeing): %d",
             MIN_WALLETS, len(consensus))
    if not consensus:
        return []

    client = KalshiClient(env="prod")
    cat_bars = load_category_bars()      # per-domain wallet grades, loaded once
    events_by_series = {}

    def events_for(series: str) -> list:
        if series not in events_by_series:
            try:
                data = client._request(
                    "GET", "/events",
                    params={"series_ticker": series, "status": "open",
                            "with_nested_markets": "true", "limit": 60})
                events_by_series[series] = data.get("events", [])
            except Exception as exc:
                log.warning("Kalshi events fetch failed for %s: %s",
                            series, exc)
                events_by_series[series] = []
        return events_by_series[series]

    def all_open_events() -> list:
        """Every open event, paged once per scan — venue DISCOVERY for
        advance plays, since tournament games aren't a fixed series."""
        if "__ALL__" not in events_by_series:
            out, cursor = [], ""
            try:
                for _ in range(12):
                    params = {"status": "open", "limit": 200,
                              "with_nested_markets": "true"}
                    if cursor:
                        params["cursor"] = cursor
                    data = client._request("GET", "/events", params=params)
                    out.extend(data.get("events", []))
                    cursor = data.get("cursor") or ""
                    if not cursor:
                        break
            except Exception as exc:
                log.warning("Open-events discovery fetch failed: %s", exc)
            events_by_series["__ALL__"] = out
        return events_by_series["__ALL__"]

    results = []
    for cons in consensus:
        ml = MONEYLINE_RE.match(cons["slug"])
        tennis = TENNIS_TITLE_RE.search(cons.get("title") or "")
        if ml:
            sig = consensus_signal(cons,
                                   events_for(LEAGUE_SERIES[ml.group(1)]))
        elif WC_RE.match(cons["slug"]):
            sig = wc_signal(cons, events_for(WC_SERIES))
        elif WC_ADV_RE.match(cons["slug"]):
            sig = advance_signal(cons, all_open_events())
            if not sig:
                log.info("Advance play not placeable (no binary Kalshi "
                         "venue found): %s | %s | %d sharps $%.0f @ %.0fc",
                         cons["title"], cons["outcome"], cons["wallets"],
                         cons["stake"], 100 * cons["avg_price"])
        elif tennis:
            sig = tennis_signal(
                cons, events_for(TENNIS_SERIES[tennis.group(1).upper()]))
        elif WC_WINNER_RE.match((cons.get("title") or "").strip()):
            sig = wc_winner_signal(cons, events_for(WC_WINNER_SERIES))
        elif POLITICS_RE.match((cons.get("title") or "").strip()):
            sig = politics_signal(cons, all_open_events())
            if sig is None:
                log.info("Politics consensus not safely mappable "
                         "(qualifier/uniqueness guard): %s | %s",
                         cons["title"], cons["outcome"])
                continue
        else:
            # last resort: generic head-to-head discovery across ALL open
            # Kalshi events (UFC, golf, soccer clubs — whatever is listed)
            sig = generic_vs_signal(cons, all_open_events())
            if sig is None:
                log.info("Unmappable consensus (no Kalshi venue): %s | %s | "
                         "%d sharps $%.0f @ %.0fc", cons["title"],
                         cons["outcome"], cons["wallets"], cons["stake"],
                         100 * cons["avg_price"])
                continue
        if not sig:
            continue
        # per-category gate: only sharps with a NON-losing record in this
        # domain count. If barring them drops the consensus below the
        # minimum, the pick no longer has enough proven backing -> skip.
        cat = category_of(sig["ticker"])
        backing = [w for w in sig.get("wallet_ids", [])
                   if category_allowed(w, cat, cat_bars)]
        if len(backing) < MIN_WALLETS:
            log.info("Category gate (%s): %s dropped — only %d/%d backing "
                     "sharps have a non-losing %s record", cat, sig["ticker"],
                     len(backing), cons["wallets"], cat)
            continue
        event_ticker = sig.pop("event_ticker")
        title = sig.pop("event_title")
        log.info("SMART MONEY: %d sharps ($%.0f) on %s -> %s",
                 cons["wallets"], cons["stake"], cons["outcome"],
                 sig["ticker"])
        results.append(dict(date=event_ticker, mu=100 * cons["avg_price"],
                            city=f"{cons['wallets']} sharps",
                            title=title, signals=[sig]))
    return results


def main() -> int:
    setup_logging()
    try:
        score_copier_clv()                # early line read before settlement
        score_pending_paper_trades(PAPER_LOG)
    except Exception as exc:
        log.warning("Scoring skipped (%s)", exc)
    results = scan()
    total = sum(len(r["signals"]) for r in results)
    for r in results:
        for s in r["signals"]:
            log.info("  SIGNAL: buy %s %s @ %.0fc | prob %.0f%% | EV +%.1fc | %s",
                     s["side"].upper(), s["ticker"], s["price_cents"],
                     100 * s["model_prob"], s["ev_cents"], s["subtitle"])
    log.info("%s smart-money signal(s). NO ORDERS placed by this script.",
             total or "No")
    return 0


if __name__ == "__main__":
    sys.exit(main())
