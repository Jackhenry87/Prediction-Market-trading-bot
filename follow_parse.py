"""Parse a Kalshi push notification about a followed trader into a signal.

WHY THIS MODULE EXISTS
----------------------
Kalshi exposes no way to attribute a trade to a named account. The public
tape (`GET /markets/trades`) carries `ticker`, `taker_side`, prices and a
`trade_id` — and no user field at all. `/users/<name>`, `/leaderboard` and
`/social/*` are 404; the web app's `/v1/users/...` routes answer only to a
browser session token, not to the RSA API key this bot signs with.

So the ONLY thing that knows "blushing.wildebeest7119 just bought X" is the
push notification on the owner's phone. An iOS Shortcut forwards that
notification's text to `follow_webhook`, and this module turns the text back
into structured data.

DEFENSIVE BY DESIGN
-------------------
The exact wording Kalshi uses is not known to us — we have never seen one of
these notifications. Every assumption here is therefore a guess under test:

  * patterns are tried in order, most specific first;
  * anything that does not match returns None rather than a half-parsed
    signal, because a wrong ticker with live money is far worse than a miss;
  * a notification naming a DIFFERENT trader returns None even if it parses
    perfectly — following one account must never become following the world;
  * unmatched bodies are written verbatim to follow_unparsed.log by the
    caller, so a real sample turns into a one-line pattern addition.

MISSING PRICE IS FINE
---------------------
If the notification says only "X placed a bet" with no price, the signal is
still useful: our own model supplies the probability and the live order book
supplies the entry price. `price_cents` is therefore optional, and its
absence only disables the staleness guard (see follow_runner).
"""

import re

# Kalshi auto-generates usernames as adjective.animal####  (e.g.
# blushing.wildebeest7119). Matching the SHAPE rather than one literal name
# lets the parser pull whoever the notification is about, and the caller then
# checks it against the single account we actually follow.
USERNAME_RE = re.compile(r"\b([a-z][a-z-]+\.[a-z][a-z-]+\d{2,6})\b", re.I)

# "42¢", "at 42c", "at 42 cents", "for 42¢"
PRICE_RE = re.compile(
    r"(?:at|for|@)?\s*(\d{1,3})\s*(?:¢|c\b|cents\b)", re.I)
# Kalshi also renders prices as dollars: "$0.42"
PRICE_DOLLARS_RE = re.compile(r"\$0?\.(\d{2})\b")
# "354,725 contracts" / "1 contract" / "5,000 shares", and the unit-less form
# Kalshi's own order emails use ("bought 354,725 Yes on ...").
CONTRACTS_RE = re.compile(r"([\d,]+)\s*(?:contracts?|shares?)\b", re.I)
CONTRACTS_BARE_RE = re.compile(
    r"\b(?:bought|buys|placed|backed|bet)\s+([\d,]+)\s+(?:yes|no)\b", re.I)
# "$100,000" / "$1,999.58" — his stake, recorded but never used to size ours
STAKE_RE = re.compile(r"\$([\d,]+(?:\.\d{1,2})?)(?!\s*\.\d)\b")
# "Los Angeles D vs Chicago C", "Sonego vs Griekspoor", "A vs. B".
# MULTILINE matters: iOS sends the notification's title and body as separate
# fields and the webhook joins them with a newline, so the matchup almost
# always ends at a LINE break rather than at the end of the string.
MATCHUP_RE = re.compile(
    r"([A-Z][\w'.\- ]{1,40}?)\s+vs\.?\s+([A-Z][\w'.\- ]{1,40}?)"
    r"(?=\s*(?:[:·—|]|$))", re.M)

# "bought Yes on Chicago C at 42¢" / "backed No on Over 8.5 runs scored".
# The explicit "on <outcome>" form is far stronger evidence than segment
# scanning, so it is tried first.
OUTCOME_ON_RE = re.compile(
    r"\b(?:yes|no)\s+on\s+(.+?)(?=\s+(?:at|for|@)\s|\s*[·—|]|$)", re.I | re.M)

# Words that mean "opened a position" vs "closed one". We copy ENTRIES only;
# a notification about a sale is a different (and unbuilt) strategy, so it is
# parsed and then dropped by the caller rather than silently treated as a buy.
BUY_WORDS = ("bought", "buys", "bet", "backed", "placed", "opened", "long")
SELL_WORDS = ("sold", "sells", "closed", "exited")

# Separators Kalshi's UI uses between a market and its outcome.
_SPLIT = re.compile(r"\s*[·|—\n]+\s*")


def _clean(text: str) -> str:
    """Collapse whitespace but KEEP the ·/—/newline separators, which are
    what distinguish the market title from the outcome label."""
    return re.sub(r"[ \t]+", " ", (text or "").strip())


def extract_price_cents(text: str):
    """Entry price in cents, or None. Tries the ¢ form then the $0.xx form."""
    m = PRICE_RE.search(text)
    if m:
        cents = int(m.group(1))
        if 0 < cents < 100:
            return cents
    m = PRICE_DOLLARS_RE.search(text)
    if m:
        cents = int(m.group(1))
        if 0 < cents < 100:
            return cents
    return None


def extract_side(text: str):
    """'yes' or 'no' — the side of the contract he took.

    Order matters: a bare "No" appears in plenty of prose, so we only read a
    side from an explicit outcome marker ("Yes · ", "bought No", "No on ...").
    Ambiguity returns None, and the caller skips.
    """
    low = text.lower()
    patterns = [
        (r"\b(?:bought|buys|placed|backed|bet)\s+(?:\d[\d,]*\s+)?(yes|no)\b",
         1),
        (r"\b(yes|no)\s*(?:·|—|\|)", 1),
        (r"\b(yes|no)\s+on\b", 1),
        (r"^\s*(yes|no)\s*$", 1),
    ]
    for pat, grp in patterns:
        m = re.search(pat, low, re.M)
        if m:
            return m.group(grp)
    return None


def extract_action(text: str):
    """'buy' | 'sell' | None. Entries and exits read very differently and
    only entries are in scope for v1."""
    low = text.lower()
    for w in SELL_WORDS:
        if re.search(rf"\b{w}\b", low):
            return "sell"
    for w in BUY_WORDS:
        if re.search(rf"\b{w}\b", low):
            return "buy"
    return None


def extract_matchup(text: str):
    """The 'A vs B' market title, or None. This is what follow_resolve turns
    into a Kalshi ticker."""
    m = MATCHUP_RE.search(text)
    return f"{m.group(1).strip()} vs {m.group(2).strip()}" if m else None


def extract_outcome(text: str, matchup: str = None):
    """The specific outcome he took ('Chicago C', 'Over 8.5 runs scored').

    Two sources, strongest first:

      1. an explicit "Yes on <outcome> at 42¢" phrase in the body;
      2. Kalshi's stacked `Market · Yes · Outcome` layout, where the outcome
         is the last segment that is not the matchup, a bare side word, or a
         price.

    A segment containing the trader's username is never an outcome — that is
    the sentence describing the trade, and treating it as a market label sent
    the whole notification body downstream as an outcome during testing.
    """
    m = OUTCOME_ON_RE.search(text)
    if m:
        candidate = m.group(1).strip(" .,")
        if candidate and not USERNAME_RE.search(candidate):
            return candidate

    parts = [p.strip() for p in _SPLIT.split(text) if p.strip()]
    for part in reversed(parts):
        low = part.lower()
        if low in ("yes", "no"):
            continue
        if matchup and part.strip() == matchup.strip():
            continue
        if re.fullmatch(r"[^A-Za-z]*", part):      # prices, counts, symbols
            continue
        if USERNAME_RE.search(part):               # the trade sentence itself
            continue
        # strip a leading side word: "Yes Chicago C" -> "Chicago C"
        cleaned = re.sub(r"^(yes|no)\b[\s:·—-]*", "", part, flags=re.I).strip()
        if cleaned and cleaned != matchup:
            return cleaned
    return None


def parse_notification(text: str, expect_username: str = None) -> dict:
    """Notification text -> signal dict, or None when it is not a usable
    entry by the account we follow.

    Returns:
        {username, action, side, market_title, outcome_text,
         price_cents|None, contracts|None, stake_usd|None, raw}

    None is returned — deliberately, and in preference to a guess — when the
    text names no user, names the WRONG user, carries no side, or describes
    an exit rather than an entry.
    """
    text = _clean(text)
    if not text:
        return None

    m = USERNAME_RE.search(text)
    if not m:
        return None
    username = m.group(1).lower()

    # Following one trader means following exactly one trader. A notification
    # about anyone else is dropped here, before any market lookup happens.
    if expect_username and username != expect_username.strip().lower():
        return None

    action = extract_action(text)
    if action == "sell":
        return None                     # exits are out of scope for v1

    side = extract_side(text)
    if side is None:
        return None

    matchup = extract_matchup(text)
    outcome = extract_outcome(text, matchup)
    if not matchup and not outcome:
        return None                     # nothing to resolve to a ticker

    contracts = None
    cm = CONTRACTS_RE.search(text) or CONTRACTS_BARE_RE.search(text)
    if cm:
        try:
            contracts = int(cm.group(1).replace(",", ""))
        except ValueError:
            contracts = None

    stake = None
    sm = STAKE_RE.search(text)
    if sm:
        try:
            value = float(sm.group(1).replace(",", ""))
            # "$0.42" is a price, not a stake — don't record it as one.
            stake = value if value >= 1 else None
        except ValueError:
            stake = None

    return dict(username=username, action=action or "buy", side=side,
                market_title=matchup, outcome_text=outcome,
                price_cents=extract_price_cents(text), contracts=contracts,
                stake_usd=stake, raw=text)
