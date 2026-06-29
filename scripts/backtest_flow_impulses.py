"""Report historical flow impulse alert volume from stored snapshots.

Usage:
    .venv/bin/python -m scripts.backtest_flow_impulses [--db PATH] [--limit-examples 10]
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

import config
from engine.flow_impulse import FlowImpulse, classify_flow_impulse
from models import SubnetSnapshot

_KNOWN_FIELDS = {field.name for field in fields(SubnetSnapshot)}
_REQUIRED_COLUMNS = {"netuid", "polled_at", "net_tao_flow_tao", "alpha_mcap_tao"}


@dataclass(frozen=True)
class ImpulseExample:
    netuid: int
    polled_at: str
    alert_type: str
    direction: str
    flow_tao: float
    relative_flow_pct: float
    price_move_pct: float | None
    impact_score: float


def _row_to_snapshot(row: Mapping[str, Any]) -> SubnetSnapshot:
    data = {key: value for key, value in dict(row).items() if key in _KNOWN_FIELDS}
    polled_at = data.get("polled_at")
    if isinstance(polled_at, str):
        data["polled_at"] = datetime.fromisoformat(polled_at)
    return SubnetSnapshot(**data)


def _readonly_uri(db_path: str) -> str:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")
    return f"file:{quote(str(path), safe='/:')}?mode=ro"


def _validate_snapshots_table(
    conn: sqlite3.Connection,
    db_path: str,
) -> list[str]:
    table_exists = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'snapshots'
        """
    ).fetchone()
    if table_exists is None:
        raise ValueError(f"snapshots table does not exist in {db_path}")

    table_cols = [row[1] for row in conn.execute("PRAGMA table_info(snapshots)")]
    missing = sorted(_REQUIRED_COLUMNS - set(table_cols))
    if missing:
        raise ValueError(
            "snapshots table missing required columns: " + ", ".join(missing)
        )
    return table_cols


def load_snapshots(db_path: str) -> list[SubnetSnapshot]:
    conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        table_cols = _validate_snapshots_table(conn, db_path)
        cols = [col for col in table_cols if col in _KNOWN_FIELDS]
        tie_breaker = "id" if "id" in table_cols else "rowid"
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM snapshots "
            f"ORDER BY polled_at ASC, netuid ASC, {tie_breaker} ASC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_snapshot(row) for row in rows]


def _cooldown_key(impulse: FlowImpulse) -> tuple[int, str]:
    return (impulse.netuid, impulse.alert_type)


def collect_impulses(
    snapshots: Iterable[SubnetSnapshot],
    *,
    cooldown_hours: int,
) -> list[tuple[SubnetSnapshot, FlowImpulse]]:
    previous_by_netuid: dict[int, SubnetSnapshot] = {}
    last_fired_at: dict[tuple[int, str], datetime] = {}
    impulses: list[tuple[SubnetSnapshot, FlowImpulse]] = []
    cooldown = timedelta(hours=cooldown_hours)

    for snap in snapshots:
        previous = previous_by_netuid.get(snap.netuid)
        impulse = classify_flow_impulse(snap, previous)
        previous_by_netuid[snap.netuid] = snap
        if impulse is None:
            continue
        key = _cooldown_key(impulse)
        fired_at = last_fired_at.get(key)
        if fired_at is not None and snap.polled_at - fired_at < cooldown:
            continue
        last_fired_at[key] = snap.polled_at
        impulses.append((snap, impulse))

    return impulses


def _example(snap: SubnetSnapshot, impulse: FlowImpulse) -> ImpulseExample:
    return ImpulseExample(
        netuid=impulse.netuid,
        polled_at=snap.polled_at.isoformat(),
        alert_type=impulse.alert_type,
        direction=impulse.direction,
        flow_tao=impulse.flow_tao,
        relative_flow_pct=impulse.relative_flow_pct,
        price_move_pct=impulse.price_move_pct,
        impact_score=impulse.impact_score,
    )


def run_backtest(
    db_path: str,
    *,
    cooldown_hours: int = config.FLOW_IMPULSE_COOLDOWN_HOURS,
    limit_examples: int = 10,
) -> dict[str, Any]:
    snapshots = load_snapshots(db_path)
    impulses = collect_impulses(snapshots, cooldown_hours=cooldown_hours)
    by_direction: dict[str, int] = {}
    by_netuid: dict[int, int] = {}
    by_day: dict[str, int] = {}

    for snap, impulse in impulses:
        by_direction[impulse.direction] = by_direction.get(impulse.direction, 0) + 1
        by_netuid[impulse.netuid] = by_netuid.get(impulse.netuid, 0) + 1
        day = snap.polled_at.date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1

    examples = sorted(
        [_example(snap, impulse) for snap, impulse in impulses],
        key=lambda item: item.impact_score,
        reverse=True,
    )[:limit_examples]

    return {
        "db_path": db_path,
        "snapshot_count": len(snapshots),
        "total_impulses": len(impulses),
        "cooldown_hours": cooldown_hours,
        "by_direction": dict(sorted(by_direction.items())),
        "by_netuid": dict(sorted(by_netuid.items())),
        "by_day": dict(sorted(by_day.items())),
        "top_examples": [asdict(example) for example in examples],
    }


def format_report(report: Mapping[str, Any]) -> str:
    buy_count = report["by_direction"].get("buy", 0)
    sell_count = report["by_direction"].get("sell", 0)
    top_netuids = sorted(
        report["by_netuid"].items(),
        key=lambda item: (-item[1], item[0]),
    )
    lines = [
        (
            "Flow impulse backtest "
            f"snapshots={report['snapshot_count']} "
            f"total_impulses={report['total_impulses']} "
            f"cooldown_hours={report['cooldown_hours']}"
        ),
        f"direction buy={buy_count} sell={sell_count}",
        "top_netuids:",
    ]
    lines.extend(
        f"  SN{netuid}={count}" for netuid, count in top_netuids[:10]
    )
    if not top_netuids:
        lines.append("  --")
    lines.append("daily_counts:")
    lines.extend(
        f"  {day}={count}" for day, count in report["by_day"].items()
    )
    if not report["by_day"]:
        lines.append("  --")
    lines.append("top_examples:")
    for example in report["top_examples"]:
        lines.append(
            "  "
            f"SN{example['netuid']} {example['polled_at']} {example['alert_type']} "
            f"flow={example['flow_tao']:+.1f} "
            f"relative={example['relative_flow_pct'] * 100:.1f}% "
            f"price={_format_price_move(example['price_move_pct'])} "
            f"impact={example['impact_score']:.0f}"
        )
    return "\n".join(lines)


def _format_price_move(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.1f}%"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest flow impulse alert volume.")
    parser.add_argument("--db", default=config.DB_PATH, help="Path to SQLite DB.")
    parser.add_argument(
        "--cooldown-hours",
        type=int,
        default=config.FLOW_IMPULSE_COOLDOWN_HOURS,
        help="Direction-specific cooldown window in hours.",
    )
    parser.add_argument(
        "--limit-examples",
        type=int,
        default=10,
        help="Number of high-impact examples to print.",
    )
    args = parser.parse_args(argv)

    report = run_backtest(
        args.db,
        cooldown_hours=args.cooldown_hours,
        limit_examples=args.limit_examples,
    )
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
