from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import config
from models import SubnetSnapshot


@dataclass(frozen=True)
class Spec421Component:
    score: Optional[float]
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    is_positive: bool = False
    is_negative: bool = False
    is_strong: bool = False


@dataclass(frozen=True)
class Spec421Signal:
    netuid: int
    price_ema: Spec421Component
    emission_value: Spec421Component
    protocol_context: Spec421Component
    spec421_score: float
    reasons: list[str]
    risks: list[str]
    notes: list[str]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _positive(value: Optional[float]) -> Optional[float]:
    if value is None or value <= 0:
        return None
    return float(value)


def _is_later_snapshot(
    candidate: SubnetSnapshot,
    candidate_index: int,
    current: SubnetSnapshot,
    current_index: int,
) -> bool:
    if candidate.polled_at is None:
        return current.polled_at is None and candidate_index > current_index
    if current.polled_at is None:
        return True
    if candidate.polled_at == current.polled_at:
        return candidate_index > current_index
    return candidate.polled_at > current.polled_at


def _latest_by_netuid(snapshots: list[SubnetSnapshot]) -> list[SubnetSnapshot]:
    selected: dict[int, tuple[int, SubnetSnapshot]] = {}
    for index, snap in enumerate(snapshots):
        current = selected.get(snap.netuid)
        if current is None or _is_later_snapshot(snap, index, current[1], current[0]):
            selected[snap.netuid] = (index, snap)
    return [snap for _, snap in sorted(selected.values(), key=lambda item: item[1].netuid)]


def _ordered_price_history(
    current: SubnetSnapshot,
    history: list[SubnetSnapshot],
) -> list[float]:
    rows = [
        row
        for row in history
        if row.polled_at is not None
        and row.polled_at < current.polled_at
        and _positive(row.alpha_price_tao) is not None
    ]
    rows.sort(key=lambda row: row.polled_at)
    prices = [float(row.alpha_price_tao) for row in rows]
    prices.append(float(current.alpha_price_tao))
    return prices


def _ema(values: list[float], alpha: float) -> float:
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def compute_price_ema_score(
    current: SubnetSnapshot,
    history: list[SubnetSnapshot],
) -> Spec421Component:
    if _positive(current.alpha_price_tao) is None:
        return Spec421Component(score=None, notes=["invalid current alpha price"])

    prices = _ordered_price_history(current, history)
    if len(prices) < 4:
        return Spec421Component(score=None, notes=["insufficient price history"])

    current_price = prices[-1]
    slow = _ema(prices, alpha=0.12)
    fast = _ema(prices, alpha=0.35)
    if slow <= 0:
        return Spec421Component(score=None, notes=["invalid EMA price proxy"])

    spot_vs_slow = current_price / slow - 1.0
    fast_vs_slow = fast / slow - 1.0
    score = 50.0
    score += max(-35.0, min(35.0, spot_vs_slow * 500.0))
    score += max(-15.0, min(15.0, fast_vs_slow * 300.0))
    score = _clamp(score)

    reasons: list[str] = []
    risks: list[str] = []
    if spot_vs_slow >= 0.03:
        reasons.append("price above EMA proxy")
    if fast_vs_slow > 0:
        reasons.append("fast EMA proxy above slow EMA proxy")
    if spot_vs_slow <= -0.03:
        risks.append("price below EMA proxy")

    return Spec421Component(
        score=round(score, 2),
        reasons=reasons,
        risks=risks,
        notes=["Spec 421 score uses EMA-price proxy, not exact SubnetMovingPrice"],
        is_positive=score >= 65.0,
        is_negative=score <= 40.0,
        is_strong=score >= 75.0,
    )


def _raw_emission_yield(snap: SubnetSnapshot) -> Optional[float]:
    if (
        snap.daily_emission_tao is None
        or snap.daily_emission_tao <= 0
        or snap.tao_usd_price is None
        or snap.tao_usd_price <= 0
        or snap.alpha_mcap_usd is None
        or snap.alpha_mcap_usd <= 0
    ):
        return None
    if snap.alpha_mcap_usd < config.YIELD_MIN_MCAP_USD:
        return None
    return (snap.daily_emission_tao * snap.tao_usd_price * 365.0) / snap.alpha_mcap_usd


def compute_emission_value_scores(
    snapshots: list[SubnetSnapshot],
) -> dict[int, Spec421Component]:
    snapshots = _latest_by_netuid(snapshots)
    raw_yields = {
        snap.netuid: raw
        for snap in snapshots
        if (raw := _raw_emission_yield(snap)) is not None
    }
    valid_mcap = [
        (snap.netuid, snap.alpha_mcap_tao)
        for snap in snapshots
        if _positive(snap.alpha_mcap_tao) is not None
    ]
    valid_mcap.sort(key=lambda item: item[1], reverse=True)
    mcap_rank = {netuid: rank for rank, (netuid, _) in enumerate(valid_mcap, start=1)}
    min_yield = min(raw_yields.values()) if raw_yields else None
    max_yield = max(raw_yields.values()) if raw_yields else None

    result: dict[int, Spec421Component] = {}
    for snap in snapshots:
        parts: list[float] = []
        reasons: list[str] = []
        risks: list[str] = []
        notes: list[str] = []

        if snap.alpha_mcap_usd is not None and snap.alpha_mcap_usd < config.YIELD_MIN_MCAP_USD:
            result[snap.netuid] = Spec421Component(
                score=None,
                notes=["below emission-value market-cap floor"],
            )
            continue

        raw = raw_yields.get(snap.netuid)
        if raw is not None and min_yield is not None and max_yield is not None:
            if max_yield == min_yield:
                yield_score = 50.0
            else:
                yield_score = (raw - min_yield) / (max_yield - min_yield) * 100.0
            parts.append(yield_score)
            if yield_score >= 75.0:
                reasons.append("price-based emission value versus market cap")

        mc_rank = mcap_rank.get(snap.netuid)
        if (
            raw is not None
            and snap.emission_rank is not None
            and mc_rank is not None
            and snap.emission_rank > 0
        ):
            ratio = mc_rank / snap.emission_rank
            rank_score = _clamp(50.0 + (ratio - 1.0) * 35.0)
            parts.append(rank_score)
            if ratio >= 1.3:
                reasons.append("price-based emission rank discounted by market cap")
            elif ratio <= 0.7:
                risks.append("market cap rich versus price-based emissions")

        if not parts:
            result[snap.netuid] = Spec421Component(
                score=None,
                notes=notes or ["missing emission-value data"],
            )
            continue

        score = sum(parts) / len(parts)
        result[snap.netuid] = Spec421Component(
            score=round(_clamp(score), 2),
            reasons=reasons,
            risks=risks,
            notes=notes,
            is_positive=score >= 65.0,
            is_negative=score <= 35.0,
            is_strong=score >= 75.0,
        )

    return result


def compute_protocol_context_score(snap: SubnetSnapshot) -> Spec421Component:
    score = 50.0
    reasons: list[str] = []
    risks: list[str] = []
    notes = [
        "exact root_proportion not collected",
        "exact miner_burned not collected",
        "exact alpha injection cap not collected",
    ]

    if snap.emergence_stage in {"nascent", "accelerating"}:
        score += 12.0
        reasons.append("newer subnet may benefit from root-proportion weighting")
    if snap.emergence_score is not None and snap.emergence_score >= 70.0:
        score += 13.0
        reasons.append("emergence score supports Spec 421 new-subnet context")
    if snap.emergence_stage == "established":
        score -= 8.0
        risks.append("older subnet may have lower root-proportion support")

    score = _clamp(score)
    return Spec421Component(
        score=round(score, 2),
        reasons=reasons,
        risks=risks,
        notes=notes,
        is_positive=score >= 62.0,
        is_negative=score <= 42.0,
        is_strong=score >= 72.0,
    )


def _weighted_score(components: list[tuple[Optional[float], float]]) -> float:
    available = [(score, weight) for score, weight in components if score is not None]
    if not available:
        return 0.0
    total_weight = sum(weight for _, weight in available)
    return sum(score * weight for score, weight in available) / total_weight


def compute_spec421_signals(
    snapshots: list[SubnetSnapshot],
    history_by_netuid: dict[int, list[SubnetSnapshot]],
) -> dict[int, Spec421Signal]:
    snapshots = _latest_by_netuid(snapshots)
    emission_values = compute_emission_value_scores(snapshots)
    result: dict[int, Spec421Signal] = {}
    for snap in snapshots:
        price_ema = compute_price_ema_score(snap, history_by_netuid.get(snap.netuid, []))
        emission_value = emission_values[snap.netuid]
        protocol_context = compute_protocol_context_score(snap)
        score = _weighted_score(
            [
                (price_ema.score, 0.45),
                (emission_value.score, 0.40),
                (protocol_context.score, 0.15),
            ]
        )
        reasons = price_ema.reasons + emission_value.reasons + protocol_context.reasons
        risks = price_ema.risks + emission_value.risks + protocol_context.risks
        notes = sorted(set(price_ema.notes + emission_value.notes + protocol_context.notes))
        result[snap.netuid] = Spec421Signal(
            netuid=snap.netuid,
            price_ema=price_ema,
            emission_value=emission_value,
            protocol_context=protocol_context,
            spec421_score=round(_clamp(score), 2),
            reasons=reasons,
            risks=risks,
            notes=notes,
        )
    return result
