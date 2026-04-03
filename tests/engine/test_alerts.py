import pytest
import aiosqlite
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from models import SubnetSnapshot, AlertRecord
from db.database import SCHEMA_SQL
from engine.alerts import (
    check_emission_divergence,
    check_dead_github,
    check_emission_drop,
    check_github_spike,
    check_social_silence,
    check_new_entry,
    evaluate_alerts,
)


def now(): return datetime.now(timezone.utc)


def make_snap(netuid: int, **kwargs) -> SubnetSnapshot:
    return SubnetSnapshot(netuid=netuid, polled_at=now(), **kwargs)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


# ── Individual checks ─────────────────────────────────────────────────────────

def test_emission_divergence_fires():
    snap = make_snap(1, emission_rank=3, alpha_mcap_tao=32000.0)
    # mcap_rank=18 → ratio=6.0 > 1.5
    result = check_emission_divergence(snap, emission_rank=3, mcap_rank=18)
    assert result is not None
    assert result.alert_type == "emission_divergence"
    assert result.current_value == pytest.approx(6.0)


def test_emission_divergence_does_not_fire_below_threshold():
    result = check_emission_divergence(make_snap(1), emission_rank=5, mcap_rank=6)
    assert result is None  # mcap_rank/emission_rank = 6/5 = 1.2 < 1.5


def test_dead_github_fires():
    old_push = now() - timedelta(days=70)
    snap = make_snap(1, gh_last_push=old_push, alpha_mcap_usd=1_000_000)
    result = check_dead_github(snap)
    assert result is not None
    assert result.alert_type == "dead_github"


def test_dead_github_does_not_fire_below_mcap_threshold():
    old_push = now() - timedelta(days=70)
    snap = make_snap(1, gh_last_push=old_push, alpha_mcap_usd=100_000)  # < $500K
    assert check_dead_github(snap) is None


def test_emission_drop_fires():
    prev = make_snap(1, emission_rank=5)
    prev.polled_at = now() - timedelta(hours=23)
    curr = make_snap(1, emission_rank=8)  # dropped 3 ranks
    result = check_emission_drop(curr, prev)
    assert result is not None
    assert result.alert_type == "emission_drop"


def test_github_spike_fires():
    prev = make_snap(1, gh_stars=50, gh_forks=10)
    curr = make_snap(1, gh_stars=105, gh_forks=10)  # stars doubled
    result = check_github_spike(curr, prev)
    assert result is not None
    assert result.alert_type == "github_spike"


def test_social_silence_fires():
    old_tweet = now() - timedelta(days=20)
    snap = make_snap(1, x_last_tweet=old_tweet)
    result = check_social_silence(snap)
    assert result is not None
    assert result.alert_type == "social_silence"


def test_new_entry_fires_for_unknown_netuid():
    snap = make_snap(999)
    result = check_new_entry(snap, known_netuids={1, 2, 3})
    assert result is not None
    assert result.alert_type == "new_entry"


def test_new_entry_does_not_fire_for_known():
    snap = make_snap(1)
    assert check_new_entry(snap, known_netuids={1, 2, 3}) is None


# ── evaluate_alerts integration ───────────────────────────────────────────────

async def test_evaluate_alerts_respects_cooldown(db):
    from db.database import insert_alert
    # Pre-fire an emission_divergence alert for netuid 1
    existing = AlertRecord(
        fired_at=now(), netuid=1, subnet_name="Apex",
        alert_type="emission_divergence", description="x", current_value=3.0, threshold=1.5
    )
    await insert_alert(db, existing)

    snap = make_snap(1, emission_rank=3, alpha_mcap_tao=32000.0, alpha_mcap_usd=5_000_000)
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}
    prev_by_netuid = {}
    known_netuids = {1}

    alerts = await evaluate_alerts(
        db, [snap], registry, prev_by_netuid, known_netuids
    )
    # Should not fire again (cooldown)
    em_div_alerts = [a for a in alerts if a.alert_type == "emission_divergence" and a.netuid == 1]
    assert len(em_div_alerts) == 0
