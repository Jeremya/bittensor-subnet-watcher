from datetime import datetime, timedelta, timezone

from models import SubnetSnapshot
from engine.emergence import compute_reg_demand_score


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
