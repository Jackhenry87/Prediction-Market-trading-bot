"""Turn a notification's prose ("Los Angeles D vs Chicago C" / "Chicago C")
into a Kalshi ticker.

The push notification names a market the way a human reads it. Orders need a
ticker like KXMLBGAME-26AUG06LADCHC-CHC. This module bridges the two by
walking Kalshi's own open-events listing and matching on team/outcome words.

FAILS CLOSED, ALWAYS
--------------------
Every function here returns None the moment a match is ambiguous. Resolving
"Chicago C" to the wrong side of the wrong game does not produce a bad trade
— it produces a confident bad trade, sized by Kelly, with real money. One
unambiguous hit or nothing.

This reuses the matching primitives already written for the smart-money
copier, which solves the identical problem (mapping an off-venue pick onto a
Kalshi market): `strategy_sports._words` for tokenising team labels and
`match_team`'s "exactly one hit" discipline.

WHY WE QUERY BY SERIES, NOT THE WHOLE BOARD
-------------------------------------------
The obvious implementation — page through every open event and search it —
does not work, and fails silently. Kalshi has far more open events than a
paged walk returns: a 2,000-event walk on a live August afternoon came back
522 midterm-movement markets, 502 turnout markets, and **zero** MLB games,
while a targeted `series_ticker=KXMLBGAME` query returned all 52 of them.
A resolver built on the walk would simply never find the markets he trades.

So we query the specific series our models can price. That is bounded, fast,
and exactly aligned with the copier's central rule: we only trade what a
model of ours can put a probability on, so those are the only series worth
resolving into.

Each series is cached for EVENTS_TTL seconds, and a lookup that misses
forces one refresh before giving up — the markets he bets are often minutes
old, and must not be invisible for the life of a cache entry.
"""

import os
import time

from strategy_sports import _words
from trade_logger import get_logger

log = get_logger("follow_resolve")

EVENTS_TTL = float(os.getenv("FOLLOW_EVENTS_TTL", "120"))

# The series follow_prob can price. Anything outside this list is a market
# we would refuse to trade anyway, so there is no reason to resolve it.
SERIES = [s.strip().upper() for s in os.getenv(
    "FOLLOW_SERIES",
    "KXMLBGAME,KXMLBTOTAL,KXMLBRFI,KXNBAGAME,KXNBA,KXNFLGAME,KXNHLGAME,"
    "KXWNBAGAME,KXHIGHNY,KXHIGHCHI,KXHIGHMIA,KXHIGHDEN,KXHIGHLAX,KXHIGHAUS,"
    "KXBTCD,KXETHD").split(",") if s.strip()]

_cache = {}          # series -> (fetched_at, events)


def fetch_series_events(client, series: str) -> list:
    """Open events for one series, with nested markets."""
    data = client._request("GET", "/events",
                           params={"series_ticker": series, "status": "open",
                                   "with_nested_markets": "true",
                                   "limit": 200})
    return data.get("events", []) or []


def series_events(client, series: str, force: bool = False) -> list:
    """Cached open events for ONE series.

    A series whose fetch fails keeps its last good value rather than
    vanishing — one flaky call must not make a market look unresolvable.
    """
    hit = _cache.get(series)
    if not force and hit and time.time() - hit[0] < EVENTS_TTL:
        return hit[1]
    try:
        events = fetch_series_events(client, series)
        _cache[series] = (time.time(), events)
        return events
    except Exception as exc:
        log.warning("events fetch for %s failed: %s", series, exc)
        return hit[1] if hit else []


def open_events(client, force: bool = False) -> list:
    """Every open event across the priced series. Convenience for callers
    that want the whole picture; `resolve_ticker` deliberately does NOT use
    this (see its docstring)."""
    out = []
    for series in SERIES:
        out += series_events(client, series, force=force)
    return out


def _leading_words(label: str) -> set:
    """First token of each side of a matchup, lowercased.

    Kalshi truncates team names ("Los Angeles D", "Chicago C"), so the only
    reliably shared token between its title and ours is the leading city
    word. `match_total_game` in strategy_sports keys off exactly this.
    """
    return {w.lower() for w in (label or "").split() if len(w) > 2}


def match_event(market_title: str, events: list) -> dict:
    """The single open event whose title names both sides of the matchup.

    Returns None unless exactly one event matches — two candidates means we
    cannot tell which game he bet, and a coin flip is not a resolution.
    """
    if not market_title:
        return None
    parts = market_title.split(" vs ")
    if len(parts) != 2:
        return None
    home_words, away_words = _leading_words(parts[0]), _leading_words(parts[1])
    if not home_words or not away_words:
        return None

    hits = []
    for ev in events:
        title = (ev.get("title") or "").lower()
        if (any(w in title for w in home_words)
                and any(w in title for w in away_words)):
            hits.append(ev)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        log.info("matchup %r matched %d events — ambiguous, skipping",
                 market_title, len(hits))
    return None


def _market_labels(market: dict) -> str:
    """Every human-readable label Kalshi hangs off a market, joined."""
    return " ".join(str(market.get(k) or "") for k in
                    ("yes_sub_title", "subtitle", "title", "yes_sub_title"))


def match_market(outcome_text: str, event: dict) -> dict:
    """The market within an event that carries this outcome label.

    Tried STRONGEST EVIDENCE FIRST, because the weaker tests are actively
    misleading on some real markets. `strategy_sports._words` drops anything
    shorter than three letters, so "Over 8.5 runs scored" and "Over 9.5 runs
    scored" tokenise identically — a word-subset match calls that pair
    ambiguous and skips a signal it could have resolved exactly. So:

      1. exact label equality,
      2. substring of a label,
      3. word-subset containment.

    Each rung still requires exactly one hit; ambiguity at every rung is None.
    """
    if not event:
        return None
    markets = event.get("markets") or []
    if not outcome_text:
        # A binary event with a single market needs no outcome label.
        return markets[0] if len(markets) == 1 else None

    low = outcome_text.lower().strip()

    exact = [m for m in markets
             if any(low == str(m.get(k) or "").lower().strip()
                    for k in ("yes_sub_title", "subtitle", "title"))]
    if len(exact) == 1:
        return exact[0]

    sub = [m for m in markets if low in _market_labels(m).lower()]
    if len(sub) == 1:
        return sub[0]

    want = _words(outcome_text)
    if want:
        hits = [m for m in markets if want <= _words(_market_labels(m))]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            log.info("outcome %r matched %d markets in %s — ambiguous, "
                     "skipping", outcome_text, len(hits),
                     event.get("event_ticker"))
    return None


def resolve_ticker(client, signal: dict) -> dict:
    """Signal -> {'ticker', 'event_ticker', 'market'} or None.

    Walks the priced series ONE AT A TIME and returns the first series in
    which both the matchup and the outcome resolve unambiguously. Two
    properties fall out of that, and both were found the hard way against
    live data:

      * CORRECTNESS. One ball game appears in several series at once —
        "Los Angeles A vs Baltimore" is a KXMLBGAME event AND a KXMLBTOTAL
        event. Searching every series at once makes that look ambiguous and
        resolves nothing. Searching a series at a time lets the outcome
        label ("Los Angeles A" vs "Over 8.5 runs scored") pick the family.
      * LATENCY. Fetching all sixteen series took ~14s cold. Stopping at the
        first hit resolves a moneyline — most of what we can trade — after
        one call.

    A full miss retries once with the caches forced, because the markets he
    bets are often minutes old.
    """
    title, outcome = signal.get("market_title"), signal.get("outcome_text")
    for force in (False, True):
        for series in SERIES:
            events = series_events(client, series, force=force)
            if not events:
                continue
            event = match_event(title, events)
            if event is None and outcome:
                # Some notifications carry only an outcome ("Scottie
                # Scheffler") with no "A vs B" title.
                event = _event_by_outcome(outcome, events)
            market = match_market(outcome, event)
            if market and market.get("ticker"):
                return dict(ticker=market["ticker"],
                            event_ticker=event.get("event_ticker", ""),
                            market=market)
    log.info("could not resolve %r / %r to a ticker", title, outcome)
    return None


def _event_by_outcome(outcome_text: str, events: list) -> dict:
    """Find an event by a distinctive outcome label alone. Only useful for
    unique names (a golfer, a fighter); returns None on any ambiguity."""
    want = _words(outcome_text)
    if len(want) < 2:            # one-word outcomes are far too collidable
        return None
    hits = [ev for ev in events
            if any(want <= _words(_market_labels(m))
                   for m in (ev.get("markets") or []))]
    return hits[0] if len(hits) == 1 else None
