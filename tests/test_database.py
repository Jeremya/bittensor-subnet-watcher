# tests/test_database.py
import pytest
import aiosqlite
from datetime import datetime, timezone, timedelta
from models import SubnetSnapshot, AlertRecord
from db.database import SCHEMA_SQL, insert_snapshot, get_latest_snapshots, \
    insert_alert, get_unsent_alerts, mark_alerts_sent, is_alert_in_cooldown, \
    prune_old_snapshots, upsert_registry_entry, \
    get_latest_snapshots_with_registry, get_emission_rank_24h_ago, \
    get_subnet_detail, get_alerts_for_netuid


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


async def test_insert_snapshot_persists_spec421_fields(db):
    now = datetime.now(timezone.utc)
    snap = SubnetSnapshot(
        netuid=1,
        polled_at=now,
        price_ema_score=72.5,
        emission_value_score=64.0,
        protocol_context_score=58.5,
        spec421_score=66.25,
    )
    await insert_snapshot(db, snap)

    rows = await get_latest_snapshots(db)
    assert len(rows) == 1
    assert rows[0]["price_ema_score"] == pytest.approx(72.5)
    assert rows[0]["emission_value_score"] == pytest.approx(64.0)
    assert rows[0]["protocol_context_score"] == pytest.approx(58.5)
    assert rows[0]["spec421_score"] == pytest.approx(66.25)


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


# ── New DB functions ───────────────────────────────────────────────────────────

async def test_get_latest_snapshots_with_registry_includes_name(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              composite_score=80.0))
    await upsert_registry_entry(db, 1, "Apex", "https://github.com/apex/sn",
                                 "apex_subnet", "https://apex.ai")
    rows = await get_latest_snapshots_with_registry(db)
    assert len(rows) == 1
    assert rows[0]["name"] == "Apex"
    assert rows[0]["x_handle"] == "apex_subnet"


async def test_registry_upsert_preserves_supplemental_fields_when_refresh_lacks_them(db):
    await upsert_registry_entry(
        db,
        1,
        "Apex",
        "https://github.com/apex/sn",
        "apex_subnet",
        "https://apex.ai",
    )

    await upsert_registry_entry(db, 1, "Apex Renamed", None, None, None)
    cursor = await db.execute("SELECT * FROM subnet_registry WHERE netuid=?", (1,))
    row = await cursor.fetchone()
    assert row["name"] == "Apex Renamed"
    assert row["github_url"] == "https://github.com/apex/sn"
    assert row["x_handle"] == "apex_subnet"
    assert row["website"] == "https://apex.ai"

    await upsert_registry_entry(
        db,
        1,
        "Apex Renamed",
        "https://github.com/apex/new",
        "apex_new",
        "https://new.apex.ai",
    )
    cursor = await db.execute("SELECT * FROM subnet_registry WHERE netuid=?", (1,))
    row = await cursor.fetchone()
    assert row["github_url"] == "https://github.com/apex/new"
    assert row["x_handle"] == "apex_new"
    assert row["website"] == "https://new.apex.ai"


async def test_get_latest_snapshots_with_registry_name_none_without_registry(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=42, polled_at=now,
                                              composite_score=50.0))
    rows = await get_latest_snapshots_with_registry(db)
    assert rows[0]["name"] is None  # no registry entry → LEFT JOIN produces NULL


async def test_get_emission_rank_24h_ago_returns_old_rank(db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=25)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=old,
                                              emission_rank=5))
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              emission_rank=3))
    result = await get_emission_rank_24h_ago(db)
    assert result[1] == 5  # returns the OLD rank, not the current one


async def test_get_emission_rank_24h_ago_ignores_recent_only(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              emission_rank=3))
    result = await get_emission_rank_24h_ago(db)
    assert 1 not in result  # no snapshot older than 24h → nothing returned


async def test_get_subnet_detail_includes_registry_data(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=5, polled_at=now,
                                              composite_score=60.0,
                                              alpha_mcap_usd=1_200_000.0))
    await upsert_registry_entry(db, 5, "Vision", None, None, "https://vision.ai")
    row = await get_subnet_detail(db, 5)
    assert row is not None
    assert row["name"] == "Vision"
    assert row["composite_score"] == pytest.approx(60.0)


async def test_get_alerts_for_netuid_filters_correctly(db):
    now = datetime.now(timezone.utc)
    for netuid in [1, 2, 1]:
        await insert_alert(db, AlertRecord(
            fired_at=now, netuid=netuid, subnet_name=f"SN{netuid}",
            alert_type="new_entry", description="x"
        ))
    rows = await get_alerts_for_netuid(db, 1, limit=10)
    assert len(rows) == 2
    assert all(r["netuid"] == 1 for r in rows)
