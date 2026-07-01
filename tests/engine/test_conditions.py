import pytest

from db.database import init_db
from engine.conditions import advance_condition, get_active_conditions


async def _observe(db, breached, value=1.0, n=1):
    """Advance the same (netuid=5, 'emission_near_zero') condition n times."""
    results = []
    for _ in range(n):
        results.append(await advance_condition(
            db, netuid=5, condition="emission_near_zero",
            breached=breached, value=value,
        ))
    return results


@pytest.mark.asyncio
async def test_single_breach_does_not_enter(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        assert await _observe(db, True, n=1) == [None]
        assert await get_active_conditions(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_two_breaches_enter_once(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        assert await _observe(db, True, n=3) == [None, "entered", None]
        active = await get_active_conditions(db)
        assert len(active) == 1 and active[0]["condition"] == "emission_near_zero"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_flap_pending_is_dropped(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=1)     # pending
        await _observe(db, False, n=1)    # healthy again → pending dropped
        assert await _observe(db, True, n=1) == [None]  # streak restarted at 1
        assert await get_active_conditions(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_needs_four_clear_polls(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)                       # entered
        assert await _observe(db, False, n=3) == [None] * 3
        assert await _observe(db, False, n=1) == ["recovered"]
        assert await get_active_conditions(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_clear_streak_resets_on_rebreak(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)      # entered
        await _observe(db, False, n=3)     # clearing…
        await _observe(db, True, n=1)      # re-breach resets clear streak
        assert await _observe(db, False, n=3) == [None] * 3   # needs 4 again
        assert len(await get_active_conditions(db)) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_data_freezes_state(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)   # entered
        assert await _observe(db, None, n=10) == [None] * 10
        assert len(await get_active_conditions(db)) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reentry_creates_new_episode(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)      # episode 1 entered
        await _observe(db, False, n=4)     # episode 1 recovered
        assert (await _observe(db, True, n=2))[-1] == "entered"   # episode 2
        cur = await db.execute("SELECT COUNT(*) FROM condition_states WHERE netuid=5")
        assert (await cur.fetchone())[0] == 2
    finally:
        await db.close()
