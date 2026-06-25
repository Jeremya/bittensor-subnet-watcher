from dataclasses import dataclass
from math import isfinite
from typing import Literal

import config
from models import SubnetSnapshot


FlowDirection = Literal["buy", "sell"]
FlowAlertType = Literal["important_buy", "important_sell"]
FlowSource = Literal["snapshot_net_flow"]


@dataclass(frozen=True)
class FlowImpulse:
    netuid: int
    direction: FlowDirection
    alert_type: FlowAlertType
    source: FlowSource
    flow_tao: float
    relative_flow_pct: float
    threshold_pct: float
    impact_score: float
    price_move_pct: float | None = None
    volume_turnover_pct: float | None = None
    buy_slippage_pct: float | None = None
    sell_slippage_pct: float | None = None
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _finite_number(value: float | None) -> float | None:
    if value is None:
        return None
    if not isfinite(value):
        return None
    return value


def _valid_price_tao(value: float | None) -> float | None:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return number


def _valid_volume_alpha(value: float | None) -> float | None:
    number = _finite_number(value)
    if number is None or number < 0:
        return None
    return number


def _valid_market_cap(value: float | None) -> float | None:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return number


def _valid_slippage_pct(value: float | None) -> float | None:
    number = _finite_number(value)
    if number is None or number < 0:
        return None
    return number


def _price_move_pct(
    current: SubnetSnapshot,
    previous: SubnetSnapshot | None,
) -> float | None:
    if previous is None:
        return None
    current_price = _valid_price_tao(current.alpha_price_tao)
    previous_price = _valid_price_tao(previous.alpha_price_tao)
    if current_price is None or previous_price is None:
        return None
    return _finite_number(((current_price - previous_price) / previous_price) * 100.0)


def _volume_turnover_pct(snap: SubnetSnapshot) -> float | None:
    volume = _valid_volume_alpha(snap.volume_24h_alpha)
    price = _valid_price_tao(snap.alpha_price_tao)
    pool = _valid_market_cap(snap.alpha_mcap_tao)
    if volume is None or price is None or pool is None:
        return None
    return _finite_number((volume * price / pool) * 100.0)


def _price_confirms(direction: FlowDirection, price_move_pct: float | None) -> bool:
    if price_move_pct is None:
        return False
    if direction == "buy":
        return price_move_pct > 0
    return price_move_pct < 0


def _price_moves_against(direction: FlowDirection, price_move_pct: float | None) -> bool:
    if price_move_pct is None:
        return False
    if direction == "buy":
        return price_move_pct < 0
    return price_move_pct > 0


def _impact_score(
    *,
    flow_tao: float,
    relative_flow_pct: float,
    threshold_pct: float,
    price_confirmed: bool,
) -> float:
    relative_multiple = relative_flow_pct / threshold_pct
    absolute_multiple = abs(flow_tao) / config.FLOW_IMPULSE_MIN_TAO
    score = 50.0
    score += 25.0 * min(max(relative_multiple - 1.0, 0.0), 2.0) / 2.0
    score += 15.0 * min(max(absolute_multiple - 1.0, 0.0), 4.0) / 4.0
    if price_confirmed:
        score += 10.0
    return round(_clamp(score), 2)


def classify_flow_impulse(
    current: SubnetSnapshot,
    previous: SubnetSnapshot | None = None,
) -> FlowImpulse | None:
    flow = _finite_number(current.net_tao_flow_tao)
    pool = _finite_number(current.alpha_mcap_tao)
    if flow is None or pool is None or pool <= 0:
        return None
    if flow == 0:
        return None
    if abs(flow) < config.FLOW_IMPULSE_MIN_TAO:
        return None
    alpha_mcap_usd = _valid_market_cap(current.alpha_mcap_usd)
    if alpha_mcap_usd is not None and alpha_mcap_usd < config.FLOW_IMPULSE_MIN_MCAP_USD:
        return None

    direction: FlowDirection = "buy" if flow > 0 else "sell"
    alert_type: FlowAlertType = "important_buy" if direction == "buy" else "important_sell"
    threshold = (
        config.FLOW_IMPULSE_BUY_PCT
        if direction == "buy"
        else config.FLOW_IMPULSE_SELL_PCT
    )
    relative = abs(flow) / pool
    if relative < threshold:
        return None

    price_move = _price_move_pct(current, previous)
    price_confirmed = _price_confirms(direction, price_move)
    reasons: list[str] = [
        f"{direction} pressure {relative * 100:.1f}% of pool",
        "emission-adjusted net flow",
    ]
    risks: list[str] = []
    if price_confirmed:
        reasons.append("price confirmed impulse direction")
    elif _price_moves_against(direction, price_move):
        risks.append("price moved against impulse")

    turnover = _volume_turnover_pct(current)
    score = _impact_score(
        flow_tao=flow,
        relative_flow_pct=relative,
        threshold_pct=threshold,
        price_confirmed=price_confirmed,
    )

    return FlowImpulse(
        netuid=current.netuid,
        direction=direction,
        alert_type=alert_type,
        source="snapshot_net_flow",
        flow_tao=round(flow, 6),
        relative_flow_pct=round(relative, 6),
        threshold_pct=threshold,
        impact_score=score,
        price_move_pct=round(price_move, 4) if price_move is not None else None,
        volume_turnover_pct=round(turnover, 4) if turnover is not None else None,
        buy_slippage_pct=_valid_slippage_pct(current.buy_slippage_pct),
        sell_slippage_pct=_valid_slippage_pct(current.sell_slippage_pct),
        reasons=tuple(reasons),
        risks=tuple(risks),
    )
