from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from engine.signals import SignalComponent, _clamp
from models import SubnetSnapshot


@dataclass
class EmergenceSignal:
    netuid: int
    reg_demand: SignalComponent
    slot_fill: SignalComponent
    flow_accel: SignalComponent
    emergence_score: Optional[float]   # None = no component had data (never a fake 0)
    stage: str
    reasons: list[str]


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


def compute_slot_fill_score(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
    window_hours: int = config.EMERGENCE_WINDOW_HOURS,
) -> SignalComponent:
    """Score UID fill level and velocity toward capacity."""
    cap = snap.max_allowed_uids
    n = snap.n_neurons
    if cap is None or cap <= 0 or n is None:
        return SignalComponent(score=None, risks=["missing slot data"])

    now = snap.polled_at or datetime.now(timezone.utc)
    rows = _window(history, now, window_hours)
    fill_now = min(1.0, n / cap)

    velocity_pts = 0.0
    reasons: list[str] = []
    prior = [
        row
        for row in rows
        if row.n_neurons is not None
        and row.max_allowed_uids is not None
        and row.max_allowed_uids > 0
    ]
    if prior:
        fill_then = min(1.0, prior[0].n_neurons / prior[0].max_allowed_uids)
        delta_fill = fill_now - fill_then
        velocity_pts = max(0.0, min(60.0, delta_fill * 120.0))
        if delta_fill >= 0.2:
            reasons.append("UID slots filling rapidly")

    score = _clamp(fill_now * 40.0 + velocity_pts)
    return SignalComponent(
        score=round(score, 2),
        reasons=reasons,
        is_positive=velocity_pts >= 24.0,
        is_strong=velocity_pts >= 48.0,
    )


def compute_flow_accel_score(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
    window_hours: int = config.EMERGENCE_WINDOW_HOURS,
) -> SignalComponent:
    """Score whether net TAO flow is accelerating across the lookback window."""
    now = snap.polled_at or datetime.now(timezone.utc)
    rows = _window(history, now, window_hours)
    pool = snap.alpha_mcap_tao
    flows = [
        (row.polled_at, row.net_tao_flow_tao)
        for row in rows
        if row.net_tao_flow_tao is not None
    ]
    if len(flows) < 4 or pool is None or pool <= 0:
        return SignalComponent(score=None, risks=["insufficient flow history"])

    mid = now - timedelta(hours=window_hours / 2)
    early = [flow for polled_at, flow in flows if polled_at < mid]
    late = [flow for polled_at, flow in flows if polled_at >= mid]
    if not early or not late:
        return SignalComponent(score=None, risks=["flow history not split-able"])

    early_rate = (sum(early) / len(early)) / pool
    late_rate = (sum(late) / len(late)) / pool
    accel = late_rate - early_rate
    score = _clamp(50.0 + max(-50.0, min(50.0, accel * 8000.0)))

    reasons: list[str] = []
    if accel > 0 and late_rate > 0:
        reasons.append("net TAO inflow accelerating")

    return SignalComponent(
        score=round(score, 2),
        reasons=reasons,
        is_positive=accel > 0 and late_rate > 0,
        is_strong=accel > 0 and score >= 75.0,
    )


def classify_stage(age_days: float, snap: SubnetSnapshot) -> str:
    """Classify emergence stage. Caller must provide owner-epoch-scoped age."""
    if (
        snap.alpha_mcap_usd is not None
        and snap.alpha_mcap_usd >= config.EMERGENCE_MAX_MCAP_USD
    ):
        return "established"
    if age_days < config.EMERGENCE_NASCENT_AGE_DAYS:
        return "nascent"
    if age_days < config.EMERGENCE_ACCELERATING_AGE_DAYS:
        return "accelerating"
    return "maturing"


def compute_emergence_signal(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
    first_seen_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> EmergenceSignal:
    now = now or snap.polled_at or datetime.now(timezone.utc)
    age_days = (
        (now - first_seen_at).total_seconds() / 86400.0
        if first_seen_at is not None
        else 0.0
    )
    stage = classify_stage(age_days, snap)

    reg = compute_reg_demand_score(snap, history)
    slot = compute_slot_fill_score(snap, history)
    flow = compute_flow_accel_score(snap, history)

    weighted = [
        (reg.score, config.EMERGENCE_REG_DEMAND_WEIGHT),
        (slot.score, config.EMERGENCE_SLOT_FILL_WEIGHT),
        (flow.score, config.EMERGENCE_FLOW_ACCEL_WEIGHT),
    ]
    available = [(score, weight) for score, weight in weighted if score is not None]
    if available:
        total_weight = sum(weight for _, weight in available)
        raw = sum(score * weight for score, weight in available) / total_weight
        emergence_score = round(_clamp(raw), 2)
    else:
        emergence_score = None   # no data must persist as NULL, never a fake 0.0

    return EmergenceSignal(
        netuid=snap.netuid,
        reg_demand=reg,
        slot_fill=slot,
        flow_accel=flow,
        emergence_score=emergence_score,
        stage=stage,
        reasons=reg.reasons + slot.reasons + flow.reasons,
    )


def score_emergence(
    snapshots: list[SubnetSnapshot],
    history_by_netuid: dict[int, list[SubnetSnapshot]],
    age_context: dict[int, datetime],
    now: Optional[datetime] = None,
) -> None:
    """Compute emergence fields on each snapshot in-place."""
    for snap in snapshots:
        sig = compute_emergence_signal(
            snap,
            history=history_by_netuid.get(snap.netuid, []),
            first_seen_at=age_context.get(snap.netuid),
            now=now,
        )
        snap.reg_demand_score = sig.reg_demand.score
        snap.slot_fill_score = sig.slot_fill.score
        snap.flow_accel_score = sig.flow_accel.score
        snap.emergence_score = sig.emergence_score
        snap.emergence_stage = sig.stage
