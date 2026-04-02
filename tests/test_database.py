# tests/test_database.py
import pytest
import aiosqlite
from datetime import datetime, timezone, timedelta
from models import SubnetSnapshot, AlertRecord
from db.database import SCHEMA_SQL, insert_snapshot, get_latest_snapshots, \
    insert_alert, get_unsent_alerts, mark_alerts_sent, is_alert_in_cooldown, \
    prune_old_snapshots


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


async def test_insert_and_get_snapshot(db):
    now = datetime.now(timezone.utc)
    snap = SubnetSnapshot(netuid=1, polled_at=now, alpha_price_tao=0.0135,
                          alpha_mcap_tao=32433.0, composite_score=75.0)
    await insert_snapshot(db, snap)
    rows = await get_latest_snapshots(db)
    assert len(rows) == 1
    assert rows[0]["netuid"] == 1
    assert rows[0]["composite_score"] == pytest.approx(75.0)


async def test_get_latest_snapshots_returns_one_per_netuid(db):
    now = datetime.now(timezone.utc)
    for i in range(3):
        snap = SubnetSnapshot(netuid=1, polled_at=now + timedelta(minutes=i),
                              composite_score=float(i))
        await insert_snapshot(db, snap)
    await insert_snapshot(db, SubnetSnapshot(netuid=2, polled_at=now, composite_score=50.0))
    rows = await get_latest_snapshots(db)
    assert len(rows) == 2  # one per netuid
    sn1 = next(r for r in rows if r["netuid"] == 1)
    assert sn1["composite_score"] == 2.0  # latest


async def test_insert_alert_and_get_unsent(db):
    now = datetime.now(timezone.utc)
    alert = AlertRecord(fired_at=now, netuid=42, subnet_name="Chutes",
                        alert_type="emission_divergence", description="ratio 3.0x",
                        current_value=3.0, threshold=1.5)
    await insert_alert(db, alert)
    unsent = await get_unsent_alerts(db)
    assert len(unsent) == 1
    assert unsent[0]["alert_type"] == "emission_divergence"


async def test_mark_alerts_sent(db):
    now = datetime.now(timezone.utc)
    alert = AlertRecord(fired_at=now, netuid=1, subnet_name="Apex",
                        alert_type="new_entry", description="new")
    await insert_alert(db, alert)
    unsent = await get_unsent_alerts(db)
    ids = [row["id"] for row in unsent]
    await mark_alerts_sent(db, ids)
    assert len(await get_unsent_alerts(db)) == 0


async def test_alert_cooldown(db):
    now = datetime.now(timezone.utc)
    alert = AlertRecord(fired_at=now, netuid=1, subnet_name="Apex",
                        alert_type="emission_divergence", description="x")
    await insert_alert(db, alert)
    # Same type within 6 hours — should be in cooldown
    in_cooldown = await is_alert_in_cooldown(db, netuid=1,
                                              alert_type="emission_divergence",
                                              cooldown_hours=6)
    assert in_cooldown is True
    # Different type — not in cooldown
    not_cool = await is_alert_in_cooldown(db, netuid=1,
                                           alert_type="dead_github",
                                           cooldown_hours=6)
    assert not_cool is False


async def test_prune_old_snapshots(db):
    old = datetime.now(timezone.utc) - timedelta(days=31)
    recent = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=old))
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=recent))
    await prune_old_snapshots(db, days=30)
    rows = await get_latest_snapshots(db)
    assert len(rows) == 1  # old row pruned
