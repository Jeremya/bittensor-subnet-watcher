from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any, Iterable, Mapping

from models import SubnetSnapshot


BUCKETS: list[tuple[str, float | None, float | None]] = [
    ("<50", None, 50.0),
    ("50-60", 50.0, 60.0),
    ("60-70", 60.0, 70.0),
    ("70-80", 70.0, 80.0),
    ("80+", 80.0, None),
]


def _as_snapshot(row: SubnetSnapshot | Mapping[str, Any]) -> SubnetSnapshot:
    if isinstance(row, SubnetSnapshot):
        return row
    return SubnetSnapshot(**dict(row))


def _score(snapshot: SubnetSnapshot) -> float | None:
    return snapshot.swing_score if snapshot.swing_score is not None else snapshot.composite_score


def _bucket_for_score(score: float | None) -> str | None:
    if score is None:
        return None
    for label, low, high in BUCKETS:
        if low is not None and score < low:
            continue
        if high is not None and score >= high:
            continue
        return label
    return None


def _forward_return(anchor: SubnetSnapshot, future: SubnetSnapshot) -> float | None:
    if (
        anchor.alpha_price_tao is None
        or future.alpha_price_tao is None
        or anchor.alpha_price_tao <= 0
    ):
        return None
    return future.alpha_price_tao / anchor.alpha_price_tao - 1.0


def _summarize(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {
            "samples": 0,
            "mean_return": None,
            "median_return": None,
            "positive_rate": None,
        }
    return {
        "samples": len(values),
        "mean_return": round(mean(values), 6),
        "median_return": round(median(values), 6),
        "positive_rate": round(sum(1 for value in values if value > 0) / len(values), 6),
    }


def _build_bucket_metrics() -> dict[str, dict[str, Any]]:
    return {
        label: {
            "anchors": 0,
            "forward_7d": [],
            "forward_14d": [],
        }
        for label, _, _ in BUCKETS
    }


def run_backtest(
    rows: Iterable[SubnetSnapshot | Mapping[str, Any]],
    *,
    horizons_hours: tuple[int, int] = (168, 336),
) -> dict[str, Any]:
    snapshots = [_as_snapshot(row) for row in rows]
    series_by_netuid: dict[int, list[SubnetSnapshot]] = defaultdict(list)
    for snap in snapshots:
        series_by_netuid[snap.netuid].append(snap)

    for series in series_by_netuid.values():
        series.sort(key=lambda snap: snap.polled_at)

    bucket_metrics = _build_bucket_metrics()
    total_anchors = 0

    for series in series_by_netuid.values():
        for idx, anchor in enumerate(series):
            score = _score(anchor)
            bucket = _bucket_for_score(score)
            if bucket is None:
                continue

            bucket_metrics[bucket]["anchors"] += 1
            total_anchors += 1

            for horizon_hours in horizons_hours:
                target = anchor.polled_at + timedelta(hours=horizon_hours)
                future = next(
                    (snap for snap in series[idx + 1 :] if snap.polled_at >= target),
                    None,
                )
                if future is None:
                    continue
                ret = _forward_return(anchor, future)
                if ret is None:
                    continue
                bucket_metrics[bucket][f"forward_{horizon_hours // 24}d"].append(ret)

    report_buckets: dict[str, dict[str, Any]] = {}
    for label, data in bucket_metrics.items():
        report_buckets[label] = {
            "anchors": data["anchors"],
            "forward_7d": _summarize(data["forward_7d"]),
            "forward_14d": _summarize(data["forward_14d"]),
        }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(snapshots),
        "anchor_count": total_anchors,
        "buckets": report_buckets,
        "horizons_hours": list(horizons_hours),
    }
