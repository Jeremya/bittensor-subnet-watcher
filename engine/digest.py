"""Daily digest of active chronic conditions and collector health."""
from datetime import datetime, timedelta, timezone

import aiosqlite

from engine.conditions import get_active_conditions, get_condition_transitions_since

SENTINEL_NETUID = -1
COLLECTOR_PREFIX = "collector_stale_"
MAX_NAMES_PER_CONDITION = 8


def _subnet_label(registry: dict, netuid: int) -> str:
    row = registry.get(netuid)
    name = None
    if row is not None:
        try:
            name = row.get("name") if isinstance(row, dict) else row["name"]
        except (KeyError, TypeError, IndexError):
            name = getattr(row, "name", None)
    return f"SN{netuid} {name}" if name else f"SN{netuid}"


async def build_daily_digest(db: aiosqlite.Connection,
                             registry: dict,
                             now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    active = await get_active_conditions(db)
    transitions = await get_condition_transitions_since(db, since)

    new_keys = {(r["netuid"], r["condition"]) for r in transitions
                if r["entered_at"] and r["entered_at"] > since}
    recovered = [r for r in transitions
                 if r["cleared_at"] and r["cleared_at"] > since]

    subnet_rows = [r for r in active if r["netuid"] != SENTINEL_NETUID]
    collector_rows = [r for r in active if r["netuid"] == SENTINEL_NETUID]

    lines = [f"📋 TAO Monitor digest — {now.strftime('%Y-%m-%d %H:%M UTC')}"]

    from engine.regime import get_latest_market_state
    state = await get_latest_market_state(db)
    if state is not None:
        direction = "flowing in" if state["flows_24h_tao"] >= 0 else "flowing out"
        lines.append(
            f"🌊 Tide: {state['flows_24h_tao']:+,.0f} τ {direction} · "
            f"breadth {state['breadth_pct'] * 100:.0f}% · "
            f"{state['regime'].replace('_', '-').upper()}")
    else:
        # Pre-regime fallback: raw 24h sum from snapshots.
        cursor = await db.execute(
            """SELECT SUM(net_tao_flow_tao) FROM snapshots
               WHERE datetime(polled_at) > datetime('now', '-24 hours')""")
        row = await cursor.fetchone()
        tide = row[0] if row and row[0] is not None else None
        if tide is not None:
            direction = "flowing in" if tide >= 0 else "flowing out"
            lines.append(f"🌊 Tide: {tide:+,.0f} τ {direction} (24h, all subnets)")

    cursor = await db.execute(
        """SELECT COUNT(*) FROM alerts
           WHERE alert_type='pump_ignition' AND netuid != -1
             AND datetime(fired_at) > datetime('now', '-30 days')""")
    fired_30d = (await cursor.fetchone())[0]
    if fired_30d:
        cursor = await db.execute(
            """SELECT COUNT(*) FROM alerts a
               WHERE a.alert_type='pump_ignition' AND a.netuid != -1
                 AND datetime(a.fired_at) > datetime('now', '-30 days')
                 AND EXISTS (
                     SELECT 1 FROM pump_events p
                     WHERE p.netuid = a.netuid
                       AND datetime(a.fired_at) BETWEEN datetime(p.start_at)
                           AND datetime(p.start_at, '+6 hours'))""")
        hits = (await cursor.fetchone())[0]
        lines.append(f"🔥 Ignition 30d: {fired_30d} fired, {hits} hit")

    if collector_rows:
        lines.append("\n🩺 Collector health:")
        for row in collector_rows:
            name = row["condition"].removeprefix(COLLECTOR_PREFIX)
            lines.append(f"  ⛔ {name} stale since {row['entered_at'][:16]}")

    if not subnet_rows and not collector_rows:
        lines.append("✅ All clear — no active conditions.")
        return "\n".join(lines)

    by_condition: dict[str, list] = {}
    for row in subnet_rows:
        by_condition.setdefault(row["condition"], []).append(row)

    if by_condition:
        lines.append("\n⚠️ Active conditions:")
        for condition in sorted(by_condition):
            rows = by_condition[condition]
            new_count = sum(1 for r in rows if (r["netuid"], r["condition"]) in new_keys)
            suffix = f" ({new_count} new)" if new_count else ""
            names = ", ".join(_subnet_label(registry, r["netuid"])
                              for r in rows[:MAX_NAMES_PER_CONDITION])
            if len(rows) > MAX_NAMES_PER_CONDITION:
                names += f", +{len(rows) - MAX_NAMES_PER_CONDITION} more"
            lines.append(f"  {condition}: {len(rows)} subnets{suffix} — {names}")

    sub_recovered = [r for r in recovered if r["netuid"] != SENTINEL_NETUID]
    if sub_recovered:
        names = ", ".join(_subnet_label(registry, r["netuid"]) for r in sub_recovered[:8])
        lines.append(f"\n✅ Recovered in last 24h: {len(sub_recovered)} — {names}")

    return "\n".join(lines)
