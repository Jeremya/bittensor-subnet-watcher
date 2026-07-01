from datetime import datetime, timedelta, timezone

import pytest

from db.database import downsample_old_snapshots, init_db, insert_snapshot
from models import SubnetSnapshot


@pytest.mark.asyncio
async def test_downsample_thins_old_rows_keeps_recent(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        # Pin to the top of the hour: bucket counts must not depend on the
        # wall-clock minute the test happens to run at.
        anchor = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        base_old = anchor - timedelta(days=100)
        base_new = anchor - timedelta(days=5)
        for base, n in ((base_old, 8), (base_new, 8)):   # 8 rows over 2 hours, 4/hour
            for i in range(n):
                await insert_snapshot(db, SubnetSnapshot(
                    netuid=3, polled_at=base + timedelta(minutes=15 * i)))
        deleted = await downsample_old_snapshots(db, older_than_days=90)
        assert deleted == 6   # old: 8 rows in 2 hour-buckets → keep 2
        cur = await db.execute(
            "SELECT COUNT(*) FROM snapshots WHERE polled_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),))
        assert (await cur.fetchone())[0] == 2
        cur = await db.execute("SELECT COUNT(*) FROM snapshots")
        assert (await cur.fetchone())[0] == 10   # recent 8 untouched
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_downsample_keeps_one_row_per_subnet_per_hour(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        base = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                - timedelta(days=100))
        for netuid in (1, 2):
            for i in range(4):
                await insert_snapshot(db, SubnetSnapshot(
                    netuid=netuid, polled_at=base + timedelta(minutes=15 * i)))
        await downsample_old_snapshots(db, older_than_days=90)
        cur = await db.execute("SELECT netuid, COUNT(*) FROM snapshots GROUP BY netuid")
        assert {tuple(r) for r in await cur.fetchall()} == {(1, 1), (2, 1)}
    finally:
        await db.close()
