from datetime import datetime, timedelta, timezone
import sqlite3

from scripts.backtest_flow_impulses import main, run_backtest


def _create_db(path):
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

    exit_code = main(["--db", str(db_path), "--limit-examples", "2"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Flow impulse backtest" in captured.out
    assert "total_impulses=2" in captured.out
    assert "direction buy=1 sell=1" in captured.out
