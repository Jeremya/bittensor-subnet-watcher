from datetime import datetime, timedelta, timezone

import pytest

from db.database import init_db, insert_snapshot
from engine.pump_events import scan_and_store
from models import SubnetSnapshot
from scripts.signal_leadlag import run_leadlag


@pytest.mark.asyncio
async def test_leadlag_samples_signals_and_grades_alerts(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        now = datetime.now(timezone.utc)
        # 30h of pre-history with swing=80 (a "hit" at every offset), then a pump
        t = now - timedelta(hours=30)
        price = 1.0
        while t < now:
            if t > now - timedelta(hours=1):
                price = 2.0                       # the pump: last hour doubles
            await insert_snapshot(db, SubnetSnapshot(
                netuid=7, polled_at=t, alpha_price_tao=price,
                alpha_mcap_usd=1_000_000.0, owner_coldkey="o",
                swing_score=80.0, emergence_score=30.0))
            t += timedelta(minutes=15)
        # close the event with a retrace
        await insert_snapshot(db, SubnetSnapshot(
            netuid=7, polled_at=now, alpha_price_tao=1.2,
            alpha_mcap_usd=1_000_000.0, owner_coldkey="o"))
        await scan_and_store(db, since_days=7)

        report = await run_leadlag(db, threshold=70.0)
        assert report["event_count"] == 1
        sw = report["signals"]["swing_score"]
        assert sw["hit_rate"] == pytest.approx(1.0)     # 80 >= 70 before T0
        em = report["signals"]["emergence_score"]
        assert em["hit_rate"] == pytest.approx(0.0)     # 30 < 70
    finally:
        await db.close()
