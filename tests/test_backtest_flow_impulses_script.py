from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from models import SubnetSnapshot
from scripts.backtest_flow_impulses import (
    collect_impulses,
    load_snapshots,
    main,
    run_backtest,
)


def _create_db(path, rows=None):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY,
            netuid INTEGER NOT NULL,
            polled_at TEXT NOT NULL,
            alpha_price_tao REAL,
            alpha_mcap_tao REAL,
            alpha_mcap_usd REAL,
            volume_24h_alpha REAL,
            buy_slippage_pct REAL,
            sell_slippage_pct REAL,
            net_tao_flow_tao REAL
        )
        """
    )
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    if rows is None:
        rows = [
            (1, 101, now - timedelta(minutes=15), 1.0, 1000.0, 100_000.0, 100.0, 3.0, 4.0, 0.0),
            (2, 101, now, 1.02, 1000.0, 100_000.0, 100.0, 3.0, 4.0, 60.0),
            (3, 102, now - timedelta(minutes=15), 1.0, 1000.0, 100_000.0, 100.0, 3.0, 4.0, 0.0),
            (4, 102, now, 0.985, 1000.0, 100_000.0, 100.0, 3.0, 4.0, -40.0),
        ]
    conn.executemany(
        """
        INSERT INTO snapshots (
            id, netuid, polled_at, alpha_price_tao, alpha_mcap_tao, alpha_mcap_usd,
            volume_24h_alpha, buy_slippage_pct, sell_slippage_pct, net_tao_flow_tao
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [(row[0], row[1], row[2].isoformat(), *row[3:]) for row in rows],
    )
    conn.commit()
    conn.close()


def _snap(netuid, minutes, flow, *, price=1.0):
    return SubnetSnapshot(
        netuid=netuid,
        polled_at=datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
        + timedelta(minutes=minutes),
        alpha_price_tao=price,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        volume_24h_alpha=100.0,
        buy_slippage_pct=3.0,
        sell_slippage_pct=4.0,
        net_tao_flow_tao=flow,
    )


def test_run_backtest_reports_buy_and_sell_counts(tmp_path):
    db_path = tmp_path / "flow.db"
    _create_db(db_path)

    report = run_backtest(str(db_path), cooldown_hours=2, limit_examples=5)

    assert report["total_impulses"] == 2
    assert report["by_direction"] == {"buy": 1, "sell": 1}
    assert report["by_netuid"][101] == 1
    assert report["by_netuid"][102] == 1
    assert report["top_examples"][0]["impact_score"] >= report["top_examples"][1]["impact_score"]


def test_main_prints_summary(tmp_path, capsys):
    db_path = tmp_path / "flow.db"
    _create_db(db_path)

    exit_code = main([
        "--db",
        str(db_path),
        "--limit-examples",
        "2",
        "--cooldown-hours",
        "4",
    ])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Flow impulse backtest" in captured.out
    assert "total_impulses=2" in captured.out
    assert "cooldown_hours=4" in captured.out
    assert "direction buy=1 sell=1" in captured.out
    assert "top_netuids:" in captured.out
    assert "SN101=1" in captured.out
    assert "SN102=1" in captured.out
    assert "daily_counts:" in captured.out
    assert "2026-06-25=2" in captured.out
    assert "price=+2.0%" in captured.out


def test_main_formats_missing_price_as_placeholder(tmp_path, capsys):
    db_path = tmp_path / "flow.db"
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    _create_db(
        db_path,
        rows=[
            (1, 101, now, 1.0, 1000.0, 100_000.0, 100.0, 3.0, 4.0, 60.0),
        ],
    )

    exit_code = main(["--db", str(db_path), "--limit-examples", "1"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "price=--" in captured.out


def test_load_snapshots_missing_db_does_not_create_file(tmp_path):
    db_path = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError, match="SQLite database not found"):
        load_snapshots(str(db_path))

    assert not db_path.exists()


def test_load_snapshots_missing_snapshots_table_raises_clear_error(tmp_path):
    db_path = tmp_path / "flow.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="snapshots table does not exist"):
        load_snapshots(str(db_path))


def test_load_snapshots_missing_required_columns_raises_clear_error(tmp_path):
    db_path = tmp_path / "flow.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            netuid INTEGER NOT NULL,
            polled_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="snapshots table missing required columns"):
        load_snapshots(str(db_path))


def test_collect_impulses_suppresses_same_type_within_cooldown():
    snapshots = [
        _snap(201, 0, 0.0, price=1.0),
        _snap(201, 15, 60.0, price=1.02),
        _snap(201, 60, 70.0, price=1.04),
    ]

    impulses = collect_impulses(snapshots, cooldown_hours=2)

    assert len(impulses) == 1
    assert impulses[0][1].alert_type == "important_buy"


def test_collect_impulses_allows_same_type_after_cooldown():
    snapshots = [
        _snap(201, 0, 0.0, price=1.0),
        _snap(201, 15, 60.0, price=1.02),
        _snap(201, 195, 70.0, price=1.04),
    ]

    impulses = collect_impulses(snapshots, cooldown_hours=2)

    assert [impulse.alert_type for _, impulse in impulses] == [
        "important_buy",
        "important_buy",
    ]


def test_collect_impulses_does_not_suppress_opposite_direction():
    snapshots = [
        _snap(201, 0, 0.0, price=1.0),
        _snap(201, 15, 60.0, price=1.02),
        _snap(201, 30, -40.0, price=0.98),
    ]

    impulses = collect_impulses(snapshots, cooldown_hours=2)

    assert [impulse.alert_type for _, impulse in impulses] == [
        "important_buy",
        "important_sell",
    ]
