"""Config loader: read-only scripts (account refresh, reports) must load with
just credentials — they never place orders, so the trading caps aren't required.
Trading scripts must still fail closed when a cap is missing."""

import tempfile

import pytest

import config


@pytest.fixture
def creds_only(monkeypatch):
    """Workflow env for a read-only job: creds + pem, no trading caps."""
    pem = tempfile.NamedTemporaryFile(suffix=".pem", delete=False)
    pem.write(b"x")
    pem.close()
    for k in ("MAX_ORDER_SIZE", "MAX_TOTAL_EXPOSURE", "MARKET_TICKER",
              "DRY_RUN", "KILL_SWITCH"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("KALSHI_ENV", "prod")
    monkeypatch.setenv("KALSHI_API_KEY_ID", "id")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", pem.name)


def test_read_only_load_needs_no_trading_caps(creds_only):
    s = config.load_kalshi_settings(require_market=False, require_trading=False)
    assert s.kalshi_env == "prod"
    assert s.max_order_size == 0.0 and s.max_total_exposure == 0.0


def test_trading_load_still_requires_caps(creds_only):
    # the default (trading) path must fail closed without the caps
    with pytest.raises(config.ConfigError):
        config.load_kalshi_settings(require_market=False)
