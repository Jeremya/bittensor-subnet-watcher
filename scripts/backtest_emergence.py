"""Backtest historical emergence scores against forward price returns.

Usage:
    .venv/bin/python -m scripts.backtest_emergence [--db PATH] [--output report.json]
"""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any, Iterable, Mapping

import config
from engine.backtest import BUCKETS
from models import SubnetSnapshot
from scripts.backtest_signals import load_snapshots


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


def _empty_bucket_metrics() -> dict[str, dict[str, Any]]:
    return {
        label: {"anchors": 0, "forward_7d": [], "forward_14d": []}
        for label, _, _ in BUCKETS
    }


def run_emergence_backtest(
    rows: Iterable[SubnetSnapshot],
    *,
    horizons_hours: tuple[int, int] = (168, 336),
    include_established: bool = False,
) -> dict[str, Any]:
    snapshots = list(rows)
    stage_counts = Counter(snap.emergence_stage or "unknown" for snap in snapshots)
    series_by_netuid: dict[int, list[SubnetSnapshot]] = defaultdict(list)
    for snap in snapshots:
        series_by_netuid[snap.netuid].append(snap)
    for series in series_by_netuid.values():
        series.sort(key=lambda snap: snap.polled_at)

    bucket_metrics = _empty_bucket_metrics()
    total_anchors = 0

    for series in series_by_netuid.values():
        for idx, anchor in enumerate(series):
            if not include_established and anchor.emergence_stage == "established":
                continue
            bucket = _bucket_for_score(anchor.emergence_score)
            if bucket is None:
                continue

            bucket_metrics[bucket]["anchors"] += 1
            total_anchors += 1
            for horizon_hours in horizons_hours:
                target = anchor.polled_at + timedelta(hours=horizon_hours)
                future = next(
                    (snap for snap in series[idx + 1:] if snap.polled_at >= target),
                    None,
                )
                if future is None:
                    continue
                ret = _forward_return(anchor, future)
                if ret is None:
                    continue
                bucket_metrics[bucket][f"forward_{horizon_hours // 24}d"].append(ret)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(snapshots),
        "anchor_count": total_anchors,
        "stage_counts": dict(stage_counts),
        "buckets": {
            label: {
                "anchors": data["anchors"],
                "forward_7d": _summarize(data["forward_7d"]),
                "forward_14d": _summarize(data["forward_14d"]),
            }
            for label, data in bucket_metrics.items()
        },
        "horizons_hours": list(horizons_hours),
    }


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _rate(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def format_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"Emergence backtest  rows={report['row_count']}  anchors={report['anchor_count']}"
        f"  horizons={report['horizons_hours']}h"
    )
    lines.append(f"generated_at={report['generated_at']}")
    stages = " ".join(
        f"{stage}={count}" for stage, count in sorted(report["stage_counts"].items())
    )
    lines.append(f"stages: {stages or '-'}")
    lines.append("")
    header = (
        f"{'bucket':>7} {'anchors':>8} "
        f"{'7d mean':>9} {'7d median':>10} {'7d +rate':>9} {'7d n':>5}  "
        f"{'14d mean':>9} {'14d median':>10} {'14d +rate':>9} {'14d n':>5}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for label, data in report["buckets"].items():
        f7 = data["forward_7d"]
        f14 = data["forward_14d"]
        lines.append(
            f"{label:>7} {data['anchors']:>8} "
            f"{_pct(f7['mean_return']):>9} {_pct(f7['median_return']):>10} "
            f"{_rate(f7['positive_rate']):>9} {f7['samples']:>5}  "
            f"{_pct(f14['mean_return']):>9} {_pct(f14['median_return']):>10} "
            f"{_rate(f14['positive_rate']):>9} {f14['samples']:>5}"
        )
    return "\n".join(lines)


def emergence_coverage(snapshots: list[SubnetSnapshot]) -> dict[str, int]:
    return {
        "total": len(snapshots),
        "emergence_score": sum(1 for snap in snapshots if snap.emergence_score is not None),
        "non_established": sum(
            1 for snap in snapshots
            if snap.emergence_score is not None and snap.emergence_stage != "established"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest emergence scores over stored history.")
    parser.add_argument("--db", default=config.DB_PATH, help="Path to the SQLite DB.")
    parser.add_argument("--output", help="Optional path to write the raw JSON report.")
    parser.add_argument(
        "--include-established",
        action="store_true",
        help="Include established-stage rows in bucket metrics.",
    )
    args = parser.parse_args(argv)

    snapshots = load_snapshots(args.db)
    coverage = emergence_coverage(snapshots)
    print(
        f"Loaded {coverage['total']} snapshots - "
        f"emergence_score={coverage['emergence_score']}, "
        f"non_established={coverage['non_established']}"
    )
    report = run_emergence_backtest(
        snapshots,
        include_established=args.include_established,
    )
    print(format_report(report))
    if args.output:
        with open(args.output, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nWrote raw report to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
