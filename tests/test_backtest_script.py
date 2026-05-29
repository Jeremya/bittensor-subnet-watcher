from datetime import datetime, timezone

from models import SubnetSnapshot
from scripts.backtest_signals import (
    rows_to_snapshots,
    score_coverage,
    format_report,
)


def test_rows_to_snapshots_filters_unknown_columns_and_parses_datetime():
    rows = [
        {
            "id": 1,                  # not a SubnetSnapshot field
            "quality_score": 50.0,    # legacy DB column not in the dataclass
            "netuid": 3,
            "polled_at": "2026-04-12T15:34:45.870170+00:00",
            "alpha_price_tao": 1.5,
            "composite_score": 72.0,
        }
    ]

    snaps = rows_to_snapshots(rows)

    assert len(snaps) == 1
    snap = snaps[0]
    assert isinstance(snap, SubnetSnapshot)
    assert snap.netuid == 3
    assert isinstance(snap.polled_at, datetime)
    assert snap.polled_at.tzinfo is not None
    assert snap.composite_score == 72.0


def test_rows_to_snapshots_passes_through_datetime_polled_at():
    now = datetime.now(timezone.utc)
    snaps = rows_to_snapshots([{"netuid": 1, "polled_at": now, "alpha_price_tao": 1.0}])
    assert snaps[0].polled_at == now


def test_score_coverage_counts_swing_and_composite_separately():
    snaps = [
        SubnetSnapshot(netuid=1, polled_at=datetime.now(timezone.utc), composite_score=70.0),
        SubnetSnapshot(netuid=2, polled_at=datetime.now(timezone.utc), composite_score=60.0, swing_score=80.0),
        SubnetSnapshot(netuid=3, polled_at=datetime.now(timezone.utc)),
    ]

    coverage = score_coverage(snaps)

    assert coverage["composite_score"] == 2
    assert coverage["swing_score"] == 1
    assert coverage["total"] == 3


def test_format_report_renders_buckets_and_handles_empty_summaries():
    report = {
        "generated_at": "2026-05-29T00:00:00+00:00",
        "row_count": 100,
        "anchor_count": 40,
        "horizons_hours": [168, 336],
        "buckets": {
            "<50": {
                "anchors": 5,
                "forward_7d": {"samples": 0, "mean_return": None, "median_return": None, "positive_rate": None},
                "forward_14d": {"samples": 0, "mean_return": None, "median_return": None, "positive_rate": None},
            },
            "80+": {
                "anchors": 10,
                "forward_7d": {"samples": 8, "mean_return": 0.12, "median_return": 0.1, "positive_rate": 0.75},
                "forward_14d": {"samples": 6, "mean_return": 0.2, "median_return": 0.18, "positive_rate": 0.83},
            },
        },
    }

    out = format_report(report)

    # Both buckets appear
    assert "<50" in out
    assert "80+" in out
    # Header context
    assert "100" in out  # row_count
    # Populated bucket shows a percentage-formatted return
    assert "12.0%" in out or "12%" in out
    # Empty summary renders a placeholder rather than "None"
    assert "None" not in out
