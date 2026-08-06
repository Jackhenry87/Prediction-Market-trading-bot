"""Prose -> ticker resolution.

Every test here is about a REFUSAL. Resolving "Chicago C" to the wrong side
of the wrong game does not produce a bad trade, it produces a confident bad
trade sized by Kelly with real money. One unambiguous hit, or nothing.
"""

import follow_resolve


def _event(ticker, title, markets):
    return {"event_ticker": ticker, "title": title, "markets": markets}


def _market(ticker, label):
    return {"ticker": ticker, "yes_sub_title": label}


CUBS = _event("KXMLBGAME-26AUG06LADCHC", "Los Angeles D vs Chicago C",
              [_market("KXMLBGAME-26AUG06LADCHC-CHC", "Chicago C"),
               _market("KXMLBGAME-26AUG06LADCHC-LAD", "Los Angeles D")])
PADRES = _event("KXMLBGAME-26AUG06COLSD", "Colorado vs San Diego",
                [_market("KXMLBGAME-26AUG06COLSD-SD", "San Diego"),
                 _market("KXMLBGAME-26AUG06COLSD-COL", "Colorado")])


def test_matches_the_right_event():
    ev = follow_resolve.match_event("Los Angeles D vs Chicago C",
                                    [CUBS, PADRES])
    assert ev is CUBS


def test_picks_the_right_side_of_the_event():
    m = follow_resolve.match_market("Chicago C", CUBS)
    assert m["ticker"] == "KXMLBGAME-26AUG06LADCHC-CHC"
    m = follow_resolve.match_market("Los Angeles D", CUBS)
    assert m["ticker"] == "KXMLBGAME-26AUG06LADCHC-LAD"


def test_ambiguous_event_resolves_to_nothing():
    """Two games matching the same words is not a coin-flip situation."""
    twin = _event("KXMLBGAME-26AUG07LADCHC", "Los Angeles D vs Chicago C",
                  [_market("X-CHC", "Chicago C")])
    assert follow_resolve.match_event("Los Angeles D vs Chicago C",
                                      [CUBS, twin]) is None


def test_unknown_matchup_resolves_to_nothing():
    assert follow_resolve.match_event("Boston vs A's", [CUBS, PADRES]) is None


def test_malformed_matchup_resolves_to_nothing():
    for bad in (None, "", "Chicago C", "a vs b vs c"):
        assert follow_resolve.match_event(bad, [CUBS, PADRES]) is None


def test_outcome_that_matches_no_market_resolves_to_nothing():
    assert follow_resolve.match_market("Milwaukee", CUBS) is None


def test_single_market_event_needs_no_outcome_label():
    """A binary event with one market is unambiguous on its own."""
    solo = _event("KXMLBRFI-26AUG06", "Detroit vs Seattle: First Inning Run",
                  [_market("KXMLBRFI-26AUG06DETSEA", "Yes")])
    assert follow_resolve.match_market("", solo)["ticker"] == \
        "KXMLBRFI-26AUG06DETSEA"


def test_no_outcome_on_a_multi_market_event_is_refused():
    assert follow_resolve.match_market("", CUBS) is None


def test_totals_label_matches_by_substring():
    """'Over 8.5 runs scored' loses its digits to the word tokeniser, so the
    looser substring path has to carry it."""
    totals = _event("KXMLBGAME-26AUG06CLECIN-TOT",
                    "Cleveland vs Cincinnati: Total Runs",
                    [_market("T-OVER85", "Over 8.5 runs scored"),
                     _market("T-OVER95", "Over 9.5 runs scored")])
    m = follow_resolve.match_market("Over 8.5 runs scored", totals)
    assert m["ticker"] == "T-OVER85"


def _only_mlb(monkeypatch, events):
    """Serve `events` for the moneyline series and nothing for the rest, so
    no test touches the network."""
    monkeypatch.setattr(
        follow_resolve, "series_events",
        lambda c, series, force=False: (events if series == "KXMLBGAME"
                                        else []))


def test_resolve_returns_none_when_nothing_matches(monkeypatch):
    _only_mlb(monkeypatch, [CUBS, PADRES])
    assert follow_resolve.resolve_ticker(
        None, {"market_title": "Boston vs A's",
               "outcome_text": "Boston"}) is None


def test_resolve_returns_the_ticker_on_a_clean_match(monkeypatch):
    _only_mlb(monkeypatch, [CUBS, PADRES])
    hit = follow_resolve.resolve_ticker(
        None, {"market_title": "Los Angeles D vs Chicago C",
               "outcome_text": "Chicago C"})
    assert hit["ticker"] == "KXMLBGAME-26AUG06LADCHC-CHC"
    assert hit["event_ticker"] == "KXMLBGAME-26AUG06LADCHC"


def test_empty_event_list_is_survivable(monkeypatch):
    """A dead Kalshi listing is a miss, not a crash."""
    _only_mlb(monkeypatch, [])
    assert follow_resolve.resolve_ticker(
        None, {"market_title": "Los Angeles D vs Chicago C",
               "outcome_text": "Chicago C"}) is None


def test_one_game_in_two_series_still_resolves(monkeypatch):
    """The bug that live data exposed: a game exists as BOTH a moneyline and
    a totals event. Searching every series at once calls that ambiguous and
    resolves nothing; a series-at-a-time walk lets the outcome label pick."""
    totals = _event("KXMLBTOTAL-26AUG06LADCHC",
                    "Los Angeles D vs Chicago C: Total Runs",
                    [_market("KXMLBTOTAL-26AUG06LADCHC-8", "Over 8.5 runs scored")])
    monkeypatch.setattr(
        follow_resolve, "series_events",
        lambda c, series, force=False: {"KXMLBGAME": [CUBS],
                                        "KXMLBTOTAL": [totals]}.get(series, []))

    ml = follow_resolve.resolve_ticker(
        None, {"market_title": "Los Angeles D vs Chicago C",
               "outcome_text": "Chicago C"})
    assert ml["ticker"] == "KXMLBGAME-26AUG06LADCHC-CHC"

    tot = follow_resolve.resolve_ticker(
        None, {"market_title": "Los Angeles D vs Chicago C",
               "outcome_text": "Over 8.5 runs scored"})
    assert tot["ticker"] == "KXMLBTOTAL-26AUG06LADCHC-8"


def test_a_miss_retries_once_when_a_cache_is_stale(monkeypatch):
    """Markets he bets are often minutes old, so a cold/stale cache earns a
    forced refresh before we give up."""
    follow_resolve._cache.clear()
    calls = []

    def fake(c, series, force=False):
        calls.append((series, force))
        return []

    monkeypatch.setattr(follow_resolve, "series_events", fake)
    follow_resolve.resolve_ticker(None, {"market_title": "A vs B",
                                         "outcome_text": "A"})
    assert any(force for _, force in calls)          # the retry happened


def test_a_miss_does_not_retry_when_everything_is_fresh(monkeypatch):
    """Two thirds of his trades are markets we cannot price at all, so the
    miss path is the common path — a forced second pass over 16 series that
    were just fetched is pure waste."""
    import time as _t
    follow_resolve._cache.clear()
    for s in follow_resolve.SERIES:                  # all fresh
        follow_resolve._cache[s] = (_t.time(), [])
    calls = []

    def fake(c, series, force=False):
        calls.append((series, force))
        return []

    monkeypatch.setattr(follow_resolve, "series_events", fake)
    follow_resolve.resolve_ticker(None, {"market_title": "A vs B",
                                         "outcome_text": "A"})
    assert calls, "should still have looked"
    assert not any(force for _, force in calls)      # but never forced
    assert len(calls) == len(follow_resolve.SERIES)  # exactly one pass
