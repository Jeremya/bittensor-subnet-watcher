from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import config
from engine.signals import SignalComponent, _clamp
from models import SubnetSnapshot


def _window(history: list[SubnetSnapshot], now: datetime, hours: int) -> list[SubnetSnapshot]:
    cutoff = now - timedelta(hours=hours)
    rows = [row for row in history if row.polled_at is not None and row.polled_at >= cutoff]
    rows.sort(key=lambda row: row.polled_at)
    return rows


def compute_reg_demand_score(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
    window_hours: int = config.EMERGENCE_WINDOW_HOURS,
) -> SignalComponent:
    """Registration-demand trend: rising burn means competition to register."""
    now = snap.polled_at or datetime.now(timezone.utc)
    rows = _window(history, now, window_hours)
    costs = [
        row.reg_cost_tao
        for row in rows
        if row.reg_cost_tao is not None and row.reg_cost_tao > 0
    ]
    current = snap.reg_cost_tao
    if not costs or current is None or current <= 0:
        return SignalComponent(score=None, risks=["insufficient reg-cost history"])

    baseline = costs[0]
    if baseline <= 0:
        return SignalComponent(score=None, risks=["zero reg-cost baseline"])

    ratio = current / baseline
    score = _clamp(50.0 + 25.0 * math.log2(ratio))
    reasons: list[str] = []
    if ratio >= 1.5:
        reasons.append("registration burn cost rising")

    return SignalComponent(
        score=round(score, 2),
        reasons=reasons,
        is_positive=ratio >= 1.5,
        is_strong=ratio >= 3.0,
    )
