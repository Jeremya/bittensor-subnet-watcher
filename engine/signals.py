from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from models import SubnetSnapshot

SEVERE_RISK_ALERTS = {"emission_near_zero", "liquidity_floor"}
MODERATE_RISK_ALERTS = {
    "ownership_transfer",
    "hyperparameter_change",
    "tao_outflow",
    "dead_github",
}
CATALYST_ALERTS = {
    "convergence",
    "analyst_mention",
    "milestone",
    "github_spike",
    "whale_inflow",
}


@dataclass
class SignalComponent:
    score: Optional[float]
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    is_positive: bool = False
    is_negative: bool = False
    is_strong: bool = False
    blocks_new_buy: bool = False


@dataclass
class RiskSignal:
    penalty: float
    risks: list[str] = field(default_factory=list)
    has_severe_risk: bool = False
    moderate_count: int = 0


@dataclass
class SwingSignal:
    netuid: int
    flow: SignalComponent
    relative_value: SignalComponent
    tradability: SignalComponent
    catalyst: SignalComponent
    risk: RiskSignal
    swing_score: float
    reasons: list[str]
    risks: list[str]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _pool_size(snap: SubnetSnapshot) -> Optional[float]:
    for value in (snap.tao_in_tao, snap.alpha_mcap_tao):
        if value is not None and value > 0:
            return value
    return None


def _history_since(history: list[SubnetSnapshot], cutoff: datetime) -> list[SubnetSnapshot]:
    return [row for row in history if row.polled_at >= cutoff]


def _flow_rate(rows: list[SubnetSnapshot], pool: float) -> Optional[float]:
    flows = [row.net_tao_flow_tao for row in rows if row.net_tao_flow_tao is not None]
    if not flows:
        return None
    return sum(flows) / pool


def compute_flow_score(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
) -> SignalComponent:
    if not history:
        return SignalComponent(score=None, risks=["insufficient flow history"])

    now = snap.polled_at or datetime.now(timezone.utc)
    pool = _pool_size(snap)
    if pool is None:
        return SignalComponent(score=None, risks=["missing pool size"])

    rows_24h = _history_since(history, now - timedelta(hours=24))
    rows_7d = _history_since(history, now - timedelta(days=7))
    rate_24h = _flow_rate(rows_24h, pool)
    rate_7d = _flow_rate(rows_7d, pool)

    if rate_24h is None and rate_7d is None:
        return SignalComponent(score=None, risks=["missing net flow data"])

    def contribution(rate: Optional[float], scale: float) -> float:
        if rate is None:
            return 0.0
        if rate >= 0:
            return min(30.0, rate * scale)
        return max(-45.0, rate * scale * 1.5)

    score = 50.0
    score += 0.60 * contribution(rate_24h, 600.0)
    score += 0.30 * contribution(rate_7d, 300.0)

    rank_reason = None
    ranked_history = [row for row in history if row.emission_rank is not None]
    if snap.emission_rank is not None and ranked_history:
        ref = ranked_history[-1]
        rank_delta = ref.emission_rank - snap.emission_rank
        score += 0.10 * max(-15.0, min(15.0, rank_delta * 3.0))
        if rank_delta > 0:
            rank_reason = "emission rank confirming flow"

    recent_rate = rate_24h if rate_24h is not None else rate_7d or 0.0
    reasons: list[str] = []
    risks: list[str] = []
    is_positive = recent_rate > 0
    is_negative = recent_rate < 0
    if is_positive:
        reasons.append("positive net TAO flow")
    if rank_reason:
        reasons.append(rank_reason)
    if is_negative:
        risks.append("sustained net TAO outflow")

    return SignalComponent(
        score=round(_clamp(score), 2),
        reasons=reasons,
        risks=risks,
        is_positive=is_positive,
        is_negative=is_negative,
        is_strong=score >= 70.0,
    )


def _raw_yield(snap: SubnetSnapshot) -> Optional[float]:
    if (
        snap.daily_emission_tao is None
        or snap.tao_usd_price is None
        or not snap.alpha_mcap_usd
        or snap.alpha_mcap_usd < config.YIELD_MIN_MCAP_USD
    ):
        return None
    return (snap.daily_emission_tao * snap.tao_usd_price * 365) / snap.alpha_mcap_usd


def compute_relative_value_scores(
    snapshots: list[SubnetSnapshot],
) -> dict[int, SignalComponent]:
    yields = {
        snap.netuid: raw
        for snap in snapshots
        if (raw := _raw_yield(snap)) is not None
    }
    valid_mcap = [
        (snap.netuid, snap.alpha_mcap_tao)
        for snap in snapshots
        if snap.alpha_mcap_tao is not None
    ]
    valid_mcap.sort(key=lambda item: item[1], reverse=True)
    mcap_rank = {netuid: rank for rank, (netuid, _) in enumerate(valid_mcap, start=1)}

    min_yield = min(yields.values()) if yields else None
    max_yield = max(yields.values()) if yields else None
    result: dict[int, SignalComponent] = {}

    for snap in snapshots:
        score_parts: list[float] = []
        reasons: list[str] = []
        risks: list[str] = []

        raw = yields.get(snap.netuid)
        if raw is not None and min_yield is not None and max_yield is not None:
            if max_yield == min_yield:
                yield_score = 50.0
            else:
                yield_score = (raw - min_yield) / (max_yield - min_yield) * 100.0
            score_parts.append(yield_score)
            if yield_score >= 75.0:
                reasons.append("cheap emissions versus market cap")

        mc_rank = mcap_rank.get(snap.netuid)
        if snap.emission_rank is not None and mc_rank is not None and snap.emission_rank > 0:
            ratio = mc_rank / snap.emission_rank
            rank_score = _clamp(50.0 + (ratio - 1.0) * 35.0)
            score_parts.append(rank_score)
            if ratio >= 1.3:
                reasons.append("cheap emissions versus market cap")
            elif ratio <= 0.7:
                risks.append("rich market cap versus emissions")

        if not score_parts:
            result[snap.netuid] = SignalComponent(
                score=None, risks=["missing relative value data"]
            )
            continue

        score = sum(score_parts) / len(score_parts)
        result[snap.netuid] = SignalComponent(
            score=round(_clamp(score), 2),
            reasons=reasons,
            risks=risks,
            is_positive=score >= 65.0,
            is_negative=score <= 35.0,
        )

    return result


def compute_tradability_score(snap: SubnetSnapshot) -> SignalComponent:
    if (
        snap.volume_24h_alpha is None
        or snap.alpha_price_tao is None
        or snap.alpha_mcap_tao is None
        or snap.alpha_mcap_tao <= 0
    ):
        return SignalComponent(score=None, risks=["missing liquidity data"])

    turnover = (snap.volume_24h_alpha * snap.alpha_price_tao) / snap.alpha_mcap_tao
    if turnover < config.LIQUIDITY_FLOOR_RATIO:
        return SignalComponent(
            score=10.0,
            risks=["liquidity below swing threshold"],
            is_negative=True,
            blocks_new_buy=True,
        )
    if turnover >= 0.05:
        score = 95.0
    elif turnover >= 0.02:
        score = 80.0
    elif turnover >= 0.01:
        score = 65.0
    else:
        score = 45.0
    return SignalComponent(
        score=score,
        reasons=["tradable daily turnover"] if score >= 65.0 else [],
        risks=[] if score >= 45.0 else ["thin swing liquidity"],
        is_positive=score >= 65.0,
        is_negative=score < 45.0,
    )


def compute_catalyst_score(
    alert_types: set[str],
    covered: bool,
    has_milestone: bool,
) -> SignalComponent:
    score = 0.0
    reasons: list[str] = []
    if "convergence" in alert_types:
        score += 50.0
        reasons.append("fresh convergence catalyst")
    if "whale_inflow" in alert_types:
        score += 25.0
        reasons.append("large net inflow catalyst")
    if "analyst_mention" in alert_types or covered:
        score += 22.0
        reasons.append("fresh analyst coverage")
    if "milestone" in alert_types or has_milestone:
        score += 18.0
        reasons.append("fresh product/research milestone")
    if "github_spike" in alert_types:
        score += 8.0
        reasons.append("GitHub attention spike")

    if score == 0.0:
        return SignalComponent(score=None)

    score = _clamp(score)
    return SignalComponent(
        score=round(score, 2),
        reasons=reasons,
        is_positive=score > 0,
        is_strong=score >= 50.0,
    )


def compute_risk_penalty(alert_types: set[str], flow_negative: bool) -> RiskSignal:
    severe = SEVERE_RISK_ALERTS & alert_types
    moderate = MODERATE_RISK_ALERTS & alert_types
    penalty = 0.0
    risks: list[str] = []

    if severe:
        penalty += 45.0
        risks.append("severe liquidity/emission risk")
    if moderate:
        penalty += min(30.0, 12.0 * len(moderate))
        risks.append("multiple moderate risk alerts" if len(moderate) >= 2 else "moderate risk alert")
    if flow_negative:
        penalty += 15.0
        risks.append("negative flow risk")

    return RiskSignal(
        penalty=round(_clamp(penalty, 0.0, 70.0), 2),
        risks=risks,
        has_severe_risk=bool(severe),
        moderate_count=len(moderate),
    )


def compute_swing_signal(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
    relative_value: SignalComponent,
    alert_types: set[str],
    covered: bool,
    has_milestone: bool,
) -> SwingSignal:
    flow = compute_flow_score(snap, history)
    tradability = compute_tradability_score(snap)
    catalyst = compute_catalyst_score(alert_types, covered, has_milestone)
    risk = compute_risk_penalty(alert_types, flow.is_negative)

    weighted = [
        (flow.score, 0.40),
        (relative_value.score, 0.25),
        (tradability.score, 0.20),
        (catalyst.score, 0.15),
    ]
    available = [(score, weight) for score, weight in weighted if score is not None]
    if available:
        total_weight = sum(weight for _, weight in available)
        base = sum(score * weight for score, weight in available) / total_weight
    else:
        base = 0.0

    swing_score = round(_clamp(base - risk.penalty), 2)
    reasons = flow.reasons + relative_value.reasons + tradability.reasons + catalyst.reasons
    risks = flow.risks + relative_value.risks + tradability.risks + risk.risks

    return SwingSignal(
        netuid=snap.netuid,
        flow=flow,
        relative_value=relative_value,
        tradability=tradability,
        catalyst=catalyst,
        risk=risk,
        swing_score=swing_score,
        reasons=reasons,
        risks=risks,
    )
