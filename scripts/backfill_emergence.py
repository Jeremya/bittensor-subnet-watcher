"""Backfill emergence scores for stored snapshot history.

Usage:
    .venv/bin/python -m scripts.backfill_emergence [--db PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any, Iterable, Mapping

import config
from db.database import backup_db_file
from engine.emergence import compute_emergence_signal
from models import SubnetSnapshot

_KNOWN_FIELDS = {f.name for f in fields(SubnetSnapshot)}
_EMERGENCE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("reg_demand_score", "REAL"),
    ("slot_fill_score", "REAL"),
    ("flow_accel_score", "REAL"),
    ("emergence_score", "REAL"),
    ("emergence_stage", "TEXT"),
)


@dataclass
class EmergenceUpdate:
    snapshot_id: int | None
    key_column: str
    key_value: int
    netuid: int
    polled_at: str
    reg_demand_score: float | None
    slot_fill_score: float | None
    flow_accel_score: float | None
    emergence_score: float
    emergence_stage: str


def _row_to_snapshot(row: Mapping[str, Any]) -> SubnetSnapshot:
    data = {key: value for key, value in dict(row).items() if key in _KNOWN_FIELDS}
    polled_at = data.get("polled_at")
    if isinstance(polled_at, str):
        data["polled_at"] = datetime.fromisoformat(polled_at)
    return SubnetSnapshot(**data)


BackfillRow = tuple[str, int, SubnetSnapshot]


def load_snapshot_rows(db_path: str) -> list[BackfillRow]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        table_cols = [row[1] for row in conn.execute("PRAGMA table_info(snapshots)")]
        key_expr = "id" if "id" in table_cols else "rowid AS __rowid"
        cols = [key_expr] + [
            col for col in table_cols if col in _KNOWN_FIELDS
        ]
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM snapshots ORDER BY netuid ASC, polled_at ASC"
        ).fetchall()
    finally:
        conn.close()
    return [
        (
            "id" if "id" in row.keys() else "rowid",
            int(row["id"]) if "id" in row.keys() else int(row["__rowid"]),
            _row_to_snapshot(row),
        )
        for row in rows
    ]


def ensure_emergence_columns(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
        for name, definition in _EMERGENCE_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE snapshots ADD COLUMN {name} {definition}")
        conn.commit()
    finally:
        conn.close()


def _unpack_backfill_row(
    row: tuple[int | None, SubnetSnapshot] | BackfillRow,
) -> BackfillRow:
    if len(row) == 2:
        snapshot_id, snap = row
        if snapshot_id is None:
            raise ValueError("2-item backfill rows require a snapshot id")
        return "id", int(snapshot_id), snap
    key_column, key_value, snap = row
    return key_column, int(key_value), snap


def build_emergence_updates(
    rows: Iterable[tuple[int | None, SubnetSnapshot] | BackfillRow],
    *,
    window_hours: int = config.EMERGENCE_WINDOW_HOURS,
) -> list[EmergenceUpdate]:
    updates: list[EmergenceUpdate] = []
    history_by_netuid: dict[int, list[SubnetSnapshot]] = {}
    owner_by_netuid: dict[int, str | None] = {}
    epoch_start_by_netuid: dict[int, datetime] = {}

    normalized = [_unpack_backfill_row(row) for row in rows]
    ordered = sorted(normalized, key=lambda item: (item[2].netuid, item[2].polled_at))
    for key_column, key_value, snap in ordered:
        history = history_by_netuid.setdefault(snap.netuid, [])
        current_owner = owner_by_netuid.get(snap.netuid)
        if snap.netuid not in epoch_start_by_netuid:
            epoch_start_by_netuid[snap.netuid] = snap.polled_at
            owner_by_netuid[snap.netuid] = snap.owner_coldkey
        elif snap.owner_coldkey is not None and snap.owner_coldkey != current_owner:
            epoch_start_by_netuid[snap.netuid] = snap.polled_at
            owner_by_netuid[snap.netuid] = snap.owner_coldkey

        signal = compute_emergence_signal(
            snap,
            history,
            first_seen_at=epoch_start_by_netuid.get(snap.netuid),
            now=snap.polled_at,
        )
        updates.append(
            EmergenceUpdate(
                snapshot_id=key_value if key_column == "id" else None,
                key_column=key_column,
                key_value=key_value,
                netuid=snap.netuid,
                polled_at=snap.polled_at.isoformat(),
                reg_demand_score=signal.reg_demand.score,
                slot_fill_score=signal.slot_fill.score,
                flow_accel_score=signal.flow_accel.score,
                emergence_score=signal.emergence_score,
                emergence_stage=signal.stage,
            )
        )
        history.append(snap)

    return updates


def apply_updates(db_path: str, updates: Iterable[EmergenceUpdate]) -> int:
    ensure_emergence_columns(db_path)
    update_list = list(updates)
    conn = sqlite3.connect(db_path)
    try:
        by_id = [update for update in update_list if update.key_column == "id"]
        by_rowid = [update for update in update_list if update.key_column == "rowid"]
        id_params = [
            (
                update.reg_demand_score,
                update.slot_fill_score,
                update.flow_accel_score,
                update.emergence_score,
                update.emergence_stage,
                update.key_value,
            )
            for update in by_id
        ]
        if id_params:
            conn.executemany(
                """
                UPDATE snapshots
                SET reg_demand_score = ?,
                    slot_fill_score = ?,
                    flow_accel_score = ?,
                    emergence_score = ?,
                    emergence_stage = ?
                WHERE id = ?
                """,
                id_params,
            )
        rowid_params = [
            (
                update.reg_demand_score,
                update.slot_fill_score,
                update.flow_accel_score,
                update.emergence_score,
                update.emergence_stage,
                update.key_value,
            )
            for update in by_rowid
        ]
        if rowid_params:
            conn.executemany(
                """
                UPDATE snapshots
                SET reg_demand_score = ?,
                    slot_fill_score = ?,
                    flow_accel_score = ?,
                    emergence_score = ?,
                    emergence_stage = ?
                WHERE rowid = ?
                """,
                rowid_params,
            )
        conn.commit()
    finally:
        conn.close()
    return len(update_list)


def summarize_updates(updates: list[EmergenceUpdate]) -> dict[str, int | float | None]:
    scored = [update for update in updates if update.emergence_score is not None]
    high = [update for update in scored if update.emergence_score >= config.EMERGENCE_WATCH_SCORE]
    return {
        "rows": len(updates),
        "scored": len(scored),
        "watch_threshold": config.EMERGENCE_WATCH_SCORE,
        "watch_candidates": len(high),
        "max_score": max((update.emergence_score for update in scored), default=None),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill emergence scores in snapshots.")
    parser.add_argument("--db", default=config.DB_PATH, help="Path to SQLite DB.")
    parser.add_argument("--dry-run", action="store_true", help="Compute but do not update the DB.")
    args = parser.parse_args(argv)

    rows = load_snapshot_rows(args.db)
    updates = build_emergence_updates(rows)
    summary = summarize_updates(updates)
    print(
        "Emergence backfill "
        f"rows={summary['rows']} scored={summary['scored']} "
        f"watch_candidates={summary['watch_candidates']} max_score={summary['max_score']}"
    )
    if args.dry_run:
        print("Dry run: no DB changes written")
        return 0

    backup = backup_db_file(args.db)
    if backup:
        print(f"Backup: {backup}")
    ensure_emergence_columns(args.db)
    count = apply_updates(args.db, updates)
    print(f"Updated {count} snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
