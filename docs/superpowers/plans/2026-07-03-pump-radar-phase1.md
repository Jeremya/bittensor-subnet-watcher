# Pump Radar Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pump-event registry + signal lead/lag harness + ignition detector, with the approved bundle (emergence fake-0 fix, tide digest line, runway-enriched alerts, /pumps page, subnet pump-record block).

**Architecture:** Two new pure-function engines (`engine/pump_events.py`, `engine/ignition.py`) in the style of `engine/conditions.py`; one new table (`pump_events`); an hourly scan job; two analysis scripts (`signal_leadlag.py`, `tune_ignition.py`); dashboard/digest additions. Spec: `docs/superpowers/specs/2026-07-03-pump-radar-phase1-design.md`.

**Tech Stack:** Python 3.13, aiosqlite, FastAPI/Jinja2, APScheduler, pytest. Work on branch `pump-radar-phase1`.

**Verification for every task:** `python -m pytest tests/ -q` (currently 393 passing).

**Codebase facts the engineer must know:**
- `history_by_netuid` in `main.py` is **newest-first** (DESC) and its `SubnetSnapshot` objects are built with a *subset* of fields — Task 5 adds `volume_24h_alpha` + slippage to that subset.
- `polled_at` strings use isoformat `T` separator: **always wrap SQLite time comparisons in `datetime()`** or same-day filters silently break.
- Swing/spec421/emergence *components* already return `score=None` on missing data; the only fake default found in audit is `emergence_score = 0.0` at `engine/emergence.py:186-188`.
- Netuid −1 is the established sentinel for non-subnet rows (collector health).
- Alert emoji map lives in `bot/telegram.py`; `🚀` is taken by github_spike — use `🔥` for ignition.

---

## Task 0: Branch

- [ ] `git checkout -b pump-radar-phase1`

---

## Task 1: Config, schema, and pump-event detection engine

**Files:**
- Modify: `config.py`, `db/database.py`
- Create: `engine/pump_events.py`
- Test: `tests/engine/test_pump_events.py`

- [ ] **Step 1.1: Config + schema**

`config.py`, new section after the Emergence block:

```python
# ── Pump radar (registry + ignition) ─────────────────────────────────────────
PUMP_MIN_RATIO: float = 1.5            # peak/local-min to qualify as a pump event
PUMP_WINDOW_HOURS: int = 72            # trailing window for the local min & post-peak timeout
PUMP_CLOSE_RETRACE: float = 0.5        # event closes after retracing 50% of the gain
PUMP_MIN_MCAP_USD: float = 250_000.0   # ignore micro-cap noise events
PUMP_MAX_GAP_HOURS: int = 6            # a data gap larger than this resets detection
```

`db/database.py` `SCHEMA_SQL` (before the index block), plus add `"pump_events"` to `_EXPECTED_TABLES`:

```sql
CREATE TABLE IF NOT EXISTS pump_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid         INTEGER NOT NULL,
    start_at       TEXT NOT NULL,
    peak_at        TEXT,
    end_at         TEXT,
    start_price    REAL NOT NULL,
    peak_price     REAL,
    end_price      REAL,
    ratio          REAL,
    retrace_pct    REAL,
    status         TEXT NOT NULL,      -- 'active' | 'closed'
    start_mcap_usd REAL,
    detected_at    TEXT NOT NULL,
    UNIQUE (netuid, start_at)
);
CREATE INDEX IF NOT EXISTS idx_pump_events_netuid ON pump_events (netuid, start_at DESC);
```

- [ ] **Step 1.2: Failing tests** — create `tests/engine/test_pump_events.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

import config
from engine.pump_events import PumpEvent, detect_pump_events
from models import SubnetSnapshot

# Recent base time: Task 2's scan_and_store filters to the trailing 7 days,
# so fixtures must not use a fixed calendar date.
T0 = datetime.now(timezone.utc) - timedelta(hours=2)


def series(prices, *, step_minutes=15, netuid=1, mcap_usd=1_000_000.0, owners=None):
    return [
        SubnetSnapshot(
            netuid=netuid,
            polled_at=T0 + timedelta(minutes=step_minutes * i),
            alpha_price_tao=p,
            alpha_mcap_usd=mcap_usd,
            owner_coldkey=(owners[i] if owners else "owner1"),
        )
        for i, p in enumerate(prices)
    ]


def test_detects_pump_and_tracks_peak():
    prices = [1.0, 1.0, 1.1, 1.6, 2.0, 1.9]        # 2.0x peak from 1.0 min
    events = detect_pump_events(series(prices))
    assert len(events) == 1
    ev = events[0]
    assert ev.start_price == 1.0
    assert ev.peak_price == 2.0
    assert ev.status == "active"                     # never retraced 50%


def test_no_event_below_threshold():
    assert detect_pump_events(series([1.0, 1.2, 1.4, 1.45])) == []


def test_event_closes_on_retrace():
    prices = [1.0, 1.6, 2.0, 1.4]                    # 1.4 <= 2.0 - 0.5*(2.0-1.0)
    events = detect_pump_events(series(prices))
    assert len(events) == 1
    ev = events[0]
    assert ev.status == "closed"
    assert ev.end_price == 1.4
    assert ev.retrace_pct == pytest.approx(0.6)      # (2.0-1.4)/(2.0-1.0)


def test_gap_resets_detection():
    snaps = series([1.0, 1.0])
    late = series([1.6, 2.0], netuid=1)
    for i, s in enumerate(late):                     # 12h gap before the rise
        s.polled_at = snaps[-1].polled_at + timedelta(hours=12) + timedelta(minutes=15 * i)
    assert detect_pump_events(snaps + late) == []    # rise not comparable across gap


def test_owner_change_resets_detection():
    owners = ["a", "a", "b", "b"]
    assert detect_pump_events(series([1.0, 1.0, 1.6, 2.0], owners=owners)) == []


def test_micro_cap_ignored():
    assert detect_pump_events(series([1.0, 1.6, 2.0], mcap_usd=50_000.0)) == []


def test_second_event_after_close():
    prices = [1.0, 2.0, 1.2, 1.2, 2.0]               # close, then re-pump from 1.2
    events = detect_pump_events(series(prices))
    assert len(events) == 2
    assert events[0].status == "closed"
    assert events[1].start_price == 1.2
```

- [ ] **Step 1.3:** Run `python -m pytest tests/engine/test_pump_events.py -q` — expect `ModuleNotFoundError: engine.pump_events`.

- [ ] **Step 1.4: Implement** `engine/pump_events.py`:

```python
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
```

- [ ] **Step 1.5:** Run `python -m pytest tests/engine/test_pump_events.py -q` — all pass; then full suite.
- [ ] **Step 1.6:** Commit: `feat: pump-event detection engine and registry schema`

---

## Task 2: Persistence, hourly scan job, backfill script

**Files:**
- Modify: `engine/pump_events.py` (persistence + scan), `main.py` (job)
- Create: `scripts/backfill_pump_events.py`
- Test: `tests/engine/test_pump_events.py` (extend)

- [ ] **Step 2.1: Failing tests** — append to `tests/engine/test_pump_events.py`:

```python
from db.database import init_db, insert_snapshot
from engine.pump_events import scan_and_store, get_recent_pump_events


@pytest.mark.asyncio
async def test_scan_and_store_persists_and_is_idempotent(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for s in series([1.0, 1.6, 2.0, 1.4]):
            await insert_snapshot(db, s)
        n1 = await scan_and_store(db, since_days=7)
        n2 = await scan_and_store(db, since_days=7)
        assert n1 == 1
        rows = await get_recent_pump_events(db, limit=10)
        assert len(rows) == 1                      # idempotent, no duplicate
        assert rows[0]["status"] == "closed"
        assert rows[0]["ratio"] == pytest.approx(2.0)
        assert rows[0]["retrace_pct"] == pytest.approx(0.6)
    finally:
        await db.close()
```

Run — expect ImportError, then implement.

- [ ] **Step 2.2: Implement persistence** — append to `engine/pump_events.py`:

```python
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
```

- [ ] **Step 2.3: Wire hourly job** in `main.py` (next to the other job functions; register with scheduler):

```python
async def pump_scan() -> None:
    """Hourly: detect/refresh pump events over the trailing week."""
    from engine.pump_events import scan_and_store
    count = await scan_and_store(_db, since_days=7)
    logger.info("[PUMP_SCAN] events_upserted=%d", count)
```

```python
    scheduler.add_job(
        pump_scan, "interval", hours=1,
        max_instances=1, id="pump_scan"
    )
```

- [ ] **Step 2.4: Backfill script** — create `scripts/backfill_pump_events.py`:

```python
"""One-time backfill of pump_events over full snapshot history.

Usage: .venv/bin/python -m scripts.backfill_pump_events [--db PATH]
"""
import argparse
import asyncio

import config
from db.database import init_db
from engine.pump_events import scan_and_store, get_recent_pump_events


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()

    db = await init_db(args.db)
    try:
        count = await scan_and_store(db, since_days=3650)
        rows = await get_recent_pump_events(db, limit=500)
        print(f"upserted {count} events; registry now holds {len(rows)}")
        print(f"{'SN':>4} {'start':>16} {'ratio':>6} {'status':>7} {'retrace':>8}")
        for r in rows:
            retrace = f"{r['retrace_pct']:.0%}" if r["retrace_pct"] is not None else "-"
            print(f"{r['netuid']:>4} {r['start_at'][:16]:>16} "
                  f"{r['ratio']:>6.2f} {r['status']:>7} {retrace:>8}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 2.5:** Full suite green; commit: `feat: pump-event persistence, hourly scan, backfill script`

---

## Task 3: /pumps page + subnet pump-record block

**Files:**
- Modify: `web/routes.py`, `web/templates/subnet.html`
- Create: `web/templates/pumps.html`
- Test: `tests/web/test_routes.py` (extend, existing `app`/`db` fixtures)

- [ ] **Step 3.1: Failing tests** — append to `tests/web/test_routes.py`:

```python
async def test_pumps_page_lists_events(app, db):
    from engine.pump_events import scan_and_store
    now = datetime.now(timezone.utc)
    for i, p in enumerate([1.0, 1.6, 2.0, 1.4]):
        await insert_snapshot(db, SubnetSnapshot(
            netuid=7, polled_at=now - timedelta(minutes=15 * (3 - i)),
            alpha_price_tao=p, alpha_mcap_usd=1_000_000.0, owner_coldkey="o"))
    await scan_and_store(db, since_days=7)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/pumps")
    assert resp.status_code == 200
    assert "SN7" in resp.text and "2.00" in resp.text


async def test_subnet_page_shows_pump_record(app, db):
    from engine.pump_events import scan_and_store
    now = datetime.now(timezone.utc)
    for i, p in enumerate([1.0, 1.6, 2.0, 1.4]):
        await insert_snapshot(db, SubnetSnapshot(
            netuid=7, polled_at=now - timedelta(minutes=15 * (3 - i)),
            alpha_price_tao=p, alpha_mcap_usd=1_000_000.0, owner_coldkey="o"))
    await scan_and_store(db, since_days=7)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/7")
    assert "Pump record" in resp.text
```

- [ ] **Step 3.2: Routes** — in `web/routes.py` import `get_recent_pump_events, get_pump_events_for_netuid` from `engine.pump_events`; add to `subnet_detail` context: `"pump_events": await get_pump_events_for_netuid(db, netuid, limit=5)`; add route:

```python
    @app.get("/pumps", response_class=HTMLResponse)
    async def pumps_page(request: Request):
        events = await get_recent_pump_events(db, limit=100)
        registry = await get_registry(db)
        enriched = []
        for ev in events:
            row = dict(ev)
            reg = registry.get(ev["netuid"])
            row["name"] = (reg["name"] if reg else None) or f"SN{ev['netuid']}"
            # signals at T-6h before start (the "did anything lead it?" column)
            cursor = await db.execute(
                """
                SELECT swing_score, emergence_score, catalyst_score FROM snapshots
                WHERE netuid=? AND datetime(polled_at) <= datetime(?, '-6 hours')
                ORDER BY polled_at DESC LIMIT 1
                """,
                (ev["netuid"], ev["start_at"]),
            )
            lead = await cursor.fetchone()
            row["lead_swing"] = lead["swing_score"] if lead else None
            row["lead_emergence"] = lead["emergence_score"] if lead else None
            row["lead_catalyst"] = lead["catalyst_score"] if lead else None
            enriched.append(row)
        return templates.TemplateResponse(request, "pumps.html", {"events": enriched})
```

- [ ] **Step 3.3: Templates.** Create `web/templates/pumps.html` following `analysts.html`'s exact style block (header bar, dark mono table). Table columns: Subnet (link `/subnet/{netuid}`), Start, Ratio (`%.2f`), Status, Retrace (`%.0f%%` or `—`), Peak→ dates, and "Led by" cell rendering `swing {v}` / `emerg {v}` / `cata {v}` for each non-None lead value or `nothing` in dim style. Empty state: `No pump events recorded yet.`

In `web/templates/subnet.html`, after the Analyst Mentions card:

```html
    <div class="card full-width" style="margin-top:16px">
      <h3>Pump record</h3>
      {% for ev in pump_events %}
      <div class="alert-item">
        <div class="alert-type">🔥 {{ "%.2f"|format(ev.ratio) }}x on {{ ev.start_at[:10] }}</div>
        <div class="alert-desc">{{ ev.status }}{% if ev.retrace_pct is not none %} · retraced {{ "%.0f"|format(ev.retrace_pct * 100) }}%{% endif %}</div>
      </div>
      {% endfor %}
      {% if not pump_events %}<div style="color:#333;font-style:italic;font-size:0.78rem;">No pump events recorded.</div>{% endif %}
    </div>
```

- [ ] **Step 3.4:** Tests green; full suite; commit: `feat: /pumps page and subnet pump-record block`

---

## Task 4: Signal lead/lag harness

**Files:**
- Create: `scripts/signal_leadlag.py`
- Test: `tests/test_leadlag_script.py`

- [ ] **Step 4.1: Failing test** — create `tests/test_leadlag_script.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from db.database import init_db, insert_snapshot
from engine.pump_events import scan_and_store
from models import SubnetSnapshot
from scripts.signal_leadlag import run_leadlag


@pytest.mark.asyncio
async def test_leadlag_samples_signals_and_grades_alerts(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        now = datetime.now(timezone.utc)
        # 30h of pre-history with swing=80 (a "hit" at every offset), then a pump
        t = now - timedelta(hours=30)
        i = 0
        price = 1.0
        while t < now:
            if t > now - timedelta(hours=1):
                price = 2.0                       # the pump: last hour doubles
            await insert_snapshot(db, SubnetSnapshot(
                netuid=7, polled_at=t, alpha_price_tao=price,
                alpha_mcap_usd=1_000_000.0, owner_coldkey="o",
                swing_score=80.0, emergence_score=30.0))
            t += timedelta(minutes=15); i += 1
        # close the event with a retrace
        await insert_snapshot(db, SubnetSnapshot(
            netuid=7, polled_at=now, alpha_price_tao=1.2,
            alpha_mcap_usd=1_000_000.0, owner_coldkey="o"))
        await scan_and_store(db, since_days=7)

        report = await run_leadlag(db, threshold=70.0)
        assert report["event_count"] == 1
        sw = report["signals"]["swing_score"]
        assert sw["hit_rate"] == pytest.approx(1.0)     # 80 >= 70 before T0
        em = report["signals"]["emergence_score"]
        assert em["hit_rate"] == pytest.approx(0.0)     # 30 < 70
    finally:
        await db.close()
```

- [ ] **Step 4.2: Implement** `scripts/signal_leadlag.py`:

```python
"""Grade every persisted signal against recorded pump events.

For each CLOSED pump event, sample each signal column at T-24h, T-12h, T-6h,
T-1h and T0 relative to start_at (nearest snapshot at or before the offset,
within 2h — otherwise 'no data'; NULLs counted separately, never as 0).
A signal 'hit' an event if any pre-T0 sample >= threshold. Also grades
pump_ignition alerts: hit = fired within [start, start+6h]; late = (start+6h,
peak]; false = no event started within 72h after the alert.

Usage: .venv/bin/python -m scripts.signal_leadlag [--db PATH] [--threshold 70] [--output x.json]
"""
import argparse
import asyncio
import json
import statistics
from datetime import datetime, timedelta, timezone

import aiosqlite

import config
from db.database import init_db

SIGNAL_COLUMNS = [
    "swing_score", "spec421_score", "flow_score", "emergence_score",
    "reg_demand_score", "slot_fill_score", "flow_accel_score",
    "catalyst_score", "tradability_score",
]
OFFSETS_HOURS = [24, 12, 6, 1, 0]
SAMPLE_TOLERANCE_HOURS = 2


async def _sample(db, netuid: int, at: datetime, column: str):
    cursor = await db.execute(
        f"""
        SELECT {column}, polled_at FROM snapshots
        WHERE netuid=? AND datetime(polled_at) <= datetime(?)
        ORDER BY polled_at DESC LIMIT 1
        """,
        (netuid, at.isoformat()),
    )
    row = await cursor.fetchone()
    if row is None:
        return "no_data"
    age = at - datetime.fromisoformat(row["polled_at"])
    if age > timedelta(hours=SAMPLE_TOLERANCE_HOURS):
        return "no_data"
    return row[column]           # may be None (NULL) — reported as null_sample


async def run_leadlag(db: aiosqlite.Connection, threshold: float = 70.0) -> dict:
    cursor = await db.execute(
        "SELECT * FROM pump_events WHERE status='closed' ORDER BY start_at"
    )
    events = await cursor.fetchall()

    signals: dict[str, dict] = {}
    for col in SIGNAL_COLUMNS:
        hits, misses, unmeasurable = 0, 0, 0
        values_by_offset: dict[int, list[float]] = {h: [] for h in OFFSETS_HOURS}
        for ev in events:
            start = datetime.fromisoformat(ev["start_at"])
            pre_samples = []
            for h in OFFSETS_HOURS:
                s = await _sample(db, ev["netuid"], start - timedelta(hours=h), col)
                if isinstance(s, (int, float)):
                    values_by_offset[h].append(s)
                if h > 0:
                    pre_samples.append(s)
            numeric = [s for s in pre_samples if isinstance(s, (int, float))]
            if not numeric:
                unmeasurable += 1
            elif any(v >= threshold for v in numeric):
                hits += 1
            else:
                misses += 1
        measured = hits + misses
        signals[col] = {
            "hits": hits, "misses": misses, "unmeasurable": unmeasurable,
            "hit_rate": (hits / measured) if measured else None,
            "median_by_offset": {
                f"T-{h}h" if h else "T0": (round(statistics.median(v), 1) if v else None)
                for h, v in values_by_offset.items()
            },
        }

    # Grade pump_ignition alerts
    cursor = await db.execute(
        "SELECT netuid, fired_at FROM alerts WHERE alert_type='pump_ignition'"
    )
    alerts = await cursor.fetchall()
    grades = {"hit": 0, "late": 0, "false": 0}
    for alert in alerts:
        fired = datetime.fromisoformat(alert["fired_at"])
        grade = "false"
        for ev in events:
            if ev["netuid"] != alert["netuid"]:
                continue
            start = datetime.fromisoformat(ev["start_at"])
            peak = datetime.fromisoformat(ev["peak_at"]) if ev["peak_at"] else start
            if start <= fired <= start + timedelta(hours=6):
                grade = "hit"; break
            if start + timedelta(hours=6) < fired <= peak:
                grade = "late"
        grades[grade] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(events),
        "threshold": threshold,
        "signals": signals,
        "ignition_grades": grades,
    }


def _print_report(report: dict) -> None:
    print(f"Lead/lag over {report['event_count']} closed pump events "
          f"(threshold {report['threshold']})\n")
    print(f"{'signal':<22} {'hit-rate':>8} {'hits':>5} {'miss':>5} {'unmeas':>6}  medians T-24/12/6/1/0")
    for col, s in report["signals"].items():
        rate = f"{s['hit_rate']:.0%}" if s["hit_rate"] is not None else "—"
        meds = "/".join(str(v) if v is not None else "·"
                        for v in s["median_by_offset"].values())
        print(f"{col:<22} {rate:>8} {s['hits']:>5} {s['misses']:>5} "
              f"{s['unmeasurable']:>6}  {meds}")
    g = report["ignition_grades"]
    print(f"\nignition alerts: hit={g['hit']} late={g['late']} false={g['false']}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--threshold", type=float, default=70.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    db = await init_db(args.db)
    try:
        report = await run_leadlag(db, threshold=args.threshold)
        _print_report(report)
        if args.output:
            with open(args.output, "w") as fh:
                json.dump(report, fh, indent=2)
            print(f"\nwrote {args.output}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

- [ ] **Step 4.3:** Test green; full suite; commit: `feat: signal lead/lag harness against pump events`

---

## Task 5: Ignition detector + alert wiring + tuning script

**Files:**
- Modify: `config.py`, `main.py`, `engine/alerts.py`, `bot/telegram.py`
- Create: `engine/ignition.py`, `scripts/tune_ignition.py`
- Test: `tests/engine/test_ignition.py`

- [ ] **Step 5.1: Config** (same Pump radar section):

```python
IGNITION_PRICE_IMPULSE_PCT: float = 6.0   # 1-poll price move to consider ignition (tuned by scripts/tune_ignition.py)
IGNITION_VOLUME_EXPANSION: float = 1.5    # volume_24h vs 24h earlier (confirmation)
IGNITION_FLOW_PCT: float = 0.02           # net inflow as fraction of pool (confirmation)
IGNITION_COOLDOWN_HOURS: int = 6
IGNITION_CLUSTER_MIN: int = 3             # >= this many ignitions in one poll -> single cluster message
IGNITION_MAX_PREV_AGE_MINUTES: int = POLL_INTERVAL_MINUTES * 2   # outage gate
```

- [ ] **Step 5.2: Failing tests** — create `tests/engine/test_ignition.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

import config
from engine.ignition import detect_ignition
from models import SubnetSnapshot

NOW = datetime(2026, 7, 3, 12, 0, tzinfo=timezone.utc)


def snap(price, *, minutes_ago=0, vol=100_000.0, flow=0.0, pool=10_000.0,
         mcap=1_000_000.0):
    return SubnetSnapshot(
        netuid=1, polled_at=NOW - timedelta(minutes=minutes_ago),
        alpha_price_tao=price, volume_24h_alpha=vol, net_tao_flow_tao=flow,
        alpha_mcap_tao=pool, alpha_mcap_usd=mcap, buy_slippage_pct=1.0)


def hist(*snaps):
    """history newest-first, as poll_cycle provides it."""
    return sorted(snaps, key=lambda s: s.polled_at, reverse=True)


def test_fires_on_price_impulse_with_flow_confirmation():
    cur = snap(1.10, flow=300.0)                     # +10% and 3% of pool inflow
    h = hist(snap(1.0, minutes_ago=15), snap(1.0, minutes_ago=1440, vol=100_000.0))
    sig = detect_ignition(cur, h)
    assert sig is not None
    assert sig.price_impulse_pct == pytest.approx(10.0)


def test_fires_on_price_impulse_with_volume_confirmation():
    cur = snap(1.10, vol=200_000.0)                  # 2x the volume of 24h ago
    h = hist(snap(1.0, minutes_ago=15), snap(1.0, minutes_ago=1440, vol=100_000.0))
    assert detect_ignition(cur, h) is not None


def test_no_fire_without_confirmation():
    cur = snap(1.10)                                  # price impulse alone
    h = hist(snap(1.0, minutes_ago=15), snap(1.0, minutes_ago=1440, vol=100_000.0))
    assert detect_ignition(cur, h) is None


def test_no_fire_below_price_impulse():
    cur = snap(1.03, flow=300.0)
    h = hist(snap(1.0, minutes_ago=15))
    assert detect_ignition(cur, h) is None


def test_outage_gate_blocks_stale_prev():
    """First poll after an outage must never read as an impulse."""
    cur = snap(2.0, flow=300.0)
    h = hist(snap(1.0, minutes_ago=300))              # prev is 5h old
    assert detect_ignition(cur, h) is None


def test_no_fire_below_mcap_floor():
    cur = snap(1.10, flow=300.0, mcap=50_000.0)
    h = hist(snap(1.0, minutes_ago=15))
    assert detect_ignition(cur, h) is None
```

- [ ] **Step 5.3: Implement** `engine/ignition.py`:

```python
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
```

- [ ] **Step 5.4: History fields.** In `main.py`, the `history_by_netuid` SubnetSnapshot construction (poll_cycle) must also carry `volume_24h_alpha=r["volume_24h_alpha"]` and `buy_slippage_pct=r["buy_slippage_pct"]` — add both fields.

- [ ] **Step 5.5: Alert wiring.** In `engine/alerts.py` add (with runway helper):

```python
async def _pump_runway_line(db: aiosqlite.Connection) -> Optional[str]:
    cursor = await db.execute(
        """
        SELECT COUNT(*) n FROM pump_events WHERE status='closed'
        """
    )
    row = await cursor.fetchone()
    if row is None or row["n"] < 5:
        return None
    cursor = await db.execute(
        "SELECT ratio FROM pump_events WHERE status='closed' ORDER BY ratio"
    )
    ratios = [r["ratio"] for r in await cursor.fetchall()]
    median = ratios[len(ratios) // 2]
    return f"Median recorded pump peaked {(median - 1) * 100:+.0f}% above start."


async def evaluate_ignition(
    db: aiosqlite.Connection,
    snapshots: list[SubnetSnapshot],
    history_by_netuid: dict[int, list[SubnetSnapshot]],
    registry: dict,
) -> list[AlertRecord]:
    from engine.ignition import detect_ignition

    ignitions = []
    for snap in snapshots:
        sig = detect_ignition(snap, history_by_netuid.get(snap.netuid, []))
        if sig is None:
            continue
        if await is_alert_in_cooldown(db, snap.netuid, "pump_ignition",
                                      config.IGNITION_COOLDOWN_HOURS):
            continue
        ignitions.append((snap, sig))

    if not ignitions:
        return []

    runway = await _pump_runway_line(db)
    fired: list[AlertRecord] = []
    cluster = len(ignitions) >= config.IGNITION_CLUSTER_MIN

    for snap, sig in ignitions:
        parts = [
            f"Ignition: price +{sig.price_impulse_pct:.1f}% in one poll; "
            + "; ".join(sig.confirmations) + ".",
        ]
        if sig.buy_slippage_pct is not None:
            parts.append(
                f"Entering {config.TRADABILITY_REFERENCE_TAO:.0f}τ costs "
                f"~{sig.buy_slippage_pct:.1f}% slippage."
            )
        if runway:
            parts.append(runway)
        parts.append("Watch-only: ignition is not yet a validated buy signal.")
        alert = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=_registry_name(registry, snap.netuid),
            alert_type="pump_ignition",
            description=" ".join(parts),
            current_value=sig.price_impulse_pct,
            threshold=config.IGNITION_PRICE_IMPULSE_PCT,
            notified=cluster,          # cluster: individual rows stay silent
        )
        await insert_alert(db, alert)
        fired.append(alert)
        logger.info("[ALERT] pump_ignition netuid=%d impulse=%.1f%%",
                    snap.netuid, sig.price_impulse_pct)

    if cluster:
        names = ", ".join(_registry_name(registry, s.netuid) for s, _ in ignitions)
        summary = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=-1,
            subnet_name="Market",
            alert_type="pump_ignition",
            description=(f"Market-wide ignition: {len(ignitions)} subnets "
                         f"igniting this poll — {names}. Tide event likely."),
            current_value=float(len(ignitions)),
            threshold=float(config.IGNITION_CLUSTER_MIN),
        )
        await insert_alert(db, summary)
        fired.append(summary)

    return fired
```

Wire into `main.py` `poll_cycle` right after `evaluate_alerts` (import `evaluate_ignition` in the `engine.alerts` import list):

```python
    await evaluate_ignition(_db, chain_snapshots, history_by_netuid, registry)
```

`bot/telegram.py` emoji map: `"pump_ignition": "🔥",`.

- [ ] **Step 5.6: evaluate_ignition tests** — append to `tests/engine/test_ignition.py` (uses the `db` fixture pattern from `tests/engine/test_alerts.py`: in-memory aiosqlite + SCHEMA_SQL):

```python
import aiosqlite
from db.database import SCHEMA_SQL
from engine.alerts import evaluate_ignition


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


async def test_evaluate_ignition_fires_and_respects_cooldown(db):
    cur = snap(1.10, flow=300.0)
    h = {1: hist(snap(1.0, minutes_ago=15))}
    first = await evaluate_ignition(db, [cur], h, {})
    second = await evaluate_ignition(db, [cur], h, {})
    assert len(first) == 1 and first[0].alert_type == "pump_ignition"
    assert second == []                               # cooldown


async def test_cluster_collapses_to_single_notification(db):
    snaps, h = [], {}
    for n in (1, 2, 3):
        s = snap(1.10, flow=300.0); s.netuid = n
        p = snap(1.0, minutes_ago=15); p.netuid = n
        snaps.append(s); h[n] = hist(p)
    fired = await evaluate_ignition(db, snaps, h, {})
    assert len(fired) == 4                            # 3 individual + 1 summary
    cur = await db.execute(
        "SELECT COUNT(*) FROM alerts WHERE alert_type='pump_ignition' AND notified=0")
    assert (await cur.fetchone())[0] == 1             # only the summary reaches Telegram
```

- [ ] **Step 5.7: Tuning script** — create `scripts/tune_ignition.py`:

```python
"""Replay detect_ignition over history against recorded pump events.

Grid-searches (price impulse, flow pct) and reports events-caught vs
false-fires/day so config defaults are chosen from evidence, not vibes.

Usage: .venv/bin/python -m scripts.tune_ignition [--db PATH]
"""
import argparse
import asyncio
from datetime import datetime, timedelta

import config
from db.database import init_db
from engine.ignition import detect_ignition
from engine.pump_events import _row_to_snapshot


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()
    db = await init_db(args.db)
    try:
        cursor = await db.execute(
            """SELECT netuid, polled_at, alpha_price_tao, alpha_mcap_usd,
                      owner_coldkey, volume_24h_alpha, net_tao_flow_tao,
                      alpha_mcap_tao, buy_slippage_pct
               FROM snapshots ORDER BY netuid, polled_at""")
        rows = await cursor.fetchall()
        by_netuid: dict[int, list] = {}
        for r in rows:
            s = _row_to_snapshot(r)
            s.volume_24h_alpha = r["volume_24h_alpha"]
            s.net_tao_flow_tao = r["net_tao_flow_tao"]
            s.alpha_mcap_tao = r["alpha_mcap_tao"]
            s.buy_slippage_pct = r["buy_slippage_pct"]
            by_netuid.setdefault(r["netuid"], []).append(s)

        cursor = await db.execute("SELECT * FROM pump_events")
        events = await cursor.fetchall()
        starts = [(e["netuid"], datetime.fromisoformat(e["start_at"])) for e in events]
        total_days = max(1.0, len(rows) / 129 / 96)

        print(f"{'impulse%':>8} {'flow%':>6} {'caught':>7} {'of':>3} {'false/day':>9}")
        for impulse in (4.0, 6.0, 8.0, 10.0):
            for flow in (0.01, 0.02, 0.03):
                config.IGNITION_PRICE_IMPULSE_PCT = impulse
                config.IGNITION_FLOW_PCT = flow
                fires = []
                for netuid, series in by_netuid.items():
                    for i in range(1, len(series)):
                        sig = detect_ignition(series[i], list(reversed(series[:i])))
                        if sig:
                            fires.append((netuid, series[i].polled_at))
                caught = sum(
                    1 for en, es in starts
                    if any(fn == en and es <= ft <= es + timedelta(hours=6)
                           for fn, ft in fires))
                false = sum(
                    1 for fn, ft in fires
                    if not any(fn == en and es - timedelta(hours=1) <= ft <= es + timedelta(hours=72)
                               for en, es in starts))
                print(f"{impulse:>8.1f} {flow*100:>6.1f} {caught:>7} {len(starts):>3} "
                      f"{false / total_days:>9.2f}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

(Note: brute replay is O(rows); acceptable as an offline script. Restore config values are process-local.)

- [ ] **Step 5.8:** All tests green; full suite; commit: `feat: ignition detector with outage gate, cluster collapse, tuning script`

---

## Task 6: Emergence fake-0 fix + None-regression tests

**Files:**
- Modify: `engine/emergence.py:183-198`
- Test: `tests/engine/test_emergence.py` (extend)

- [ ] **Step 6.1: Failing test** — append to `tests/engine/test_emergence.py` (match its fixtures):

```python
def test_emergence_score_is_none_when_all_components_missing():
    """No data must persist as NULL, never a fake 0.0 (harness honesty)."""
    snap = SubnetSnapshot(netuid=1, polled_at=datetime.now(timezone.utc))
    sig = compute_emergence_signal(snap, history=[], first_seen_at=None)
    assert sig.emergence_score is None
```

- [ ] **Step 6.2: Fix** `compute_emergence_signal`: change the all-None branch to produce `None` (typed `Optional[float]` on `EmergenceSignal.emergence_score`), keep `round(_clamp(score), 2)` only when components exist. Grep consumers: `web/routes.py:381` filters `is not None` (OK), `check_emergence_watch` guards None (OK), `scripts/backfill_emergence*.py` — run its tests; fix any `>=` comparisons against None.

- [ ] **Step 6.3:** Full suite green. Commit message must flag semantics: `fix: emergence_score persists NULL (was fake 0.0) when no components — semantics change 2026-07-03, does not affect swing_score`

---

## Task 7: Digest tide line + ignition scorecard

**Files:**
- Modify: `engine/digest.py`
- Test: `tests/engine/test_digest.py` (extend)

- [ ] **Step 7.1: Failing tests** — append to `tests/engine/test_digest.py`:

```python
from db.database import insert_snapshot
from models import SubnetSnapshot
from datetime import timedelta


@pytest.mark.asyncio
async def test_digest_includes_tide_line(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        now = datetime.now(timezone.utc)
        for netuid, flow in ((1, 300.0), (2, -100.0)):
            await insert_snapshot(db, SubnetSnapshot(
                netuid=netuid, polled_at=now - timedelta(hours=1),
                net_tao_flow_tao=flow))
        text = await build_daily_digest(db, registry={})
        assert "Tide" in text and "+200" in text
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_digest_includes_ignition_scorecard_when_alerts_exist(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await db.execute(
            "INSERT INTO alerts (fired_at, netuid, subnet_name, alert_type, description)"
            " VALUES (?, 7, 'X', 'pump_ignition', 'd')",
            (datetime.now(timezone.utc).isoformat(),))
        await db.commit()
        text = await build_daily_digest(db, registry={})
        assert "ignition" in text.lower()
    finally:
        await db.close()
```

- [ ] **Step 7.2: Implement** in `build_daily_digest`, after the header line:

```python
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
```

- [ ] **Step 7.3:** Tests green; full suite; commit: `feat: tide line and ignition scorecard in daily digest`

---

## Task 8: Final verification & docs

- [ ] **F.1:** `python -m pytest tests/ -q` — full suite green.
- [ ] **F.2:** Backfill against a **copy** of the live DB: `cp data/monitor.db /tmp/pump_check.db && .venv/bin/python -m scripts.backfill_pump_events --db /tmp/pump_check.db` — expect ~15 events including SN16 with ratio ≈ 6.3.
- [ ] **F.3:** `.venv/bin/python -m scripts.tune_ignition --db /tmp/pump_check.db` — review the grid; if a row clearly dominates the config defaults (more caught, fewer false/day), update `config.py` and note the tuning date.
- [ ] **F.4:** `.venv/bin/python -m scripts.signal_leadlag --db /tmp/pump_check.db` — confirm the harness reproduces the CEO-review finding (low hit-rates across signals).
- [ ] **F.5:** Update TODOS.md: mark Phase 1 done; note "run backfill on live DB after merge + restart monitor".
- [ ] **F.6:** Use superpowers:finishing-a-development-branch (merge to main, tests on merged result, delete branch). Remind user: restart monitor, then run `scripts/backfill_pump_events.py` against the live DB.
