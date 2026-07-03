from datetime import datetime, timedelta, timezone

import pytest

import config
from engine.pump_events import PumpEvent, detect_pump_events
from models import SubnetSnapshot

# Recent base time: Task 2's scan_and_store filters to the trailing 7 days,
# so fixtures must not use a fixed calendar date.
T0 = datetime.now(timezone.utc) - timedelta(hours=2)


def series(prices, *, step_minutes=15, netuid=1, mcap_usd=1_000_000.0, owners=None):
    return [
        SubnetSnapshot(
            netuid=netuid,
            polled_at=T0 + timedelta(minutes=step_minutes * i),
            alpha_price_tao=p,
            alpha_mcap_usd=mcap_usd,
            owner_coldkey=(owners[i] if owners else "owner1"),
        )
        for i, p in enumerate(prices)
    ]


def test_detects_pump_and_tracks_peak():
    prices = [1.0, 1.0, 1.1, 1.6, 2.0, 1.9]        # 2.0x peak from 1.0 min
    events = detect_pump_events(series(prices))
    assert len(events) == 1
    ev = events[0]
    assert ev.start_price == 1.0
    assert ev.peak_price == 2.0
    assert ev.status == "active"                     # never retraced 50%


def test_no_event_below_threshold():
    assert detect_pump_events(series([1.0, 1.2, 1.4, 1.45])) == []


def test_event_closes_on_retrace():
    prices = [1.0, 1.6, 2.0, 1.4]                    # 1.4 <= 2.0 - 0.5*(2.0-1.0)
    events = detect_pump_events(series(prices))
    assert len(events) == 1
    ev = events[0]
    assert ev.status == "closed"
    assert ev.end_price == 1.4
    assert ev.retrace_pct == pytest.approx(0.6)      # (2.0-1.4)/(2.0-1.0)


def test_gap_resets_detection():
    snaps = series([1.0, 1.0])
    late = series([1.6, 2.0], netuid=1)
    for i, s in enumerate(late):                     # 12h gap before the rise
        s.polled_at = snaps[-1].polled_at + timedelta(hours=12) + timedelta(minutes=15 * i)
    assert detect_pump_events(snaps + late) == []    # rise not comparable across gap


def test_owner_change_resets_detection():
    owners = ["a", "a", "b", "b"]
    assert detect_pump_events(series([1.0, 1.0, 1.6, 2.0], owners=owners)) == []


def test_micro_cap_ignored():
    assert detect_pump_events(series([1.0, 1.6, 2.0], mcap_usd=50_000.0)) == []


def test_second_event_after_close():
    prices = [1.0, 2.0, 1.2, 1.2, 2.0]               # close, then re-pump from 1.2
    events = detect_pump_events(series(prices))
    assert len(events) == 2
    assert events[0].status == "closed"
    assert events[1].start_price == 1.2


def test_start_is_latest_minimum_on_flat_stretch():
    """The pump starts at the LAST local low before the breakout, so lead/lag
    offsets land on real pre-pump snapshots."""
    prices = [1.0] * 8 + [1.6, 2.0]
    events = detect_pump_events(series(prices))
    assert len(events) == 1
    assert events[0].start_at == T0 + timedelta(minutes=15 * 7)


from db.database import init_db, insert_snapshot
from engine.pump_events import scan_and_store, get_recent_pump_events


@pytest.mark.asyncio
async def test_scan_and_store_persists_and_is_idempotent(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for s in series([1.0, 1.6, 2.0, 1.4]):
            await insert_snapshot(db, s)
        n1 = await scan_and_store(db, since_days=7)
        n2 = await scan_and_store(db, since_days=7)
        assert n1 == 1 and n2 == 1
        rows = await get_recent_pump_events(db, limit=10)
        assert len(rows) == 1                      # idempotent, no duplicate
        assert rows[0]["status"] == "closed"
        assert rows[0]["ratio"] == pytest.approx(2.0)
        assert rows[0]["retrace_pct"] == pytest.approx(0.6)
    finally:
        await db.close()
