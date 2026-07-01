import os

import aiosqlite
import pytest

from db.database import backup_db_file, init_db


@pytest.mark.asyncio
async def test_backup_created_for_existing_nonempty_db(tmp_path):
    db_path = str(tmp_path / "monitor.db")
    conn = await init_db(db_path)
    await conn.execute(
        "INSERT INTO snapshots (netuid, polled_at) VALUES (1, '2026-01-01T00:00:00+00:00')"
    )
    await conn.commit()
    await conn.close()

    backup = backup_db_file(db_path)

    assert backup is not None
    assert os.path.exists(backup)
    bconn = await aiosqlite.connect(backup)
    cur = await bconn.execute("SELECT COUNT(*) FROM snapshots")
    assert (await cur.fetchone())[0] == 1
    await bconn.close()


@pytest.mark.asyncio
async def test_backup_skipped_for_missing_db(tmp_path):
    missing = str(tmp_path / "does_not_exist.db")
    assert backup_db_file(missing) is None


@pytest.mark.asyncio
async def test_no_backup_when_schema_current(tmp_path):
    """Second init_db on an up-to-date DB must not create a new .bak."""
    import glob
    db_path = str(tmp_path / "m.db")
    db = await init_db(db_path)
    await db.close()
    assert glob.glob(f"{db_path}.*.bak") == []   # first init: empty file, no backup
    db = await init_db(db_path)
    await db.close()
    assert glob.glob(f"{db_path}.*.bak") == []   # re-init, schema unchanged: still none
