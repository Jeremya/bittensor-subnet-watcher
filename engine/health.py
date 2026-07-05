"""Collector health: data freshness + key-field null rates, computed from
existing tables at call time (no new collection infrastructure).

Feeds (a) /api/health + dashboard panel, (b) collector_stale_* sentinel
conditions (netuid -1) via the condition state machine, so a silently dead
collector — the failure class behind the never-populated X pipeline — pings
Telegram once and shows in every digest until fixed.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

import config

SENTINEL_NETUID = -1


@dataclass
class CollectorHealth:
    name: str
    last_success: Optional[str]          # ISO timestamp of newest evidence of life
    rows_24h: int
    null_rates: dict[str, float] = field(default_factory=dict)
    stale: bool = False
    reasons: list[str] = field(default_factory=list)


async def _newest(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> Optional[str]:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def _null_rates(db: aiosqlite.Connection, fields: list[str],
                      cutoff: str) -> tuple[int, dict[str, float]]:
    cols = ", ".join(f"SUM({f} IS NULL) AS n_{f}" for f in fields)
    cursor = await db.execute(
        f"SELECT COUNT(*) AS total, {cols} FROM snapshots WHERE polled_at > ?",
        (cutoff,),
    )
    row = await cursor.fetchone()
    total = row["total"]
    if total == 0:
        return 0, {f: 1.0 for f in fields}
    return total, {f: row[f"n_{f}"] / total for f in fields}


def _apply_staleness(h: CollectorHealth, max_age: timedelta,
                     now: datetime) -> None:
    if h.last_success is None:
        h.stale = True
        h.reasons.append("no data ever")
        return
    age = now - datetime.fromisoformat(h.last_success).replace(tzinfo=timezone.utc)
    if age > max_age:
        h.stale = True
        h.reasons.append(f"stale: last success {age.total_seconds() / 3600:.1f}h ago")
    for fld, rate in h.null_rates.items():
        if rate > config.HEALTH_NULL_RATE_MAX:
            h.stale = True
            h.reasons.append(f"null-rate {fld}: {rate * 100:.0f}%")


async def compute_collector_health(db: aiosqlite.Connection,
                                   now: Optional[datetime] = None) -> list[CollectorHealth]:
    now = now or datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    total, chain_nulls = await _null_rates(
        db, ["alpha_price_tao", "buy_slippage_pct"], cutoff_24h)
    chain = CollectorHealth(
        name="chain",
        last_success=await _newest(db, "SELECT MAX(polled_at) FROM snapshots"),
        rows_24h=total,
        null_rates=chain_nulls,
    )
    _apply_staleness(chain, timedelta(minutes=config.HEALTH_CHAIN_STALE_MINUTES), now)

    gh_total, gh_nulls = await _null_rates(db, ["gh_last_push"], cutoff_24h)
    # Prefer the success heartbeat: gh_* fields are carried forward between
    # snapshots, so "recent row with gh_stars" cannot distinguish a live
    # collector from a dead one (the 2026-07-02 expired-token failure hid
    # behind carried data for 3 days). Fall back for pre-heartbeat DBs.
    gh_heartbeat = await _newest(
        db, "SELECT value FROM collector_state WHERE key='github_last_success'")
    github = CollectorHealth(
        name="github",
        last_success=gh_heartbeat or await _newest(
            db, "SELECT MAX(polled_at) FROM snapshots WHERE gh_stars IS NOT NULL"),
        rows_24h=gh_total,
        null_rates=gh_nulls,
    )
    _apply_staleness(github, timedelta(hours=config.HEALTH_GITHUB_STALE_HOURS), now)

    checks = []
    for key in ("milestone_last_arxiv_check", "milestone_last_hf_check",
                "milestone_last_github_check"):
        val = await _newest(db, "SELECT value FROM collector_state WHERE key=?", (key,))
        if val:
            checks.append(val)
    milestone = CollectorHealth(
        name="milestone",
        last_success=max(checks) if checks else None,
        rows_24h=0,
    )
    _apply_staleness(milestone, timedelta(hours=config.HEALTH_MILESTONE_STALE_HOURS), now)

    return [chain, github, milestone]


async def sweep_collector_conditions(db: aiosqlite.Connection,
                                     now: Optional[datetime] = None) -> list[str]:
    """Advance collector_stale_* sentinel conditions from current health.

    Returns 'name:transition' strings for confirmed transitions."""
    from engine.conditions import advance_condition
    transitions: list[str] = []
    for h in await compute_collector_health(db, now):
        t = await advance_condition(
            db, SENTINEL_NETUID, f"collector_stale_{h.name}", h.stale,
            value=float(h.rows_24h),
        )
        if t:
            transitions.append(f"{h.name}:{t}")
    return transitions
