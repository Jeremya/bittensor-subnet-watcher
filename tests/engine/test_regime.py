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
