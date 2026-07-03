"""Pump-event detection: the evidence registry behind the Pump Radar.

Pure single-pass detection over an ascending price series. An event starts at
the trailing-window local minimum once price reaches PUMP_MIN_RATIO x that
minimum; it closes after retracing PUMP_CLOSE_RETRACE of the gain (or timing
out PUMP_WINDOW_HOURS past the peak). Data gaps and owner changes (recycled
netuids) hard-reset detection — no event may straddle either.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

import config
from models import SubnetSnapshot


@dataclass
class PumpEvent:
    netuid: int
    start_at: datetime
    start_price: float
    start_mcap_usd: Optional[float]
    peak_at: datetime
    peak_price: float
    status: str                      # 'active' | 'closed'
    end_at: Optional[datetime] = None
    end_price: Optional[float] = None

    @property
    def ratio(self) -> float:
        return self.peak_price / self.start_price

    @property
    def retrace_pct(self) -> Optional[float]:
        if self.status != "closed" or self.end_price is None:
            return None
        gain = self.peak_price - self.start_price
        if gain <= 0:
            return None
        return (self.peak_price - self.end_price) / gain


def _close(event: PumpEvent, snap: SubnetSnapshot) -> PumpEvent:
    event.status = "closed"
    event.end_at = snap.polled_at
    event.end_price = snap.alpha_price_tao
    return event


def detect_pump_events(series: list[SubnetSnapshot]) -> list[PumpEvent]:
    """series: one netuid, ascending polled_at. Returns events oldest-first."""
    window_td = timedelta(hours=config.PUMP_WINDOW_HOURS)
    gap_td = timedelta(hours=config.PUMP_MAX_GAP_HOURS)

    events: list[PumpEvent] = []
    active: Optional[PumpEvent] = None
    window: list[SubnetSnapshot] = []      # trailing candidates for the local min
    prev: Optional[SubnetSnapshot] = None

    for snap in series:
        price = snap.alpha_price_tao
        if price is None or price <= 0:
            continue

        if prev is not None:
            gap = (snap.polled_at - prev.polled_at) > gap_td
            owner_changed = (snap.owner_coldkey is not None
                             and prev.owner_coldkey is not None
                             and snap.owner_coldkey != prev.owner_coldkey)
            if gap or owner_changed:
                if active is not None:
                    events.append(_close(active, prev))
                    active = None
                window.clear()

        cutoff = snap.polled_at - window_td
        window = [s for s in window if s.polled_at >= cutoff]

        if active is None:
            # Latest minimum on ties: the pump starts at the LAST local low
            # before the breakout, not the first of a flat stretch (also what
            # makes lead/lag offsets land on real pre-pump snapshots).
            low = (min(reversed(window), key=lambda s: s.alpha_price_tao)
                   if window else None)
            if (low is not None
                    and price >= low.alpha_price_tao * config.PUMP_MIN_RATIO
                    and low.alpha_mcap_usd is not None
                    and low.alpha_mcap_usd >= config.PUMP_MIN_MCAP_USD):
                active = PumpEvent(
                    netuid=snap.netuid,
                    start_at=low.polled_at,
                    start_price=low.alpha_price_tao,
                    start_mcap_usd=low.alpha_mcap_usd,
                    peak_at=snap.polled_at,
                    peak_price=price,
                    status="active",
                )
                window.clear()
            else:
                window.append(snap)
        else:
            if price > active.peak_price:
                active.peak_price = price
                active.peak_at = snap.polled_at
            gain = active.peak_price - active.start_price
            retrace_level = active.peak_price - config.PUMP_CLOSE_RETRACE * gain
            timed_out = (snap.polled_at - active.peak_at) > window_td
            if price <= retrace_level or timed_out:
                events.append(_close(active, snap))
                active = None
                window.append(snap)

        prev = snap

    if active is not None:
        events.append(active)
    return events


async def upsert_pump_event(db: aiosqlite.Connection, ev: PumpEvent) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT INTO pump_events
            (netuid, start_at, peak_at, end_at, start_price, peak_price,
             end_price, ratio, retrace_pct, status, start_mcap_usd, detected_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(netuid, start_at) DO UPDATE SET
            peak_at=excluded.peak_at, end_at=excluded.end_at,
            peak_price=excluded.peak_price, end_price=excluded.end_price,
            ratio=excluded.ratio, retrace_pct=excluded.retrace_pct,
            status=excluded.status
        """,
        (ev.netuid, ev.start_at.isoformat(),
         ev.peak_at.isoformat(),
         ev.end_at.isoformat() if ev.end_at else None,
         ev.start_price, ev.peak_price, ev.end_price,
         round(ev.ratio, 4),
         round(ev.retrace_pct, 4) if ev.retrace_pct is not None else None,
         ev.status, ev.start_mcap_usd, now),
    )
    await db.commit()


def _row_to_snapshot(row) -> SubnetSnapshot:
    return SubnetSnapshot(
        netuid=row["netuid"],
        polled_at=datetime.fromisoformat(row["polled_at"]),
        alpha_price_tao=row["alpha_price_tao"],
        alpha_mcap_usd=row["alpha_mcap_usd"],
        owner_coldkey=row["owner_coldkey"],
    )


async def scan_and_store(db: aiosqlite.Connection, since_days: int = 7) -> int:
    """Detect and upsert pump events over the trailing window. Returns event count."""
    cursor = await db.execute(
        """
        SELECT netuid, polled_at, alpha_price_tao, alpha_mcap_usd, owner_coldkey
        FROM snapshots
        WHERE datetime(polled_at) > datetime('now', ?)
        ORDER BY netuid, polled_at
        """,
        (f"-{since_days} days",),
    )
    rows = await cursor.fetchall()
    by_netuid: dict[int, list[SubnetSnapshot]] = {}
    for row in rows:
        by_netuid.setdefault(row["netuid"], []).append(_row_to_snapshot(row))

    count = 0
    for series in by_netuid.values():
        for ev in detect_pump_events(series):
            await upsert_pump_event(db, ev)
            count += 1
    return count


async def get_recent_pump_events(db: aiosqlite.Connection,
                                 limit: int = 100) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM pump_events ORDER BY start_at DESC LIMIT ?", (limit,)
    )
    return await cursor.fetchall()


async def get_pump_events_for_netuid(db: aiosqlite.Connection, netuid: int,
                                     limit: int = 10) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM pump_events WHERE netuid=? ORDER BY start_at DESC LIMIT ?",
        (netuid, limit),
    )
    return await cursor.fetchall()
