import pytest

from db.database import init_db


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
