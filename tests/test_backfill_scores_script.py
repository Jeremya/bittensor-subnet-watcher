import sqlite3
from datetime import datetime, timedelta, timezone

from scripts.backfill_scores import backfill_scores


DETERMINISTIC_SCORE_COLUMNS = (
    "flow_score",
    "relative_value_score",
    "tradability_score",
    "risk_penalty",
    "swing_score",
    "composite_score",
    "price_ema_score",
    "emission_value_score",
    "protocol_context_score",
    "spec421_score",
)


def _init_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            netuid INTEGER NOT NULL,
            polled_at TEXT NOT NULL,
            alpha_price_tao REAL,
            alpha_mcap_tao REAL,
            alpha_mcap_usd REAL,
            tao_in_tao REAL,
            volume_24h_alpha REAL,
            buy_slippage_pct REAL,
            sell_slippage_pct REAL,
            tao_usd_price REAL,
            daily_emission_tao REAL,
            emission_rank INTEGER,
            net_tao_flow_tao REAL,
            n_neurons INTEGER,
            max_allowed_uids INTEGER,
            reg_cost_tao REAL,
            owner_coldkey TEXT,
            yield_score REAL,
            health_score REAL,
            momentum_score REAL,
            hype_score REAL,
            flow_score REAL,
            relative_value_score REAL,
            tradability_score REAL,
            catalyst_score REAL,
            risk_penalty REAL,
            swing_score REAL,
            composite_score REAL,
            price_ema_score REAL,
            emission_value_score REAL,
            protocol_context_score REAL,
            spec421_score REAL
        );
        """
    )
    conn.commit()
    return conn


def _insert_snapshot(conn, *, netuid, polled_at, price, mcap_usd, emission, rank):
    conn.execute(
        """
        INSERT INTO snapshots (
            netuid, polled_at, alpha_price_tao, alpha_mcap_tao, alpha_mcap_usd,
            tao_in_tao, volume_24h_alpha, tao_usd_price, daily_emission_tao,
            emission_rank, net_tao_flow_tao, n_neurons, max_allowed_uids,
            reg_cost_tao, owner_coldkey
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            netuid,
            polled_at.isoformat(),
            price,
            mcap_usd / 300.0,
            mcap_usd,
            1000.0 + netuid,
            5000.0,
            300.0,
            emission,
            rank,
            25.0,
            120,
            256,
            1.0,
            f"owner-{netuid}",
        ),
    )


def _seed_history(conn):
    start = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    for idx in range(5):
        polled_at = start + timedelta(hours=idx)
        _insert_snapshot(
            conn,
            netuid=1,
            polled_at=polled_at,
            price=1.0 + idx * 0.08,
            mcap_usd=300_000.0,
            emission=25.0,
            rank=5,
        )
        _insert_snapshot(
            conn,
            netuid=2,
            polled_at=polled_at,
            price=1.2 - idx * 0.04,
            mcap_usd=2_000_000.0,
            emission=5.0,
            rank=40,
        )
    conn.commit()


def test_backfill_scores_dry_run_reports_missing_scores_without_writing(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = _init_db(db_path)
    _seed_history(conn)
    conn.close()

    summary = backfill_scores(str(db_path), write=False)

    assert summary.rows_seen == 10
    assert summary.rows_scored == 10
    assert summary.rows_written == 0
    assert summary.spec421_scored > 0

    conn = sqlite3.connect(db_path)
    stored = conn.execute("SELECT COUNT(*) FROM snapshots WHERE spec421_score IS NOT NULL").fetchone()[0]
    conn.close()
    assert stored == 0


def test_backfill_scores_write_persists_score_fields(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = _init_db(db_path)
    _seed_history(conn)
    conn.close()

    summary = backfill_scores(str(db_path), write=True)

    assert summary.rows_seen == 10
    assert summary.rows_scored == 10
    assert summary.rows_written == 10

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        f"SELECT {', '.join(DETERMINISTIC_SCORE_COLUMNS)}, catalyst_score "
        "FROM snapshots ORDER BY polled_at DESC, netuid LIMIT 1"
    ).fetchone()
    conn.close()

    deterministic_values = row[:-1]
    catalyst_score = row[-1]
    assert all(value is not None for value in deterministic_values)
    assert catalyst_score is None


def test_backfill_scores_write_creates_backup_before_mutating(tmp_path):
    db_path = tmp_path / "monitor.db"
    conn = _init_db(db_path)
    _seed_history(conn)
    conn.close()

    backfill_scores(str(db_path), write=True)

    backups = list(tmp_path.glob("monitor.db.*.bak"))
    assert len(backups) == 1
    conn = sqlite3.connect(backups[0])
    count = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    scored = conn.execute(
        "SELECT COUNT(*) FROM snapshots WHERE spec421_score IS NOT NULL"
    ).fetchone()[0]
    conn.close()
    assert count == 10
    assert scored == 0
