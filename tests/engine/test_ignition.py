from datetime import datetime, timedelta, timezone

import pytest

import config
from engine.ignition import detect_ignition
from models import SubnetSnapshot

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)


def snap(price, *, minutes_ago=0, vol=100_000.0, flow=0.0, pool=10_000.0,
         mcap=1_000_000.0):
    return SubnetSnapshot(
        netuid=1, polled_at=NOW - timedelta(minutes=minutes_ago),
        alpha_price_tao=price, volume_24h_alpha=vol, net_tao_flow_tao=flow,
        alpha_mcap_tao=pool, alpha_mcap_usd=mcap, buy_slippage_pct=1.0)


def hist(*snaps):
    """history newest-first, as poll_cycle provides it."""
    return sorted(snaps, key=lambda s: s.polled_at, reverse=True)


def test_fires_on_price_impulse_with_flow_confirmation():
    cur = snap(1.10, flow=300.0)                     # +10% and 3% of pool inflow
    h = hist(snap(1.0, minutes_ago=15), snap(1.0, minutes_ago=1440, vol=100_000.0))
    sig = detect_ignition(cur, h)
    assert sig is not None
    assert sig.price_impulse_pct == pytest.approx(10.0)


def test_fires_on_price_impulse_with_volume_confirmation():
    cur = snap(1.10, vol=200_000.0)                  # 2x the volume of 24h ago
    h = hist(snap(1.0, minutes_ago=15), snap(1.0, minutes_ago=1440, vol=100_000.0))
    assert detect_ignition(cur, h) is not None


def test_no_fire_without_confirmation():
    cur = snap(1.10)                                  # price impulse alone
    h = hist(snap(1.0, minutes_ago=15), snap(1.0, minutes_ago=1440, vol=100_000.0))
    assert detect_ignition(cur, h) is None


def test_no_fire_below_price_impulse():
    cur = snap(1.03, flow=300.0)
    h = hist(snap(1.0, minutes_ago=15))
    assert detect_ignition(cur, h) is None


def test_outage_gate_blocks_stale_prev():
    """First poll after an outage must never read as an impulse."""
    cur = snap(2.0, flow=300.0)
    h = hist(snap(1.0, minutes_ago=300))              # prev is 5h old
    assert detect_ignition(cur, h) is None


def test_no_fire_below_mcap_floor():
    cur = snap(1.10, flow=300.0, mcap=50_000.0)
    h = hist(snap(1.0, minutes_ago=15))
    assert detect_ignition(cur, h) is None


import aiosqlite
from db.database import SCHEMA_SQL
from engine.alerts import evaluate_ignition


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


async def test_evaluate_ignition_fires_and_respects_cooldown(db):
    cur = snap(1.10, flow=300.0)
    h = {1: hist(snap(1.0, minutes_ago=15))}
    first = await evaluate_ignition(db, [cur], h, {})
    second = await evaluate_ignition(db, [cur], h, {})
    assert len(first) == 1 and first[0].alert_type == "pump_ignition"
    assert second == []                               # cooldown


async def test_cluster_collapses_to_single_notification(db):
    snaps, h = [], {}
    for n in (1, 2, 3):
        s = snap(1.10, flow=300.0); s.netuid = n
        p = snap(1.0, minutes_ago=15); p.netuid = n
        snaps.append(s); h[n] = hist(p)
    fired = await evaluate_ignition(db, snaps, h, {})
    assert len(fired) == 4                            # 3 individual + 1 summary
    cur = await db.execute(
        "SELECT COUNT(*) FROM alerts WHERE alert_type='pump_ignition' AND notified=0")
    assert (await cur.fetchone())[0] == 1             # only the summary reaches Telegram
