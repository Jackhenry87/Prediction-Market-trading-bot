"""Tests for the HARD-RULES order gate. Run with:  pytest tests/"""

from dataclasses import dataclass

import safety


@dataclass
class FakeSettings:
    kill_switch: bool = False
    max_order_size: float = 5.0
    max_total_exposure: float = 20.0


def check(**kwargs):
    defaults = dict(settings=FakeSettings(), side="BUY", price=0.10,
                    size_shares=10, current_exposure_usdc=0.0)
    defaults.update(kwargs)
    return safety.check_order(
        defaults["settings"], defaults["side"], defaults["price"],
        defaults["size_shares"], defaults["current_exposure_usdc"],
    )


def test_valid_order_passes():
    assert check() == []


def test_kill_switch_blocks_everything():
    problems = check(settings=FakeSettings(kill_switch=True))
    assert any("KILL_SWITCH" in p for p in problems)


def test_order_size_limit():
    # 60 shares @ 0.10 = 6.00 USDC > 5.00 cap
    assert any("MAX_ORDER_SIZE" in p for p in check(size_shares=60))
    # exactly at the cap is allowed: 50 * 0.10 = 5.00
    assert check(size_shares=50) == []


def test_total_exposure_limit():
    # 19.50 existing + 1.00 order = 20.50 > 20.00 cap
    problems = check(current_exposure_usdc=19.5)
    assert any("MAX_TOTAL_EXPOSURE" in p for p in problems)
    # exactly at the cap is allowed: 19.00 + 1.00 = 20.00
    assert check(current_exposure_usdc=19.0) == []


def test_sell_not_blocked_by_exposure():
    # SELL reduces exposure; only the per-order cap applies
    assert check(side="SELL", current_exposure_usdc=19.5) == []
    assert any("MAX_ORDER_SIZE" in p for p in check(side="SELL", size_shares=60))


def test_price_sanity():
    assert any("price" in p for p in check(price=0.0))
    assert any("price" in p for p in check(price=1.0))
    assert any("price" in p for p in check(price=-0.5))


def test_size_sanity():
    assert any("size" in p for p in check(size_shares=0))


def test_bad_side():
    assert any("side" in p for p in check(side="HOLD"))


def test_multiple_violations_all_reported():
    problems = check(settings=FakeSettings(kill_switch=True),
                     price=2.0, size_shares=-1)
    assert len(problems) >= 3


def test_scaled_caps_grow_with_bankroll(monkeypatch):
    from dataclasses import dataclass

    import safety

    @dataclass
    class S:
        max_order_size: float = 2.0
        max_total_exposure: float = 20.0

    # defaults: order 10% of bankroll (floor $2, ceiling $50);
    # exposure 80% of bankroll (floor $20, ceiling $250)
    monkeypatch.delenv("MAX_ORDER_BANKROLL_PCT", raising=False)
    monkeypatch.delenv("MAX_ORDER_ABS", raising=False)
    # tiny account: the static floor keeps trading possible
    assert safety.scaled_order_cap(15.36, S()) == 2.0      # 10% = $1.54 < $2
    assert safety.scaled_exposure_cap(15.36, S()) == 20.0
    # grown account: caps scale automatically — no repo Variable edits
    assert safety.scaled_order_cap(100.0, S()) == 10.0     # 10% of $100
    assert safety.scaled_exposure_cap(100.0, S()) == 80.0  # 80% of $100
    # huge account (or a bankroll-computation bug): ceilings backstop
    assert safety.scaled_order_cap(10_000.0, S()) == 50.0
    assert safety.scaled_exposure_cap(10_000.0, S()) == 250.0
    # knobs are env-tunable
    monkeypatch.setenv("MAX_ORDER_BANKROLL_PCT", "5")
    assert safety.scaled_order_cap(100.0, S()) == 5.0
