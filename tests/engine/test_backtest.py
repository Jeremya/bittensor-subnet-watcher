from datetime import datetime, timedelta, timezone

import pytest

from engine.backtest import run_backtest
from models import SubnetSnapshot


def make_snap(netuid: int, *, days_ago: int, **overrides) -> SubnetSnapshot:
    data = {
        "netuid": netuid,
        "polled_at": datetime.now(timezone.utc) - timedelta(days=days_ago),
        "alpha_price_tao": 1.0,
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def test_backtest_buckets_and_forward_returns():
    rows = [
        make_snap(1, days_ago=14, swing_score=85.0, alpha_price_tao=1.0),
        make_snap(1, days_ago=7, alpha_price_tao=1.2),
        make_snap(1, days_ago=0, alpha_price_tao=1.5),
        make_snap(2, days_ago=14, swing_score=65.0, alpha_price_tao=2.0),
        make_snap(2, days_ago=7, alpha_price_tao=1.8),
        make_snap(2, days_ago=0, alpha_price_tao=1.6),
    ]

    report = run_backtest(rows)

    assert report["row_count"] == 6
    assert report["anchor_count"] == 2
    assert report["horizons_hours"] == [168, 336]
    assert report["buckets"]["80+"]["anchors"] == 1
    assert report["buckets"]["80+"]["forward_7d"]["samples"] == 1
    assert report["buckets"]["80+"]["forward_7d"]["mean_return"] == pytest.approx(0.2)
    assert report["buckets"]["80+"]["forward_14d"]["mean_return"] == pytest.approx(0.5)
    assert report["buckets"]["60-70"]["anchors"] == 1
    assert report["buckets"]["60-70"]["forward_7d"]["mean_return"] == pytest.approx(-0.1)
    assert report["buckets"]["60-70"]["forward_14d"]["mean_return"] == pytest.approx(-0.2)


def test_backtest_forward_returns_include_estimated_emission_yield():
    rows = [
        make_snap(
            1,
            days_ago=7,
            swing_score=75.0,
            alpha_price_tao=1.0,
            alpha_mcap_tao=100.0,
            daily_emission_tao=1.0,
        ),
        make_snap(
            1,
            days_ago=0,
            alpha_price_tao=1.0,
            alpha_mcap_tao=100.0,
            daily_emission_tao=1.0,
        ),
    ]

    report = run_backtest(rows)

    assert report["buckets"]["70-80"]["forward_7d"]["samples"] == 1
    assert report["buckets"]["70-80"]["forward_7d"]["mean_return"] == pytest.approx(0.07)


def test_backtest_returns_json_friendly_structure():
    report = run_backtest([])

    assert isinstance(report["generated_at"], str)
    assert report["row_count"] == 0
    assert report["anchor_count"] == 0
    assert set(report["buckets"]) == {"<50", "50-60", "60-70", "70-80", "80+"}


def test_backtest_skips_forward_matches_beyond_tolerance():
    """A data gap must not silently stretch the horizon: if the first snapshot
    at/after the 7d target is days late (e.g. the Jun 26-30 outage), that
    anchor contributes no 7d sample. An on-time 14d match still counts."""
    rows = [
        make_snap(1, days_ago=14, swing_score=85.0, alpha_price_tao=1.0),
        # gap: nothing between day-14 and day-2 → "7d" match would be 5 days late
        make_snap(1, days_ago=2, alpha_price_tao=1.2),
        make_snap(1, days_ago=0, alpha_price_tao=1.5),  # exactly 14d later: on time
    ]

    report = run_backtest(rows)

    assert report["buckets"]["80+"]["forward_7d"]["samples"] == 0
    assert report["buckets"]["80+"]["forward_14d"]["samples"] == 1
    assert report["buckets"]["80+"]["forward_14d"]["mean_return"] == pytest.approx(0.5)
