import pytest
import aiosqlite
from db.database import (
    init_db, upsert_portfolio_position, delete_gone_positions,
    get_portfolio_positions, get_staked_netuids,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(str(tmp_path / "test.db"))
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_upsert_creates_position(db):
    await upsert_portfolio_position(db, "ck1", 1, 100.0, 5.0)
    rows = await get_portfolio_positions(db)
    assert len(rows) == 1
    assert rows[0]["coldkey"] == "ck1"
    assert rows[0]["netuid"] == 1
    assert rows[0]["alpha_amount"] == pytest.approx(100.0)
    assert rows[0]["tao_value"] == pytest.approx(5.0)
    assert rows[0]["baseline_tao_value"] == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_upsert_preserves_baseline_on_update(db):
    await upsert_portfolio_position(db, "ck1", 1, 100.0, 5.0)
    await upsert_portfolio_position(db, "ck1", 1, 120.0, 6.5)
    rows = await get_portfolio_positions(db)
    assert rows[0]["tao_value"] == pytest.approx(6.5)
    assert rows[0]["alpha_amount"] == pytest.approx(120.0)
    assert rows[0]["baseline_tao_value"] == pytest.approx(5.0)  # frozen


@pytest.mark.asyncio
async def test_upsert_baseline_updates_from_zero(db):
    """If first insert had tao_value=0 (missing price), baseline should update when price arrives."""
    await upsert_portfolio_position(db, "ck1", 1, 100.0, 0.0)
    rows = await get_portfolio_positions(db)
    assert rows[0]["baseline_tao_value"] == pytest.approx(0.0)

    await upsert_portfolio_position(db, "ck1", 1, 100.0, 5.0)
    rows = await get_portfolio_positions(db)
    assert rows[0]["baseline_tao_value"] == pytest.approx(5.0)  # updated from 0


@pytest.mark.asyncio
async def test_delete_gone_positions_removes_absent(db):
    await upsert_portfolio_position(db, "ck1", 1, 100.0, 5.0)
    await upsert_portfolio_position(db, "ck1", 2, 50.0, 2.5)
    await upsert_portfolio_position(db, "ck1", 3, 30.0, 1.5)

    await delete_gone_positions(db, "ck1", {1, 3})  # netuid 2 is gone
    rows = await get_portfolio_positions(db)
    netuids = {r["netuid"] for r in rows}
    assert netuids == {1, 3}


@pytest.mark.asyncio
async def test_delete_gone_positions_empty_set_removes_all(db):
    await upsert_portfolio_position(db, "ck1", 1, 100.0, 5.0)
    await delete_gone_positions(db, "ck1", set())
    rows = await get_portfolio_positions(db)
    assert rows == []


@pytest.mark.asyncio
async def test_delete_gone_positions_only_affects_own_coldkey(db):
    await upsert_portfolio_position(db, "ck1", 1, 100.0, 5.0)
    await upsert_portfolio_position(db, "ck2", 1, 50.0, 2.5)

    await delete_gone_positions(db, "ck1", set())  # unstake all for ck1
    rows = await get_portfolio_positions(db)
    assert len(rows) == 1
    assert rows[0]["coldkey"] == "ck2"


@pytest.mark.asyncio
async def test_get_staked_netuids(db):
    await upsert_portfolio_position(db, "ck1", 1, 100.0, 5.0)
    await upsert_portfolio_position(db, "ck1", 5, 50.0, 2.5)
    await upsert_portfolio_position(db, "ck2", 5, 30.0, 1.5)

    netuids = await get_staked_netuids(db)
    assert netuids == {1, 5}


@pytest.mark.asyncio
async def test_get_staked_netuids_empty(db):
    netuids = await get_staked_netuids(db)
    assert netuids == set()
