from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from collectors.locks import LockCollector
from db.database import SCHEMA_SQL, get_owner_locks_for_netuid, insert_snapshot
from models import SubnetSnapshot


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


def _snap(netuid, owner="owner1", price=0.05, mcap_tao=1000.0):
    return SubnetSnapshot(
        netuid=netuid, polled_at=datetime.now(timezone.utc),
        owner_coldkey=owner, alpha_price_tao=price, alpha_mcap_tao=mcap_tao)


def _balance(alpha: float):
    return SimpleNamespace(tao=alpha)      # Balance exposes .tao as unit float


@pytest.mark.asyncio
async def test_collect_stores_lock_with_derived_values(db):
    await insert_snapshot(db, _snap(51, price=0.05, mcap_tao=1000.0))  # supply 20k
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(
        return_value={"locked_mass": _balance(4000.0), "conviction": 1.0,
                      "last_update": 8_557_923}))
    ok = await LockCollector.collect(st, db)
    assert ok == 1
    rows = await get_owner_locks_for_netuid(db, 51)
    assert rows[0]["locked_alpha"] == 4000.0
    assert rows[0]["locked_tao"] == pytest.approx(200.0)    # 4000 * 0.05
    assert rows[0]["locked_pct"] == pytest.approx(0.20)     # 4000 / 20000


@pytest.mark.asyncio
async def test_collect_none_lock_is_measured_zero(db):
    await insert_snapshot(db, _snap(51))
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(return_value=None))
    ok = await LockCollector.collect(st, db)
    assert ok == 1
    rows = await get_owner_locks_for_netuid(db, 51)
    assert rows[0]["locked_alpha"] == 0.0


@pytest.mark.asyncio
async def test_collect_error_skips_without_row(db):
    await insert_snapshot(db, _snap(51))
    await insert_snapshot(db, _snap(52, owner="owner2"))
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(
        side_effect=[RuntimeError("rpc"), None]))
    ok = await LockCollector.collect(st, db)
    assert ok == 1                                          # one skipped, one zero
    empty_51 = await get_owner_locks_for_netuid(db, 51) == []
    empty_52 = await get_owner_locks_for_netuid(db, 52) == []
    assert empty_51 or empty_52


@pytest.mark.asyncio
async def test_collect_skips_missing_owner_and_handles_bad_price(db):
    await insert_snapshot(db, _snap(60, owner=None))         # no owner: skipped
    await insert_snapshot(db, _snap(61, price=None))         # lock stored, tao/pct NULL
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(
        return_value={"locked_mass": _balance(10.0), "conviction": 1.0,
                      "last_update": 1}))
    ok = await LockCollector.collect(st, db)
    assert ok == 1
    rows = await get_owner_locks_for_netuid(db, 61)
    assert rows[0]["locked_alpha"] == 10.0
    assert rows[0]["locked_tao"] is None and rows[0]["locked_pct"] is None
