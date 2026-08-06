"""Notification parsing.

We have never seen a real Kalshi followed-trader notification, so these
fixtures are informed guesses at the wording. They pin the BEHAVIOUR that
must hold whatever the wording turns out to be: no username means no signal,
the wrong username means no signal, an exit is not an entry, and anything
unreadable returns None rather than a half-built order.
"""

import follow_parse

ME = "blushing.wildebeest7119"


def test_full_notification_parses():
    sig = follow_parse.parse_notification(
        "blushing.wildebeest7119 bought 354,725 Yes on Chicago C at 42¢\n"
        "Los Angeles D vs Chicago C · Yes · Chicago C",
        expect_username=ME)
    assert sig is not None
    assert sig["username"] == ME
    assert sig["side"] == "yes"
    assert sig["price_cents"] == 42
    assert sig["contracts"] == 354725
    assert sig["market_title"] == "Los Angeles D vs Chicago C"
    assert sig["outcome_text"] == "Chicago C"


def test_dollar_price_form():
    sig = follow_parse.parse_notification(
        "blushing.wildebeest7119 bought Yes on Boston at $0.55\n"
        "Boston vs A's · Yes · Boston", expect_username=ME)
    assert sig["price_cents"] == 55


def test_no_side_is_read():
    sig = follow_parse.parse_notification(
        "blushing.wildebeest7119 bought No on Over 8.5 runs scored\n"
        "Cleveland vs Cincinnati: Total Runs · No · Over 8.5 runs scored",
        expect_username=ME)
    assert sig["side"] == "no"
    assert sig["outcome_text"] == "Over 8.5 runs scored"


# --- the refusals: each of these must NOT produce a tradeable signal ------

def test_different_trader_is_ignored():
    """Following one account must never become following the world."""
    assert follow_parse.parse_notification(
        "smiling.aardvark1234 bought Yes on Chicago C at 42¢\n"
        "Los Angeles D vs Chicago C · Yes · Chicago C",
        expect_username=ME) is None


def test_sale_is_not_copied():
    assert follow_parse.parse_notification(
        "blushing.wildebeest7119 sold 5,000 Yes on Chicago C at 61¢\n"
        "Los Angeles D vs Chicago C · Yes · Chicago C",
        expect_username=ME) is None


def test_no_username_no_signal():
    assert follow_parse.parse_notification(
        "Someone bought Yes on Chicago C at 42¢", expect_username=ME) is None


def test_no_side_no_signal():
    assert follow_parse.parse_notification(
        "blushing.wildebeest7119 is trading again\n"
        "Los Angeles D vs Chicago C", expect_username=ME) is None


def test_garbage_returns_none():
    for junk in ("", "   ", "Your deposit is complete. Start trading now.",
                 "\n\n", "Markets Settled"):
        assert follow_parse.parse_notification(junk, expect_username=ME) is None


def test_missing_price_still_parses():
    """A notification with no price is still tradeable — our own model
    supplies p and the live book supplies the entry."""
    sig = follow_parse.parse_notification(
        "blushing.wildebeest7119 placed a bet\n"
        "Boston vs A's · Yes · Boston", expect_username=ME)
    assert sig is not None
    assert sig["price_cents"] is None
    assert sig["side"] == "yes"


def test_price_is_not_recorded_as_a_stake():
    sig = follow_parse.parse_notification(
        "blushing.wildebeest7119 bought Yes on Boston at $0.55\n"
        "Boston vs A's · Yes · Boston", expect_username=ME)
    assert sig["stake_usd"] is None


def test_stake_is_recorded_but_separate_from_price():
    sig = follow_parse.parse_notification(
        "blushing.wildebeest7119 bought Yes on Boston at 55¢ ($100,000)\n"
        "Boston vs A's · Yes · Boston", expect_username=ME)
    assert sig["price_cents"] == 55
    assert sig["stake_usd"] == 100000.0


def test_username_shape_is_matched_generically():
    """Kalshi mints names as adjective.animal####; the parser reads the shape
    so a renamed follow target needs a config change, not a code change."""
    sig = follow_parse.parse_notification(
        "curious.otter42 bought Yes on Boston at 55¢\n"
        "Boston vs A's · Yes · Boston", expect_username="curious.otter42")
    assert sig["username"] == "curious.otter42"


def test_expect_username_is_case_insensitive():
    sig = follow_parse.parse_notification(
        "Blushing.Wildebeest7119 bought Yes on Boston at 55¢\n"
        "Boston vs A's · Yes · Boston", expect_username=ME)
    assert sig is not None and sig["username"] == ME
