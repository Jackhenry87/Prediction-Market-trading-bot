"""Generates SCOREBOARD.md — a phone-friendly, color-coded view of the
paper-trade ledgers (green wins, red losses). GitHub renders it directly.

    python scoreboard.py     # rebuild by hand; auto_trade also rebuilds it
"""

import csv
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "SCOREBOARD.md"
SOURCES = [
    ("🌡️ Weather model", ROOT / "paper_trades.csv"),
    ("₿ Crypto model", ROOT / "paper_trades_crypto.csv"),
    ("⚾ Sports model", ROOT / "paper_trades_sports.csv"),
    ("🛢️ Commodities model", ROOT / "paper_trades_commodities.csv"),
    ("📰 Macro resolution-lag", ROOT / "paper_trades_macro.csv"),
    ("🦈 Smart money (Polymarket sharps)", ROOT / "paper_trades_smartmoney.csv"),
    ("⏱️ Nowcast (intraday known outcomes)", ROOT / "paper_trades_nowcast.csv"),
]
MAX_ROWS = 20


def _read(path: Path):
    if not path.exists():
        return None, []
    with open(path, newline="") as fh:
        rows = list(csv.reader(fh))
    if len(rows) < 2:
        return None, []
    return rows[0], rows[1:]


def _pnl_cents(outcome: str) -> float:
    if "(" not in outcome:
        return 0.0
    return float(outcome.split("(")[1].rstrip("c)").replace("+", ""))


def _account_section(lines: list) -> None:
    """Headline money truth, anchored so it can't lie: total EQUITY (cash +
    live value of open positions) minus what was deposited."""
    import json
    path = ROOT / "account_snapshot.json"
    if not path.exists():
        return
    try:
        snap = json.loads(path.read_text())
    except ValueError:
        return
    # net P&L = equity - deposits (fall back to legacy field if present)
    pnl = snap.get("net_pnl_usd", snap.get("realized_pnl_usd", 0.0))
    sign = "+" if pnl >= 0 else "-"
    dot = "🟢" if pnl >= 0 else "🔴"
    cash = snap.get("balance_usd", 0.0)
    pv = snap.get("positions_value_usd")
    equity = snap.get("equity_usd", cash + (pv or 0.0))
    deposits = snap.get("deposits_usd")
    pv_s = "n/a" if pv is None else f"${pv:.2f}"
    lines += [
        "## 💰 Account", "",
        f"**Equity ${equity:.2f}** = cash ${cash:.2f} + open positions "
        f"{pv_s}", "",
    ]
    if deposits is not None:
        lines += [f"{dot} **Net P&L: {sign}${abs(pnl):.2f}** "
                  f"(${deposits:.2f} deposited → ${equity:.2f} now) · "
                  f"{snap.get('settled_wins', 0)}W / "
                  f"{snap.get('settled_losses', 0)}L settled", ""]
    else:
        lines += [f"{dot} **{sign}${abs(pnl):.2f}** since "
                  f"{snap.get('since', '?')}", ""]


def _confidence_map() -> dict:
    """ticker -> model win-probability, harvested from every signal ledger,
    so a placed order can show the confidence the model had behind it."""
    out = {}
    for _, path in SOURCES:
        header, body = _read(path)
        if not header or "model_prob" not in header or "ticker" not in header:
            continue
        ti, pi = header.index("ticker"), header.index("model_prob")
        for r in body:
            if len(r) > pi and r[pi]:
                try:
                    out[r[ti]] = float(r[pi])
                except ValueError:
                    pass
    return out


def _executed_section(lines: list) -> None:
    """Real orders actually placed (the money audit trail)."""
    path = ROOT / "executed_trades.csv"
    conf = _confidence_map()
    header, body = _read(path)
    lines += ["## 💵 Real orders placed", ""]
    if not header:
        lines += ["_No real orders placed yet._", ""]
        return
    idx = {h: i for i, h in enumerate(header)}
    spent = sum(float(r[idx["cost_usd"]]) for r in body if r[idx.get("cost_usd", -1)])

    def outcome(r):
        i = idx.get("outcome")
        return r[i] if i is not None and len(r) > i else ""

    def realized_usd(r):
        # outcome like "win (+42c)" is per contract; scale by count
        try:
            cents = float(outcome(r).split("(")[1].rstrip("c)"))
            return cents * float(r[idx["count"]]) / 100.0
        except (IndexError, ValueError):
            return None

    settled = [r for r in body if outcome(r)]
    wins = sum(1 for r in settled if outcome(r).startswith("win"))
    realized = sum(realized_usd(r) or 0.0 for r in settled)
    head = f"**{len(body)} orders, ${spent:.2f} deployed.**"
    if settled:
        rsign = "+" if realized >= 0 else "-"
        head += (f" Settled: **{wins} W — {len(settled) - wins} L, "
                 f"realized {rsign}${abs(realized):.2f}**; "
                 f"{len(body) - len(settled)} open.")
    lines += [head, "",
              "| Placed (UTC) | Model | Market | Side | Qty | Price "
              "| Confidence | Cost | Result |",
              "|---|---|---|---|---|---|---|---|---|"]
    for r in reversed(body[-MAX_ROWS:]):
        when = r[idx["placed_at_utc"]][5:16].replace("T", " ")
        out = outcome(r)
        usd = realized_usd(r)
        if not out:
            res = "⏳ open"
        else:
            dot = "🟢" if out.startswith("win") else "🔴"
            sign = "+" if (usd or 0) >= 0 else "-"
            res = f"{dot} {out.split(' ')[0]} ({sign}${abs(usd or 0):.2f})"
        p = conf.get(r[idx["ticker"]])
        conf_s = f"{p * 100:.0f}%" if p is not None else "—"
        lines.append(
            f"| {when} | {r[idx['model']]} | {r[idx['ticker']]} "
            f"| {r[idx['side']].upper()} | {r[idx['count']]} "
            f"| {r[idx['price_cents']]}¢ | {conf_s} | ${r[idx['cost_usd']]} "
            f"| {res} |")
    lines.append("")


def _weather_by_city(lines: list) -> None:
    """Per-city weather P&L, read from weather_city_pnl.json — which the
    hourly builds from Kalshi SETTLEMENTS (the authoritative record), never
    from the local ledger. Shows nothing until that snapshot exists."""
    import json
    path = ROOT / "weather_city_pnl.json"
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return
    cities = data.get("cities") or {}
    if not cities:
        return
    lines += [f"## 🌡️ Weather by city (real settled P&L since "
              f"{data.get('since', '')})", "",
              "| City | Net | W–L |", "|---|---|---|"]
    for name, a in sorted(cities.items(), key=lambda kv: -kv[1]["net"]):
        net = a["net"]
        s = "+" if net >= 0 else "-"
        dot = "🟢" if net >= 0 else "🔴"
        lines.append(f"| {name} | {dot} {s}${abs(net):.2f} "
                     f"| {a.get('wins', 0)}–{a.get('losses', 0)} |")
    lines += [""]


def _wallet_leaderboard(lines: list) -> None:
    """Which sharp wallets have actually made us money — the flywheel's
    feedback. Sizing tilts toward the top and away from the bottom once a
    wallet has enough settled copies."""
    try:
        import strategy_smartmoney as sm
        scores = sm.wallet_scores()
    except Exception:
        return
    ranked = sorted((dict(w=w, **s) for w, s in scores.items() if s["n"] > 0),
                    key=lambda r: -r["net"] / r["n"])
    if not ranked:
        return
    lines += ["## 🦈 Sharp wallet leaderboard", "",
              "_Realized ¢/copy from the wallets we've followed "
              f"(sizing tilts once a wallet has ≥{sm.PROVEN_MIN} settled)._",
              "", "| Wallet | Copies | Net | ¢/copy | Sizing |",
              "|---|---|---|---|---|"]
    show = ranked[:5] if len(ranked) <= 8 else ranked[:4] + ranked[-4:]
    for r in show:
        w = r["w"]
        tag = f"{w[:6]}…{w[-4:]}" if len(w) > 12 else w
        per = r["net"] / r["n"]
        proven = r["n"] >= sm.PROVEN_MIN
        mult = sm.backers_multiplier([w], scores) if proven else 1.0
        sizing = f"×{mult:.2f}" if proven else "building"
        lines += [f"| `{tag}` | {r['n']} | {r['net']:+.0f}¢ | {per:+.1f} | "
                  f"{sizing} |"]
    lines += [""]


def build(out: Path = OUT) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# 📊 Trading Scoreboard",
        "",
        f"_Updated {now} — auto-generated every run; do not edit._",
        "",
        "Signals are scored against official settlement whether or not a "
        "real order was placed. P&L shown is per 1-contract stakes.",
        "",
    ]
    _account_section(lines)
    lines += [
        "",
    ]
    _executed_section(lines)
    _weather_by_city(lines)
    _wallet_leaderboard(lines)

    for name, path in SOURCES:
        header, body = _read(path)
        if not header:
            lines += [f"## {name}", "", "_No signals recorded yet._", ""]
            continue
        idx = {h: i for i, h in enumerate(header)}
        outcomes = [r[idx["outcome"]] for r in body]
        wins = sum(1 for o in outcomes if o.startswith("win"))
        losses = sum(1 for o in outcomes if o.startswith("loss"))
        pending = len(body) - wins - losses
        pnl = sum(_pnl_cents(o) for o in outcomes)

        lines += [
            f"## {name}",
            "",
            f"### 🟢 {wins} W — 🔴 {losses} L — ⏳ {pending} pending "
            f"— net **{pnl:+.0f}¢**",
            "",
        ]
        # calibration + closing-line value: the early-warning metrics.
        # Brier 0.25 = coin flip (lower is better); positive CLV means we
        # beat the closing price — the classic predictor of profitability.
        settled = [r for r in body
                   if r[idx["outcome"]].startswith(("win", "loss"))]
        if "model_prob" in idx:
            briers = []
            for r in settled:
                try:
                    p = float(r[idx["model_prob"]])
                except ValueError:
                    continue
                won = 1.0 if r[idx["outcome"]].startswith("win") else 0.0
                briers.append((p - won) ** 2)
            # CLV is sampled mid-life (before settlement), so count every
            # row that has a reading, not just settled ones — that's the
            # early signal we want to see as soon as it exists
            clvs = []
            if "clv_cents" in idx:
                for r in body:
                    if len(r) > idx["clv_cents"] and r[idx["clv_cents"]]:
                        try:
                            clvs.append(float(r[idx["clv_cents"]]))
                        except ValueError:
                            pass
            stats = []
            if briers:
                stats.append(f"Brier **{sum(briers) / len(briers):.3f}** "
                             f"(coin flip 0.25) over {len(briers)} settled")
            if clvs:
                avg = sum(clvs) / len(clvs)
                stats.append(f"avg CLV **{avg:+.1f}¢** over {len(clvs)} sampled")
            if stats:
                lines += ["_" + " · ".join(stats) + "_", ""]
        lines += [
            "| Scanned (UTC) | Market | Side | Price | Model | Result |",
            "|---|---|---|---|---|---|",
        ]
        for r in reversed(body[-MAX_ROWS:]):
            outcome = r[idx["outcome"]]
            if outcome.startswith("win"):
                result = f"🟢 **{outcome}**"
            elif outcome.startswith("loss"):
                result = f"🔴 {outcome}"
            else:
                result = "⏳ pending"
            when = r[idx["scanned_at_utc"]][5:16].replace("T", " ")
            prob = float(r[idx["model_prob"]]) * 100
            lines.append(
                f"| {when} | {r[idx['ticker']]} | {r[idx['side']].upper()} "
                f"| {float(r[idx['price_cents']]):.0f}¢ | {prob:.0f}% | {result} |"
            )
        if len(body) > MAX_ROWS:
            lines.append(f"| … | _{len(body) - MAX_ROWS} older rows in the "
                         f"CSV_ | | | | |")
        lines.append("")
    out.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    build()
    print(f"Wrote {OUT}")
