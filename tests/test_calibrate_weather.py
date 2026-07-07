"""Tests for weather self-calibration (recent-window + auto-bench)."""

import calibrate_weather as cw


def _stats(n, bias, sigma, std=None):
    return dict(n=n, bias=bias, suggested_sigma=sigma,
                std=std if std is not None else sigma)


def test_prefers_recent_when_enough_days(monkeypatch):
    monkeypatch.setattr(cw, "MIN_RECENT", 20)
    monkeypatch.setattr(cw, "BENCH_SIGMA", 5.0)
    recent = _stats(30, 1.8, 2.5)      # current season ran warm, tight
    baseline = _stats(300, 0.4, 3.0)
    c = cw.calibrate(recent, baseline)
    assert c["window"] == "recent" and c["bias"] == 1.8
    # never tighter than the long-run measurement
    assert c["sigma"] == 3.0 and c["trade"] is True


def test_benches_when_error_blows_out(monkeypatch):
    monkeypatch.setattr(cw, "MIN_RECENT", 20)
    monkeypatch.setattr(cw, "BENCH_SIGMA", 5.0)
    # recent forecasts wildly off (sigma 6F > buckets) -> no edge -> bench
    c = cw.calibrate(_stats(30, -0.5, 6.0), _stats(300, 0.3, 3.0))
    assert c["trade"] is False and c["sigma"] == 6.0


def test_falls_back_to_baseline_when_recent_thin(monkeypatch):
    monkeypatch.setattr(cw, "MIN_RECENT", 20)
    c = cw.calibrate(_stats(5, 4.0, 2.0), _stats(300, 0.5, 3.0))
    assert c["window"] == "baseline" and c["bias"] == 0.5
    assert c["sigma"] == 3.0 and c["trade"] is True


def test_calibrate_handles_missing_recent(monkeypatch):
    c = cw.calibrate(None, _stats(300, 0.5, 3.0))
    assert c["window"] == "baseline" and c["trade"] is True
    assert cw.calibrate(None, None) is None
