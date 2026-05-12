import pytest

from datetime import datetime, timezone

from db.database import init_db, insert_snapshot
from models import SubnetSnapshot


@pytest.mark.asyncio
async def test_new_tables_exist(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert "analyst_watchlist" in tables
    assert "analyst_mentions" in tables
    assert "subnet_milestones" in tables
    assert "collector_state" in tables
    await db.close()


@pytest.mark.asyncio
async def test_registry_has_category_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)
    cursor = await db.execute("PRAGMA table_info(subnet_registry)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "category" in cols
    assert "category_confirmed" in cols
    await db.close()


@pytest.mark.asyncio
async def test_snapshots_schema_includes_explicit_signal_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)
    cursor = await db.execute("PRAGMA table_info(snapshots)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert {
        "flow_score",
        "relative_value_score",
        "tradability_score",
        "catalyst_score",
        "risk_penalty",
        "swing_score",
        "buy_slippage_pct",
        "sell_slippage_pct",
    } <= cols
    await db.close()


@pytest.mark.asyncio
async def test_explicit_signal_fields_persist_on_snapshot_insert(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)
    snap = SubnetSnapshot(
        netuid=7,
        polled_at=datetime.now(timezone.utc),
        flow_score=61.5,
        relative_value_score=72.25,
        tradability_score=83.0,
        buy_slippage_pct=1.25,
        sell_slippage_pct=2.5,
        catalyst_score=44.75,
        risk_penalty=12.5,
        swing_score=69.25,
        composite_score=69.25,
    )

    await insert_snapshot(db, snap)
    cursor = await db.execute(
        """
        SELECT flow_score, relative_value_score, tradability_score,
               buy_slippage_pct, sell_slippage_pct,
               catalyst_score, risk_penalty, swing_score, composite_score
        FROM snapshots
        WHERE netuid = ?
        """,
        (7,),
    )
    row = await cursor.fetchone()
    assert tuple(row) == (
        61.5,
        72.25,
        83.0,
        1.25,
        2.5,
        44.75,
        12.5,
        69.25,
        69.25,
    )
    await db.close()
