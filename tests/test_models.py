from datetime import datetime, timezone
from models import SubnetSnapshot, AlertRecord


def test_subnet_snapshot_defaults():
    snap = SubnetSnapshot(netuid=1, polled_at=datetime.now(timezone.utc))
    assert snap.netuid == 1
    assert snap.alpha_price_tao is None
    assert snap.yield_score is None
    assert snap.emergence_score is None
    assert snap.emergence_stage is None


def test_subnet_snapshot_with_values():
    now = datetime.now(timezone.utc)
    snap = SubnetSnapshot(
        netuid=64,
        polled_at=now,
        alpha_price_tao=0.086,
        alpha_mcap_tao=216869.0,
        alpha_mcap_usd=65_000_000.0,
        daily_emission_tao=50.0,
        emission_rank=1,
        n_neurons=256,
    )
    assert snap.netuid == 64
    assert snap.alpha_price_tao == 0.086
    assert snap.emission_rank == 1


def test_subnet_snapshot_with_emergence_scores():
    now = datetime.now(timezone.utc)
    snap = SubnetSnapshot(
        netuid=42,
        polled_at=now,
        reg_demand_score=72.0,
        slot_fill_score=68.0,
        flow_accel_score=80.0,
        emergence_score=74.5,
        emergence_stage="accelerating",
    )
    assert snap.reg_demand_score == 72.0
    assert snap.slot_fill_score == 68.0
    assert snap.flow_accel_score == 80.0
    assert snap.emergence_score == 74.5
    assert snap.emergence_stage == "accelerating"


def test_alert_record_defaults():
    now = datetime.now(timezone.utc)
    alert = AlertRecord(
        fired_at=now,
        netuid=1,
        subnet_name="Apex",
        alert_type="emission_divergence",
        description="ratio 3.0x",
        current_value=3.0,
        threshold=1.5,
    )
    assert alert.notified is False
    assert alert.id is None
