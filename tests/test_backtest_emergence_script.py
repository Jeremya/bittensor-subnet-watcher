from datetime import datetime, timedelta, timezone

import pytest

from models import SubnetSnapshot
from scripts.backtest_emergence import format_report, run_emergence_backtest


def _snap(netuid, days_ago, **overrides):
    data = {
        "netuid": netuid,
        "polled_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "alpha_price_tao": 1.0,
        "emergence_stage": "nascent",
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def test_run_emergence_backtest_buckets_forward_returns_and_skips_established():
    rows = [
        _snap(1, 14, emergence_score=85.0, alpha_price_tao=1.0),
        _snap(1, 7, alpha_price_tao=1.4),
        _snap(1, 0, alpha_price_tao=1.8),
        _snap(2, 14, emergence_score=65.0, alpha_price_tao=2.0, emergence_stage="accelerating"),
        _snap(2, 7, alpha_price_tao=1.8),
        _snap(2, 0, alpha_price_tao=1.6),
        _snap(3, 14, emergence_score=90.0, alpha_price_tao=1.0, emergence_stage="established"),
        _snap(3, 7, alpha_price_tao=2.0, emergence_stage="established"),
    ]

    report = run_emergence_backtest(rows)

    assert report["row_count"] == 8
    assert report["anchor_count"] == 2
    assert report["stage_counts"]["established"] == 2
    assert report["buckets"]["80+"]["anchors"] == 1
    assert report["buckets"]["80+"]["forward_7d"]["mean_return"] == pytest.approx(0.4)
    assert report["buckets"]["80+"]["forward_14d"]["mean_return"] == pytest.approx(0.8)
    assert report["buckets"]["60-70"]["anchors"] == 1
    assert report["buckets"]["60-70"]["forward_7d"]["mean_return"] == pytest.approx(-0.1)


def test_format_report_includes_stage_counts_and_no_none_text():
    report = {
        "generated_at": "2026-06-24T00:00:00+00:00",
        "row_count": 10,
        "anchor_count": 3,
        "horizons_hours": [168, 336],
        "stage_counts": {"nascent": 2, "accelerating": 1},
        "buckets": {
            "<50": {
                "anchors": 0,
                "forward_7d": {"samples": 0, "mean_return": None, "median_return": None, "positive_rate": None},
                "forward_14d": {"samples": 0, "mean_return": None, "median_return": None, "positive_rate": None},
            },
            "80+": {
                "anchors": 3,
                "forward_7d": {"samples": 3, "mean_return": 0.25, "median_return": 0.2, "positive_rate": 0.67},
                "forward_14d": {"samples": 1, "mean_return": 0.5, "median_return": 0.5, "positive_rate": 1.0},
            },
        },
    }

    out = format_report(report)

    assert "Emergence backtest" in out
    assert "nascent=2" in out
    assert "25.0%" in out
    assert "None" not in out
