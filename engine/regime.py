"""Market regime (tide) and relative-strength rotation.

The tide is the aggregate 24h net TAO flow across all subnets, as a fraction
of total pool; breadth is the share of subnets individually inflowing. Both
must agree for risk_on (a single whale in one subnet is not a tide). Regime
flips route through the condition state machine (sentinel netuid -1) so
Telegram fires once per confirmed flip. rel_strength_score is a persisted
0-100 percentile of 24h price return vs the market — backtestable from day 1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

import config
from models import AlertRecord, SubnetSnapshot


@dataclass(frozen=True)
class TideReading:
    tide_pct: float
    breadth_pct: float
    flows_24h_tao: float
    pool_tao: float


async def compute_tide(db: aiosqlite.Connection,
                       now: Optional[datetime] = None) -> Optional[TideReading]:
    cursor = await db.execute(
        """
        SELECT netuid, SUM(net_tao_flow_tao) AS flow
        FROM snapshots
        WHERE datetime(polled_at) > datetime('now', '-24 hours')
          AND net_tao_flow_tao IS NOT NULL
        GROUP BY netuid
        """
    )
    flows = {row["netuid"]: row["flow"] for row in await cursor.fetchall()}
    if not flows:
        return None    # no flow data: unknown, never a fake neutral

    cursor = await db.execute(
        """
        SELECT s.netuid, s.tao_in_tao
        FROM snapshots s
        INNER JOIN (
            SELECT netuid, MAX(polled_at) AS mt FROM snapshots GROUP BY netuid
        ) latest ON s.netuid = latest.netuid AND s.polled_at = latest.mt
        WHERE s.tao_in_tao IS NOT NULL AND s.tao_in_tao > 0
        """
    )
    pools = {row["netuid"]: row["tao_in_tao"] for row in await cursor.fetchall()}
    total_pool = sum(pools.values())
    if total_pool <= 0:
        return None

    total_flow = sum(flows.values())
    inflowing = sum(1 for f in flows.values() if f > 0)
    return TideReading(
        tide_pct=total_flow / total_pool,
        breadth_pct=inflowing / len(flows),
        flows_24h_tao=total_flow,
        pool_tao=total_pool,
    )


def classify_regime(reading: Optional[TideReading]) -> Optional[str]:
    if reading is None:
        return None
    if (reading.tide_pct >= config.REGIME_RISK_ON_TIDE_PCT
            and reading.breadth_pct >= config.REGIME_RISK_ON_BREADTH):
        return "risk_on"
    if (reading.tide_pct <= config.REGIME_RISK_OFF_TIDE_PCT
            or reading.breadth_pct <= config.REGIME_RISK_OFF_BREADTH):
        return "risk_off"
    return "neutral"


_RS_TARGET_HOURS = 24
_RS_TOLERANCE_HOURS = 4


def _return_24h(snap: SubnetSnapshot,
                history: list[SubnetSnapshot]) -> Optional[float]:
    if snap.alpha_price_tao is None or snap.alpha_price_tao <= 0:
        return None
    now = snap.polled_at
    target = now - timedelta(hours=_RS_TARGET_HOURS)
    oldest_ok = now - timedelta(hours=_RS_TARGET_HOURS + _RS_TOLERANCE_HOURS)
    # history is newest-first: first row at/before target is the nearest one
    for row in history:
        if row.polled_at > target:
            continue
        if row.polled_at < oldest_ok:
            return None
        if row.alpha_price_tao is None or row.alpha_price_tao <= 0:
            return None
        return snap.alpha_price_tao / row.alpha_price_tao - 1.0
    return None


def apply_rel_strength(snapshots: list[SubnetSnapshot],
                       history_by_netuid: dict[int, list[SubnetSnapshot]]) -> None:
    """Set rel_strength_score in-place: 0-100 percentile of 24h return."""
    returns: dict[int, float] = {}
    for snap in snapshots:
        r = _return_24h(snap, history_by_netuid.get(snap.netuid, []))
        if r is not None:
            returns[snap.netuid] = r

    values = sorted(returns.values())
    n = len(values)
    for snap in snapshots:
        r = returns.get(snap.netuid)
        if r is None or n == 0:
            snap.rel_strength_score = None
            continue
        below = sum(1 for v in values if v < r)
        equal = sum(1 for v in values if v == r)
        snap.rel_strength_score = round((below + 0.5 * equal) / n * 100.0, 2)


async def get_latest_market_state(db: aiosqlite.Connection) -> Optional[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM market_state ORDER BY polled_at DESC LIMIT 1")
    return await cursor.fetchone()


async def _top_rel_strength_names(db: aiosqlite.Connection, registry: dict,
                                  limit: int = 5) -> list[str]:
    from engine.alerts import _registry_name
    cursor = await db.execute(
        """
        SELECT s.netuid FROM snapshots s
        INNER JOIN (
            SELECT netuid, MAX(polled_at) AS mt FROM snapshots GROUP BY netuid
        ) latest ON s.netuid = latest.netuid AND s.polled_at = latest.mt
        WHERE s.rel_strength_score IS NOT NULL
        ORDER BY s.rel_strength_score DESC LIMIT ?
        """,
        (limit,),
    )
    return [_registry_name(registry, row["netuid"]) for row in await cursor.fetchall()]


async def evaluate_regime(db: aiosqlite.Connection,
                          registry: dict) -> list[AlertRecord]:
    """Record market_state and fire regime_flip alerts on confirmed transitions."""
    from db.database import insert_alert
    from engine.conditions import advance_condition

    reading = await compute_tide(db)
    regime = classify_regime(reading)
    now = datetime.now(timezone.utc)

    if reading is None:
        # Unknown market: freeze both conditions, record nothing.
        await advance_condition(db, -1, "market_risk_on", None)
        await advance_condition(db, -1, "market_risk_off", None)
        return []

    await db.execute(
        """
        INSERT OR REPLACE INTO market_state
            (polled_at, tide_pct, breadth_pct, flows_24h_tao, regime)
        VALUES (?,?,?,?,?)
        """,
        (now.isoformat(), reading.tide_pct, reading.breadth_pct,
         reading.flows_24h_tao, regime),
    )
    await db.commit()

    fired: list[AlertRecord] = []
    transitions = {
        "market_risk_on": await advance_condition(
            db, -1, "market_risk_on", regime == "risk_on", reading.tide_pct),
        "market_risk_off": await advance_condition(
            db, -1, "market_risk_off", regime == "risk_off", reading.tide_pct),
    }

    async def fire(description: str) -> None:
        alert = AlertRecord(
            fired_at=now, netuid=-1, subnet_name="Market",
            alert_type="regime_flip", description=description,
            current_value=round(reading.tide_pct, 6), threshold=None)
        await insert_alert(db, alert)
        fired.append(alert)

    tide_str = (f"tide {reading.tide_pct * 100:+.2f}% of pool "
                f"({reading.flows_24h_tao:+,.0f} τ/24h), "
                f"breadth {reading.breadth_pct * 100:.0f}%")

    if transitions["market_risk_on"] == "entered":
        leaders = await _top_rel_strength_names(db, registry)
        leader_str = ("Leading by relative strength: " + ", ".join(leaders) + "."
                      if leaders else "")
        await fire(f"Market risk-ON confirmed: {tide_str}. "
                   f"The tide is in. {leader_str}")
    elif transitions["market_risk_on"] == "recovered":
        await fire(f"Risk-on regime ended: {tide_str}.")

    if transitions["market_risk_off"] == "entered":
        await fire(f"Market risk-OFF confirmed: {tide_str} — capital leaving broadly.")
    elif transitions["market_risk_off"] == "recovered":
        await fire(f"Risk-off regime ended: {tide_str}.")

    return fired
