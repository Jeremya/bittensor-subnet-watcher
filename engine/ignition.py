"""Ignition detection: alert minutes into a pump instead of predicting it.

Rule: a 1-poll price impulse (mandatory) plus at least one confirmation
(volume expansion vs 24h earlier, or net-inflow surge). Hard gate: previous
snapshot must be fresh (<= IGNITION_MAX_PREV_AGE_MINUTES) — the first poll
after an outage must never read as an impulse (same bug class as the
backtest horizon fix).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

import config
from models import SubnetSnapshot


@dataclass(frozen=True)
class IgnitionSignal:
    netuid: int
    price_impulse_pct: float
    volume_expansion: Optional[float]     # multiple vs 24h ago, None if unknown
    flow_pct_of_pool: Optional[float]
    confirmations: tuple[str, ...]
    buy_slippage_pct: Optional[float]


def _nearest_24h_ago(history: list[SubnetSnapshot],
                     now) -> Optional[SubnetSnapshot]:
    target = now - timedelta(hours=24)
    candidates = [s for s in history
                  if s.volume_24h_alpha is not None and s.polled_at <= target]
    return candidates[0] if candidates else None      # history is newest-first


def detect_ignition(snap: SubnetSnapshot,
                    history: list[SubnetSnapshot]) -> Optional[IgnitionSignal]:
    if not history:
        return None
    prev = history[0]
    if snap.alpha_price_tao is None or prev.alpha_price_tao is None:
        return None
    if prev.alpha_price_tao <= 0:
        return None
    if snap.alpha_mcap_usd is None or snap.alpha_mcap_usd < config.PUMP_MIN_MCAP_USD:
        return None
    # Outage gate: stale prev = fake impulse.
    age = snap.polled_at - prev.polled_at
    if age > timedelta(minutes=config.IGNITION_MAX_PREV_AGE_MINUTES):
        return None

    impulse = (snap.alpha_price_tao / prev.alpha_price_tao - 1.0) * 100.0
    if impulse < config.IGNITION_PRICE_IMPULSE_PCT:
        return None

    confirmations: list[str] = []
    expansion = None
    ref = _nearest_24h_ago(history, snap.polled_at)
    if (ref is not None and snap.volume_24h_alpha is not None
            and ref.volume_24h_alpha and ref.volume_24h_alpha > 0):
        expansion = snap.volume_24h_alpha / ref.volume_24h_alpha
        if expansion >= config.IGNITION_VOLUME_EXPANSION:
            confirmations.append(f"volume {expansion:.1f}x vs 24h ago")

    flow_pct = None
    if (snap.net_tao_flow_tao is not None and snap.alpha_mcap_tao
            and snap.alpha_mcap_tao > 0):
        flow_pct = snap.net_tao_flow_tao / snap.alpha_mcap_tao
        if flow_pct >= config.IGNITION_FLOW_PCT:
            confirmations.append(f"net inflow {flow_pct * 100:.1f}% of pool")

    if not confirmations:
        return None

    return IgnitionSignal(
        netuid=snap.netuid,
        price_impulse_pct=round(impulse, 2),
        volume_expansion=round(expansion, 2) if expansion is not None else None,
        flow_pct_of_pool=round(flow_pct, 4) if flow_pct is not None else None,
        confirmations=tuple(confirmations),
        buy_slippage_pct=snap.buy_slippage_pct,
    )
