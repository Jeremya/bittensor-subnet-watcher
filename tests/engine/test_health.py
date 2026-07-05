from datetime import datetime, timedelta, timezone

import pytest

from db.database import init_db, insert_snapshot, set_collector_state
from engine.health import compute_collector_health
from models import SubnetSnapshot


def _snap(netuid=1, age_minutes=0, price=1.0, gh_stars=10):
    return SubnetSnapshot(
        netuid=netuid,
        polled_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        alpha_price_tao=price, buy_slippage_pct=1.0, gh_stars=gh_stars,
        gh_last_push=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_fresh_data_is_healthy(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(age_minutes=5))
        await set_collector_state(db, "milestone_last_arxiv_check",
                                  datetime.now(timezone.utc).isoformat())
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["chain"].stale is False
        assert health["github"].stale is False
        assert health["milestone"].stale is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_old_rows_mark_chain_stale(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(age_minutes=120))
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["chain"].stale is True
        assert any("stale" in r for r in health["chain"].reasons)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_high_null_rate_marks_chain_stale(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for i in range(10):
            await insert_snapshot(db, _snap(netuid=i, price=None))
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["chain"].stale is True
        assert any("null" in r for r in health["chain"].reasons)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_db_reports_all_stale(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        health = await compute_collector_health(db)
        assert all(h.stale for h in health)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_github_staleness_uses_success_heartbeat_not_carried_data(tmp_path):
    """Carried-forward gh_* fields must not mask a dead collector: when the
    github_last_success heartbeat exists and is old, github is stale even
    though recent snapshots carry non-NULL gh_stars (the Jul 2 dead-token
    failure hid behind exactly this)."""
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(age_minutes=5))       # fresh carried gh_stars
        await set_collector_state(
            db, "github_last_success",
            (datetime.now(timezone.utc) - timedelta(days=3)).isoformat())
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["github"].stale is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_github_staleness_falls_back_to_snapshots_without_heartbeat(tmp_path):
    """Pre-heartbeat DBs keep the old behavior."""
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(age_minutes=5))
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["github"].stale is False
    finally:
        await db.close()
