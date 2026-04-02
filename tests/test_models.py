from datetime import datetime, timezone
from models import SubnetSnapshot, AlertRecord


def test_subnet_snapshot_defaults():
    snap = SubnetSnapshot(netuid=1, polled_at=datetime.now(timezone.utc))
    assert snap.netuid == 1
    assert snap.alpha_price_tao is None
    assert snap.yield_score is None


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
