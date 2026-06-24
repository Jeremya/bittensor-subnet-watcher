from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from models import SubnetSnapshot
from scripts.backfill_emergence import (
    apply_updates,
    build_emergence_updates,
    ensure_emergence_columns,
    load_snapshot_rows,
)


def _snap(snapshot_id, netuid, when, owner, **overrides):
    snap = SubnetSnapshot(netuid=netuid, polled_at=when, owner_coldkey=owner, **overrides)
    return snapshot_id, snap


def test_build_emergence_updates_scores_rows_with_prior_history():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _snap(1, 42, now - timedelta(hours=72), "ownerA", reg_cost_tao=1.0,
              n_neurons=20, max_allowed_uids=256, net_tao_flow_tao=0.1,
              alpha_mcap_tao=1000.0, alpha_mcap_usd=100_000.0),
        _snap(2, 42, now - timedelta(hours=54), "ownerA", reg_cost_tao=2.0,
              n_neurons=60, max_allowed_uids=256, net_tao_flow_tao=0.2,
              alpha_mcap_tao=1000.0, alpha_mcap_usd=110_000.0),
        _snap(3, 42, now - timedelta(hours=36), "ownerA", reg_cost_tao=3.0,
              n_neurons=120, max_allowed_uids=256, net_tao_flow_tao=4.0,
              alpha_mcap_tao=1000.0, alpha_mcap_usd=120_000.0),
        _snap(4, 42, now - timedelta(hours=18), "ownerA", reg_cost_tao=5.0,
              n_neurons=180, max_allowed_uids=256, net_tao_flow_tao=5.0,
              alpha_mcap_tao=1000.0, alpha_mcap_usd=140_000.0),
        _snap(5, 42, now, "ownerA", reg_cost_tao=8.0,
              n_neurons=240, max_allowed_uids=256, net_tao_flow_tao=7.0,
              alpha_mcap_tao=1000.0, alpha_mcap_usd=150_000.0),
    ]

    updates = build_emergence_updates(rows, window_hours=72)

    assert len(updates) == 5
    latest = updates[-1]
    assert latest.snapshot_id == 5
    assert latest.emergence_score >= 65.0
    assert latest.reg_demand_score is not None
    assert latest.slot_fill_score is not None
    assert latest.flow_accel_score is not None
    assert latest.emergence_stage == "nascent"


def test_build_emergence_updates_resets_age_on_owner_change():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        _snap(1, 7, now - timedelta(days=90), "ownerA", alpha_mcap_usd=100_000.0),
        _snap(2, 7, now - timedelta(days=1), "ownerB", alpha_mcap_usd=100_000.0),
    ]

    updates = build_emergence_updates(rows, window_hours=72)

    assert updates[-1].snapshot_id == 2
    assert updates[-1].emergence_stage == "nascent"


def test_ensure_emergence_columns_migrates_old_snapshot_table(tmp_path):
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE snapshots (id INTEGER PRIMARY KEY, netuid INTEGER, polled_at TEXT)")
    conn.commit()
    conn.close()

    ensure_emergence_columns(db_path)

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)").fetchall()}
    conn.close()
    assert {
        "reg_demand_score",
        "slot_fill_score",
        "flow_accel_score",
        "emergence_score",
        "emergence_stage",
    } <= cols


def test_backfill_updates_legacy_table_without_id_column(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE snapshots (
            netuid INTEGER,
            polled_at TEXT,
            reg_cost_tao REAL,
            n_neurons INTEGER,
            max_allowed_uids INTEGER,
            net_tao_flow_tao REAL,
            alpha_mcap_tao REAL,
            alpha_mcap_usd REAL,
            owner_coldkey TEXT
        )
    """)
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    conn.execute(
        """
        INSERT INTO snapshots (
            netuid, polled_at, reg_cost_tao, n_neurons, max_allowed_uids,
            net_tao_flow_tao, alpha_mcap_tao, alpha_mcap_usd, owner_coldkey
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (42, now.isoformat(), 8.0, 240, 256, 6.0, 1000.0, 150_000.0, "ownerA"),
    )
    conn.commit()
    conn.close()

    rows = load_snapshot_rows(db_path)
    updates = build_emergence_updates(rows)
    count = apply_updates(db_path, updates)

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT emergence_score, emergence_stage FROM snapshots WHERE netuid=42"
    ).fetchone()
    conn.close()
    assert count == 1
    assert row[0] is not None
    assert row[1] == "nascent"
