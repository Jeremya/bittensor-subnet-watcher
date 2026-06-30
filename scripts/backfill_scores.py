"""Backfill stored snapshot score columns by replaying historical snapshots.

Usage:
    .venv/bin/python -m scripts.backfill_scores --db data/monitor.db
    .venv/bin/python -m scripts.backfill_scores --db data/monitor.db --write
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import config
from db.database import backup_db_file
from engine.scorer import score_snapshots
from models import SubnetSnapshot

_KNOWN_FIELDS = {field.name for field in fields(SubnetSnapshot)}
_DATETIME_FIELDS = {"polled_at", "gh_last_push", "x_last_tweet"}
_SCORE_COLUMNS = (
    "yield_score",
    "health_score",
    "momentum_score",
    "hype_score",
    "flow_score",
    "relative_value_score",
    "tradability_score",
    "catalyst_score",
    "risk_penalty",
    "swing_score",
    "composite_score",
    "price_ema_score",
    "emission_value_score",
    "protocol_context_score",
    "spec421_score",
)
_COLUMN_DEFINITIONS = {
    "yield_score": "REAL",
    "health_score": "REAL",
    "momentum_score": "REAL",
    "hype_score": "REAL",
    "flow_score": "REAL",
    "relative_value_score": "REAL",
    "tradability_score": "REAL",
    "catalyst_score": "REAL",
    "risk_penalty": "REAL",
    "swing_score": "REAL",
    "composite_score": "REAL",
    "price_ema_score": "REAL",
    "emission_value_score": "REAL",
    "protocol_context_score": "REAL",
    "spec421_score": "REAL",
}


@dataclass(frozen=True)
class BackfillSummary:
    rows_seen: int
    rows_scored: int
    rows_written: int
    spec421_scored: int
    first_polled_at: datetime | None
    last_polled_at: datetime | None


@dataclass
class SnapshotRow:
    rowid: int
    snapshot: SubnetSnapshot


def _parse_datetime(value: Any) -> Any:
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _row_to_snapshot(row: Mapping[str, Any], table_cols: Iterable[str]) -> SnapshotRow:
    data = {
        key: row[key]
        for key in table_cols
        if key in _KNOWN_FIELDS
    }
    for field in _DATETIME_FIELDS:
        if field in data:
            data[field] = _parse_datetime(data[field])
    return SnapshotRow(
        rowid=int(row["_rowid"]),
        snapshot=SubnetSnapshot(**data),
    )


def _load_snapshot_rows(conn: sqlite3.Connection) -> list[SnapshotRow]:
    table_cols = [row[1] for row in conn.execute("PRAGMA table_info(snapshots)")]
    if not table_cols:
        raise ValueError("snapshots table does not exist")
    cols = [col for col in table_cols if col in _KNOWN_FIELDS]
    if "netuid" not in cols or "polled_at" not in cols:
        raise ValueError("snapshots table must include netuid and polled_at")
    select_cols = ", ".join(f'"{col}"' for col in cols)
    rows = conn.execute(
        f"""
        SELECT rowid AS _rowid, {select_cols}
        FROM snapshots
        ORDER BY polled_at ASC, netuid ASC, rowid ASC
        """
    ).fetchall()
    return [_row_to_snapshot(row, cols) for row in rows]


def _ensure_score_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    for col, definition in _COLUMN_DEFINITIONS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {definition}")
    conn.commit()


def _write_scores(conn: sqlite3.Connection, rows: list[SnapshotRow]) -> int:
    assignments = ", ".join(f"{col} = ?" for col in _SCORE_COLUMNS)
    values = [
        tuple(getattr(row.snapshot, col) for col in _SCORE_COLUMNS) + (row.rowid,)
        for row in rows
        if row.snapshot.swing_score is not None
    ]
    if not values:
        return 0
    conn.executemany(
        f"UPDATE snapshots SET {assignments} WHERE rowid = ?",
        values,
    )
    conn.commit()
    return len(values)


def _score_chronologically(rows: list[SnapshotRow]) -> list[SnapshotRow]:
    history_by_netuid: dict[int, list[SubnetSnapshot]] = defaultdict(list)
    scored: list[SnapshotRow] = []
    index = 0
    while index < len(rows):
        polled_at = rows[index].snapshot.polled_at
        batch: list[SnapshotRow] = []
        while index < len(rows) and rows[index].snapshot.polled_at == polled_at:
            batch.append(rows[index])
            index += 1

        snapshots = [row.snapshot for row in batch]
        score_snapshots(snapshots, dict(history_by_netuid))
        scored.extend(batch)
        for row in batch:
            history_by_netuid[row.snapshot.netuid].append(row.snapshot)
    return scored


def backfill_scores(db_path: str = config.DB_PATH, *, write: bool = False) -> BackfillSummary:
    path = Path(db_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"SQLite database not found: {db_path}")

    if write:
        backup_db_file(str(path))

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        if write:
            _ensure_score_columns(conn)
        rows = _load_snapshot_rows(conn)
        scored = _score_chronologically(rows)
        rows_written = _write_scores(conn, scored) if write else 0
    finally:
        conn.close()

    scored_snapshots = [row.snapshot for row in scored]
    polled_times = [snap.polled_at for snap in scored_snapshots]
    return BackfillSummary(
        rows_seen=len(rows),
        rows_scored=sum(1 for snap in scored_snapshots if snap.swing_score is not None),
        rows_written=rows_written,
        spec421_scored=sum(1 for snap in scored_snapshots if snap.spec421_score is not None),
        first_polled_at=min(polled_times) if polled_times else None,
        last_polled_at=max(polled_times) if polled_times else None,
    )


def format_summary(summary: BackfillSummary, *, write: bool) -> str:
    mode = "write" if write else "dry-run"
    return (
        f"Score backfill {mode}: rows_seen={summary.rows_seen} "
        f"rows_scored={summary.rows_scored} rows_written={summary.rows_written} "
        f"spec421_scored={summary.spec421_scored} "
        f"range={summary.first_polled_at}..{summary.last_polled_at}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Replay historical snapshots and backfill computed score columns."
    )
    parser.add_argument("--db", default=config.DB_PATH, help="Path to the SQLite DB.")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Persist computed score columns. Without this flag, only report coverage.",
    )
    args = parser.parse_args(argv)

    summary = backfill_scores(args.db, write=args.write)
    print(format_summary(summary, write=args.write))
    if not args.write:
        print("Dry run only. Re-run with --write to update the database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
