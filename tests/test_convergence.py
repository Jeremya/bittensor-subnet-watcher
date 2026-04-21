from datetime import datetime, timezone

import aiosqlite
import pytest

from db.database import (
    SCHEMA_SQL,
    get_unnotified_analyst_mentions,
    get_unnotified_milestones,
    insert_alert,
    insert_analyst_mention,
    insert_milestone,
)
from engine.alerts import (
    _count_convergence_signals,
    evaluate_convergence,
    fire_analyst_alerts,
    fire_milestone_alerts,
)
from models import AlertRecord


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


def test_two_distinct_signals_triggers():
    signals_by_netuid = {3: {"milestone", "analyst_mention"}}
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 in result
    assert result[3] == {"milestone", "analyst_mention"}


def test_one_signal_does_not_trigger():
    signals_by_netuid = {3: {"milestone"}}
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 not in result


def test_three_signals_triggers():
    signals_by_netuid = {3: {"milestone", "analyst_mention", "whale_inflow"}}
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 in result


def test_multiple_netuids_filtered_correctly():
    signals_by_netuid = {
        3: {"milestone", "analyst_mention"},
        56: {"github_spike"},
        13: {"whale_inflow", "milestone", "analyst_mention"},
    }
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 in result
    assert 56 not in result
    assert 13 in result


def test_empty_input_returns_empty():
    result = _count_convergence_signals({}, min_signals=2)
    assert result == {}


@pytest.mark.asyncio
async def test_fire_analyst_alerts_marks_mentions_notified(db):
    mentioned_at = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    await insert_analyst_mention(
        db,
        "0xai_dev",
        3,
        "https://x.com/0xai_dev/status/1",
        "SN3 is moving",
        mentioned_at,
    )

    fired = await fire_analyst_alerts(db, {3: {"name": "Templar"}})

    assert len(fired) == 1
    assert fired[0].alert_type == "analyst_mention"
    assert "@0xai_dev" in fired[0].description
    assert await get_unnotified_analyst_mentions(db) == []


@pytest.mark.asyncio
async def test_fire_milestone_alerts_includes_ai_fields_and_marks_notified(db):
    published_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    await insert_milestone(
        db,
        3,
        "arxiv",
        "SparseLoCo",
        "https://arxiv.org/abs/2603.08163",
        published_at,
        ai_summary="summary",
        ai_take="take",
    )

    fired = await fire_milestone_alerts(db, {3: {"name": "Templar"}})

    assert len(fired) == 1
    assert fired[0].alert_type == "milestone"
    assert "Summary: summary" in fired[0].description
    assert "Take: take" in fired[0].description
    assert await get_unnotified_milestones(db) == []


@pytest.mark.asyncio
async def test_evaluate_convergence_fires_for_distinct_recent_signals(db):
    now = datetime.now(timezone.utc)
    await insert_alert(
        db,
        AlertRecord(
            fired_at=now,
            netuid=3,
            subnet_name="Templar",
            alert_type="milestone",
            description="m",
        ),
    )
    await insert_alert(
        db,
        AlertRecord(
            fired_at=now,
            netuid=3,
            subnet_name="Templar",
            alert_type="analyst_mention",
            description="a",
        ),
    )

    fired = await evaluate_convergence(db, {3: {"name": "Templar"}})

    assert len(fired) == 1
    assert fired[0].alert_type == "convergence"
    assert fired[0].current_value == 2.0
    assert "HIGH CONVICTION" in fired[0].description
