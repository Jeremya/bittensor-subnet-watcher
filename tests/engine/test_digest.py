import pytest

from db.database import init_db
from engine.conditions import advance_condition
from engine.digest import build_daily_digest


@pytest.mark.asyncio
async def test_digest_empty_db(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        text = await build_daily_digest(db, registry={})
        assert "all clear" in text.lower()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_digest_groups_by_condition_and_marks_new(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for netuid in (7, 8):
            for _ in range(2):
                await advance_condition(db, netuid, "emission_near_zero", True, 1.0)
        for _ in range(2):
            await advance_condition(db, 9, "dead_github", True, 90.0)
        text = await build_daily_digest(db, registry={7: {"name": "Seven"}})
        assert "emission_near_zero: 2" in text
        assert "dead_github: 1" in text
        assert "Seven" in text          # registry name used
        assert "SN8" in text            # fallback name
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_digest_renders_sentinel_as_collector_health(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for _ in range(2):
            await advance_condition(db, -1, "collector_stale_github", True, 5.0)
        text = await build_daily_digest(db, registry={})
        assert "Collector health" in text
        assert "github" in text
        assert "SN-1" not in text
    finally:
        await db.close()


from datetime import datetime, timedelta, timezone

from db.database import insert_snapshot
from models import SubnetSnapshot


@pytest.mark.asyncio
async def test_digest_includes_tide_line(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        now = datetime.now(timezone.utc)
        for netuid, flow in ((1, 300.0), (2, -100.0)):
            await insert_snapshot(db, SubnetSnapshot(
                netuid=netuid, polled_at=now - timedelta(hours=1),
                net_tao_flow_tao=flow))
        text = await build_daily_digest(db, registry={})
        assert "Tide" in text and "+200" in text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_digest_includes_ignition_scorecard_when_alerts_exist(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await db.execute(
            "INSERT INTO alerts (fired_at, netuid, subnet_name, alert_type, description)"
            " VALUES (?, 7, 'X', 'pump_ignition', 'd')",
            (datetime.now(timezone.utc).isoformat(),))
        await db.commit()
        text = await build_daily_digest(db, registry={})
        assert "Ignition" in text
    finally:
        await db.close()
