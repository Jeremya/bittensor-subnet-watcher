from datetime import datetime, timedelta, timezone

import pytest

import config
from db.database import init_db, insert_snapshot
from engine.regime import (
    TideReading,
    apply_rel_strength,
    classify_regime,
    compute_tide,
)
from models import SubnetSnapshot

NOW = datetime.now(timezone.utc)


def _snap(netuid, *, hours_ago=0.0, price=None, flow=None, tao_in=1000.0):
    return SubnetSnapshot(
        netuid=netuid, polled_at=NOW - timedelta(hours=hours_ago),
        alpha_price_tao=price, net_tao_flow_tao=flow, tao_in_tao=tao_in)


@pytest.mark.asyncio
async def test_compute_tide_magnitude_and_breadth(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        # netuid 1: +30 flow, netuid 2: -10, netuid 3: +2 -> total +22 over pool 3000
        for netuid, flow in ((1, 30.0), (2, -10.0), (3, 2.0)):
            await insert_snapshot(db, _snap(netuid, hours_ago=1, flow=flow))
        reading = await compute_tide(db)
        assert reading.flows_24h_tao == pytest.approx(22.0)
        assert reading.tide_pct == pytest.approx(22.0 / 3000.0)
        assert reading.breadth_pct == pytest.approx(2 / 3)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_compute_tide_none_without_flow_data(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(1, hours_ago=1, flow=None))
        assert await compute_tide(db) is None
    finally:
        await db.close()


def test_classify_regime_boundaries():
    def r(tide, breadth):
        return classify_regime(TideReading(tide, breadth, 0.0, 1000.0))
    assert r(0.004, 0.60) == "risk_on"
    assert r(0.004, 0.50) == "neutral"      # magnitude without breadth
    assert r(0.001, 0.90) == "neutral"
    assert r(-0.004, 0.50) == "risk_off"
    assert r(0.001, 0.30) == "risk_off"     # breadth collapse alone
    assert classify_regime(None) is None


def test_rel_strength_percentile_rank():
    snaps = [
        _snap(1, price=1.10),   # +10% -> strongest
        _snap(2, price=1.00),   # flat
        _snap(3, price=0.90),   # -10% -> weakest
        _snap(4, price=1.00),   # no history -> None
    ]
    history = {
        1: [_snap(1, hours_ago=24, price=1.0)],
        2: [_snap(2, hours_ago=24, price=1.0)],
        3: [_snap(3, hours_ago=24, price=1.0)],
        4: [],
    }
    apply_rel_strength(snaps, history)
    assert snaps[0].rel_strength_score > snaps[1].rel_strength_score > snaps[2].rel_strength_score
    assert snaps[3].rel_strength_score is None
    assert 0.0 <= snaps[2].rel_strength_score <= 100.0


def test_rel_strength_requires_reference_within_tolerance():
    snaps = [_snap(1, price=2.0)]
    history = {1: [_snap(1, hours_ago=60, price=1.0)]}   # too old (> 28h)
    apply_rel_strength(snaps, history)
    assert snaps[0].rel_strength_score is None


from engine.regime import evaluate_regime, get_latest_market_state


async def _seed_risk_on(db):
    """24h of broad inflows: tide +5% of pool, breadth 100%, RS populated."""
    for netuid in (1, 2, 3):
        s = _snap(netuid, hours_ago=1, flow=50.0, price=1.0, tao_in=1000.0)
        s.rel_strength_score = 50.0 + netuid    # so the flip message has leaders
        await insert_snapshot(db, s)


@pytest.mark.asyncio
async def test_evaluate_regime_records_state_and_fires_once(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _seed_risk_on(db)
        fired = []
        for _ in range(3):                      # 2-poll hysteresis then steady
            fired += await evaluate_regime(db, {1: {"name": "Apex"}})
        flips = [a for a in fired if a.alert_type == "regime_flip"]
        assert len(flips) == 1
        assert "risk-ON" in flips[0].description
        assert "Apex" in flips[0].description        # leaders listed by name
        state = await get_latest_market_state(db)
        assert state["regime"] == "risk_on"
        cur = await db.execute("SELECT COUNT(*) FROM market_state")
        assert (await cur.fetchone())[0] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_evaluate_regime_freezes_without_data(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        fired = await evaluate_regime(db, {})
        assert fired == []
        assert await get_latest_market_state(db) is None
    finally:
        await db.close()
