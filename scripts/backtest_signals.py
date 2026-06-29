"""Run the swing-signal backtest over the live SQLite history.

The backtest engine (``engine/backtest.py``) buckets snapshots by ``swing_score``
and falls back to ``composite_score`` when no swing score is present. This runner
loads stored snapshots, reports score-field coverage (so you know whether you are
actually backtesting the new swing signal or the legacy composite), prints a
bucket table of forward 7d/14d returns, and optionally writes the raw JSON report.

Usage:
    .venv/bin/python -m scripts.backtest_signals [--db PATH] [--output report.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import fields
from datetime import datetime
from typing import Any, Iterable, Mapping

import config
from engine.backtest import run_backtest
from models import SubnetSnapshot

_KNOWN_FIELDS = {f.name for f in fields(SubnetSnapshot)}


def rows_to_snapshots(rows: Iterable[Mapping[str, Any]]) -> list[SubnetSnapshot]:
    """Build SubnetSnapshot objects from DB rows.

    Drops columns that are not SubnetSnapshot fields (e.g. ``id``,
    ``quality_score``) and parses ISO ``polled_at`` strings into datetimes.
    """
    snapshots: list[SubnetSnapshot] = []
    for row in rows:
        data = {k: v for k, v in dict(row).items() if k in _KNOWN_FIELDS}
        polled_at = data.get("polled_at")
        if isinstance(polled_at, str):
            data["polled_at"] = datetime.fromisoformat(polled_at)
        snapshots.append(SubnetSnapshot(**data))
    return snapshots


def score_coverage(snapshots: list[SubnetSnapshot]) -> dict[str, int]:
    """Count how many snapshots carry each score field — reveals the fallback."""
    return {
        "total": len(snapshots),
        "spec421_score": sum(1 for s in snapshots if s.spec421_score is not None),
        "swing_score": sum(1 for s in snapshots if s.swing_score is not None),
        "composite_score": sum(1 for s in snapshots if s.composite_score is not None),
    }


def load_snapshots(db_path: str) -> list[SubnetSnapshot]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        table_cols = [r[1] for r in conn.execute("PRAGMA table_info(snapshots)")]
        cols = [c for c in table_cols if c in _KNOWN_FIELDS]
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM snapshots").fetchall()
    finally:
        conn.close()
    return rows_to_snapshots(rows)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def format_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    lines.append(
        f"Backtest report  rows={report['row_count']}  anchors={report['anchor_count']}"
        f"  horizons={report['horizons_hours']}h"
    )
    lines.append(f"generated_at={report['generated_at']}")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest swing/composite signals over stored history.")
    parser.add_argument("--db", default=config.DB_PATH, help="Path to the SQLite DB.")
    parser.add_argument("--output", help="Optional path to write the raw JSON report.")
    args = parser.parse_args(argv)

    snapshots = load_snapshots(args.db)
    coverage = score_coverage(snapshots)
    scored = "swing_score" if coverage["swing_score"] else "composite_score (legacy fallback)"
    print(
        f"Loaded {coverage['total']} snapshots — "
        f"spec421_score={coverage['spec421_score']}, "
        f"swing_score={coverage['swing_score']}, composite_score={coverage['composite_score']}"
    )
    print(f"Backtesting against: {scored}\n")

    report = run_backtest(snapshots)
    print(format_report(report))

    if args.output:
        with open(args.output, "w") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nWrote raw report to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
