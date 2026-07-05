"""Tests for fire_analyst_alerts, fire_milestone_alerts, and evaluate_convergence."""
import pytest
import aiosqlite
from datetime import datetime, timezone

from db.database import (
    SCHEMA_SQL,
    insert_analyst_mention,
    insert_milestone,
    insert_alert,
)
from engine.alerts import fire_analyst_alerts, fire_milestone_alerts, evaluate_convergence
from models import AlertRecord


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        # Seed a registry row so the alert functions can look up the subnet name
        await conn.execute(
            "INSERT OR IGNORE INTO subnet_registry (netuid, name, updated_at) VALUES (?, ?, ?)",
            (3, "Templar", datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()
        yield conn


REGISTRY = {3: {"name": "Templar"}}


@pytest.mark.asyncio
async def test_fire_analyst_alerts_inserts_alert(db):
    now = datetime.now(timezone.utc)
    await insert_analyst_mention(db, "user", 3, "http://x/1", "SN3 text", now)
    fired = await fire_analyst_alerts(db, REGISTRY)
    assert len(fired) == 1
    assert fired[0].alert_type == "analyst_mention"


@pytest.mark.asyncio
async def test_fire_analyst_alerts_respects_cooldown(db):
    now = datetime.now(timezone.utc)
    await insert_analyst_mention(db, "user", 3, "http://x/1", "SN3 text", now)
    # First call — inserts alert (sets cooldown)
    await fire_analyst_alerts(db, REGISTRY)
    # Second call within cooldown window — new mention, but cooldown blocks alert
    await insert_analyst_mention(db, "user", 3, "http://x/2", "SN3 again", now)
    fired2 = await fire_analyst_alerts(db, REGISTRY)
    assert len(fired2) == 0


@pytest.mark.asyncio
async def test_fire_analyst_alerts_marks_notified_regardless_of_cooldown(db):
    """Mentions should be marked notified even when the alert is suppressed by cooldown."""
    now = datetime.now(timezone.utc)
    await insert_analyst_mention(db, "user", 3, "http://x/1", "first", now)
    await fire_analyst_alerts(db, REGISTRY)  # fires + marks notified
    await insert_analyst_mention(db, "user", 3, "http://x/2", "second", now)
    await fire_analyst_alerts(db, REGISTRY)  # suppressed but still marks notified
    # After both calls, no unnotified rows should remain
    cursor = await db.execute("SELECT COUNT(*) FROM analyst_mentions WHERE notified=0")
    row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_fire_milestone_alerts_inserts_alert(db):
    now = datetime.now(timezone.utc)
    await insert_milestone(db, 3, "arxiv", "SparseLoCo", "https://arxiv.org/abs/1234", now)
    fired = await fire_milestone_alerts(db, REGISTRY)
    assert len(fired) == 1
    assert fired[0].alert_type == "milestone"


@pytest.mark.asyncio
async def test_fire_milestone_alerts_respects_cooldown(db):
    now = datetime.now(timezone.utc)
    await insert_milestone(db, 3, "arxiv", "Paper 1", "https://arxiv.org/abs/1", now)
    await fire_milestone_alerts(db, REGISTRY)
    await insert_milestone(db, 3, "arxiv", "Paper 2", "https://arxiv.org/abs/2", now)
    fired2 = await fire_milestone_alerts(db, REGISTRY)
    assert len(fired2) == 0


@pytest.mark.asyncio
async def test_evaluate_convergence_fires_on_two_signals(db):
    now = datetime.now(timezone.utc)
    # Seed a milestone alert and an analyst_mention alert directly in alerts table
    for alert_type in ("milestone", "analyst_mention"):
        alert = AlertRecord(
            fired_at=now,
            netuid=3,
            subnet_name="Templar",
            alert_type=alert_type,
            description="test",
            current_value=None,
            threshold=None,
        )
        await insert_alert(db, alert)
    fired = await evaluate_convergence(db, REGISTRY)
    assert len(fired) == 1
    assert fired[0].alert_type == "convergence"


@pytest.mark.asyncio
async def test_evaluate_convergence_respects_cooldown(db):
    now = datetime.now(timezone.utc)
    for alert_type in ("milestone", "analyst_mention"):
        alert = AlertRecord(
            fired_at=now,
            netuid=3,
            subnet_name="Templar",
            alert_type=alert_type,
            description="test",
            current_value=None,
            threshold=None,
        )
        await insert_alert(db, alert)
    await evaluate_convergence(db, REGISTRY)   # fires, sets 48h cooldown
    fired2 = await evaluate_convergence(db, REGISTRY)  # same signals, still in cooldown
    assert len(fired2) == 0


@pytest.mark.asyncio
async def test_fire_milestone_alerts_github_release_emoji(db):
    now = datetime.now(timezone.utc)
    await insert_milestone(db, 3, "github_release", "v2.0 — Mainnet",
                           "https://github.com/o/r/releases/tag/v2.0", now)
    fired = await fire_milestone_alerts(db, REGISTRY)
    assert len(fired) == 1
    assert fired[0].description.startswith("🚢")
