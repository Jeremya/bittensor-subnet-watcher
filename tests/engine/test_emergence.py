from datetime import datetime, timedelta, timezone

from models import SubnetSnapshot
from engine.emergence import (
    EmergenceSignal,
    classify_stage,
    compute_emergence_signal,
    compute_flow_accel_score,
    compute_reg_demand_score,
    compute_slot_fill_score,
    score_emergence,
)


def _row(dt, reg_cost):
    return SubnetSnapshot(netuid=42, polled_at=dt, reg_cost_tao=reg_cost)


def test_reg_demand_rising_burn_scores_high():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    hist = [_row(now - timedelta(hours=72 - i), 0.5 + i * 0.1) for i in range(0, 72, 6)]
    snap = _row(now, 8.0)
    comp = compute_reg_demand_score(snap, hist, window_hours=72)
    assert comp.score is not None and comp.score >= 70.0
    assert comp.is_positive


def test_reg_demand_flat_burn_is_neutral():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    hist = [_row(now - timedelta(hours=72 - i), 1.0) for i in range(0, 72, 6)]
    snap = _row(now, 1.0)
    comp = compute_reg_demand_score(snap, hist, window_hours=72)
    assert comp.score is not None and 40.0 <= comp.score <= 60.0


def test_reg_demand_no_history_returns_none():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    comp = compute_reg_demand_score(_row(now, 1.0), [], window_hours=72)
    assert comp.score is None


def _slot_row(dt, n, cap=256):
    return SubnetSnapshot(netuid=42, polled_at=dt, n_neurons=n, max_allowed_uids=cap)


def test_slot_fill_rapid_climb_scores_high():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    hist = [_slot_row(now - timedelta(hours=72 - i), 10 + i * 3) for i in range(0, 72, 6)]
    snap = _slot_row(now, 240)
    comp = compute_slot_fill_score(snap, hist, window_hours=72)
    assert comp.score is not None and comp.score >= 65.0
    assert comp.is_positive


def test_slot_fill_full_and_static_is_mid():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    hist = [_slot_row(now - timedelta(hours=72 - i), 256) for i in range(0, 72, 6)]
    snap = _slot_row(now, 256)
    comp = compute_slot_fill_score(snap, hist, window_hours=72)
    assert comp.score is not None and comp.score <= 75.0


def test_slot_fill_missing_cap_returns_none():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snap = SubnetSnapshot(netuid=42, polled_at=now, n_neurons=10, max_allowed_uids=None)
    comp = compute_slot_fill_score(snap, [], window_hours=72)
    assert comp.score is None


def _flow_row(dt, flow):
    return SubnetSnapshot(
        netuid=42,
        polled_at=dt,
        net_tao_flow_tao=flow,
        alpha_mcap_tao=1000.0,
    )


def test_flow_acceleration_scores_high():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(0, 36, 6):
        rows.append(_flow_row(now - timedelta(hours=72 - i), 0.1))
    for i in range(36, 72, 6):
        rows.append(_flow_row(now - timedelta(hours=72 - i), 5.0))
    snap = _flow_row(now, 6.0)
    comp = compute_flow_accel_score(snap, rows, window_hours=72)
    assert comp.score is not None and comp.score >= 65.0
    assert comp.is_positive


def test_flow_decelerating_scores_low():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(0, 36, 6):
        rows.append(_flow_row(now - timedelta(hours=72 - i), 5.0))
    for i in range(36, 72, 6):
        rows.append(_flow_row(now - timedelta(hours=72 - i), 0.1))
    snap = _flow_row(now, 0.0)
    comp = compute_flow_accel_score(snap, rows, window_hours=72)
    assert comp.score is not None and comp.score < 50.0


def test_flow_accel_insufficient_history_returns_none():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    comp = compute_flow_accel_score(_flow_row(now, 1.0), [], window_hours=72)
    assert comp.score is None


def test_classify_stage_by_age_and_mcap():
    young = SubnetSnapshot(
        netuid=1,
        polled_at=datetime.now(timezone.utc),
        alpha_mcap_usd=100_000.0,
    )
    assert classify_stage(age_days=5.0, snap=young) == "nascent"
    assert classify_stage(age_days=30.0, snap=young) == "accelerating"
    assert classify_stage(age_days=90.0, snap=young) == "maturing"
    big = SubnetSnapshot(
        netuid=1,
        polled_at=datetime.now(timezone.utc),
        alpha_mcap_usd=5_000_000.0,
    )
    assert classify_stage(age_days=5.0, snap=big) == "established"


def test_compute_emergence_signal_combines_components():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    first_seen = now - timedelta(days=10)
    hist = []
    for i in range(0, 72, 6):
        hist.append(SubnetSnapshot(
            netuid=42,
            polled_at=now - timedelta(hours=72 - i),
            reg_cost_tao=0.5 + i * 0.1,
            n_neurons=10 + i * 3,
            max_allowed_uids=256,
            net_tao_flow_tao=(0.1 if i < 36 else 5.0),
            alpha_mcap_tao=1000.0,
        ))
    snap = SubnetSnapshot(
        netuid=42,
        polled_at=now,
        reg_cost_tao=8.0,
        n_neurons=240,
        max_allowed_uids=256,
        net_tao_flow_tao=6.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=150_000.0,
    )
    sig = compute_emergence_signal(snap, hist, first_seen_at=first_seen, now=now)
    assert isinstance(sig, EmergenceSignal)
    assert sig.emergence_score >= 65.0
    assert sig.stage in ("nascent", "accelerating")
    assert sig.reg_demand.score is not None


def test_compute_emergence_established_subnet_scored_but_flagged():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snap = SubnetSnapshot(
        netuid=1,
        polled_at=now,
        alpha_mcap_usd=9_000_000.0,
        n_neurons=256,
        max_allowed_uids=256,
        reg_cost_tao=1.0,
    )
    sig = compute_emergence_signal(snap, [], first_seen_at=now - timedelta(days=300), now=now)
    assert sig.stage == "established"


def test_score_emergence_sets_columns_in_place():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snap = SubnetSnapshot(
        netuid=42,
        polled_at=now,
        reg_cost_tao=8.0,
        n_neurons=240,
        max_allowed_uids=256,
        net_tao_flow_tao=6.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=150_000.0,
    )
    hist = [
        SubnetSnapshot(
            netuid=42,
            polled_at=now - timedelta(hours=72 - i),
            reg_cost_tao=0.5 + i * 0.1,
            n_neurons=10 + i * 3,
            max_allowed_uids=256,
            net_tao_flow_tao=(0.1 if i < 36 else 5.0),
            alpha_mcap_tao=1000.0,
        )
        for i in range(0, 72, 6)
    ]

    score_emergence([snap], {42: hist}, {42: now - timedelta(days=10)}, now=now)

    assert snap.emergence_score is not None
    assert snap.emergence_stage in ("nascent", "accelerating")
    assert snap.reg_demand_score is not None
