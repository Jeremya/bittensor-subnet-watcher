# Owner Locked-Alpha Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Daily sweep of owner lock state for all subnets into a new `owner_locks` history table, shown on the subnet detail page.

**Architecture:** New `collectors/locks.py` (mock-friendly: subtensor passed in), one table + two db helpers, one daily scheduler job, one template block. Spec: `docs/superpowers/specs/2026-07-05-owner-locks-design.md`.

**Tech Stack:** Python 3.13, aiosqlite, bittensor 10.5 (`get_coldkey_lock` returns `{'locked_mass': Balance, 'conviction': float, 'last_update': int}`). Branch `owner-locks`. Suite at 440.

---

## Task 1: Schema + db helpers

**Files:** Modify `db/database.py`; Test `tests/test_database.py`

- [ ] **1.1 Failing test** — append to `tests/test_database.py`:

```python
async def test_owner_lock_roundtrip(db):
    from db.database import insert_owner_lock, get_owner_locks_for_netuid
    t0 = datetime.now(timezone.utc) - timedelta(days=1)
    t1 = datetime.now(timezone.utc)
    await insert_owner_lock(db, 51, t0, locked_alpha=300_000.0,
                            locked_tao=15_000.0, locked_pct=0.18)
    await insert_owner_lock(db, 51, t1, locked_alpha=340_000.0,
                            locked_tao=17_900.0, locked_pct=0.20)
    rows = await get_owner_locks_for_netuid(db, 51, limit=2)
    assert len(rows) == 2
    assert rows[0]["locked_alpha"] == 340_000.0       # newest first
    assert rows[1]["locked_alpha"] == 300_000.0
```

- [ ] **1.2 Implement.** `SCHEMA_SQL` (before indexes):

```sql
CREATE TABLE IF NOT EXISTS owner_locks (
    netuid       INTEGER NOT NULL,
    checked_at   TEXT NOT NULL,
    locked_alpha REAL NOT NULL,
    locked_tao   REAL,
    locked_pct   REAL,
    PRIMARY KEY (netuid, checked_at)
);
```

Add `"owner_locks"` to `_EXPECTED_TABLES`. Helpers (near `get_collector_state`):

```python
async def insert_owner_lock(db: aiosqlite.Connection, netuid: int,
                            checked_at: datetime, *, locked_alpha: float,
                            locked_tao: Optional[float],
                            locked_pct: Optional[float]) -> None:
    await db.execute(
        """
        INSERT OR REPLACE INTO owner_locks
            (netuid, checked_at, locked_alpha, locked_tao, locked_pct)
        VALUES (?,?,?,?,?)
        """,
        (netuid, checked_at.isoformat(), locked_alpha, locked_tao, locked_pct),
    )
    await db.commit()


async def get_owner_locks_for_netuid(db: aiosqlite.Connection, netuid: int,
                                     limit: int = 2) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM owner_locks WHERE netuid=? ORDER BY checked_at DESC LIMIT ?",
        (netuid, limit),
    )
    return await cursor.fetchall()
```

- [ ] **1.3** Suite green; commit: `feat: owner_locks table and helpers`

---

## Task 2: Collector + daily job

**Files:** Create `collectors/locks.py`, `tests/collectors/test_locks.py`; Modify `main.py`

- [ ] **2.1 Failing tests** — create `tests/collectors/test_locks.py`:

```python
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from collectors.locks import LockCollector
from db.database import SCHEMA_SQL, get_owner_locks_for_netuid, insert_snapshot
from models import SubnetSnapshot


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


def _snap(netuid, owner="owner1", price=0.05, mcap_tao=1000.0):
    return SubnetSnapshot(
        netuid=netuid, polled_at=datetime.now(timezone.utc),
        owner_coldkey=owner, alpha_price_tao=price, alpha_mcap_tao=mcap_tao)


def _balance(alpha: float):
    return SimpleNamespace(tao=alpha)      # Balance exposes .tao as unit float


@pytest.mark.asyncio
async def test_collect_stores_lock_with_derived_values(db):
    await insert_snapshot(db, _snap(51, price=0.05, mcap_tao=1000.0))  # supply 20k
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(
        return_value={"locked_mass": _balance(4000.0), "conviction": 1.0,
                      "last_update": 8_557_923}))
    ok = await LockCollector.collect(st, db)
    assert ok == 1
    rows = await get_owner_locks_for_netuid(db, 51)
    assert rows[0]["locked_alpha"] == 4000.0
    assert rows[0]["locked_tao"] == pytest.approx(200.0)    # 4000 * 0.05
    assert rows[0]["locked_pct"] == pytest.approx(0.20)     # 4000 / 20000


@pytest.mark.asyncio
async def test_collect_none_lock_is_measured_zero(db):
    await insert_snapshot(db, _snap(51))
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(return_value=None))
    ok = await LockCollector.collect(st, db)
    assert ok == 1
    rows = await get_owner_locks_for_netuid(db, 51)
    assert rows[0]["locked_alpha"] == 0.0


@pytest.mark.asyncio
async def test_collect_error_skips_without_row(db):
    await insert_snapshot(db, _snap(51))
    await insert_snapshot(db, _snap(52, owner="owner2"))
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(
        side_effect=[RuntimeError("rpc"), None]))
    ok = await LockCollector.collect(st, db)
    assert ok == 1                                          # one skipped, one zero
    assert await get_owner_locks_for_netuid(db, 51) == [] or \
           await get_owner_locks_for_netuid(db, 52) == []


@pytest.mark.asyncio
async def test_collect_skips_missing_owner_and_handles_bad_price(db):
    await insert_snapshot(db, _snap(60, owner=None))         # no owner: skipped
    await insert_snapshot(db, _snap(61, price=None))         # lock stored, tao/pct NULL
    st = SimpleNamespace(get_coldkey_lock=AsyncMock(
        return_value={"locked_mass": _balance(10.0), "conviction": 1.0,
                      "last_update": 1}))
    ok = await LockCollector.collect(st, db)
    assert ok == 1
    rows = await get_owner_locks_for_netuid(db, 61)
    assert rows[0]["locked_alpha"] == 10.0
    assert rows[0]["locked_tao"] is None and rows[0]["locked_pct"] is None
```

- [ ] **2.2 Implement** `collectors/locks.py`:

```python
"""Owner locked-alpha sweep (daily).

Teams locking their alpha is a supply/conviction signal that cannot be
backfilled — collection starts early, consumers (lock-delta catalysts,
float-adjusted rotation) come once history accrues. `get_coldkey_lock`
returns {'locked_mass': Balance, 'conviction': float, 'last_update': block};
a None lock is a MEASURED zero (owner holds no lock), stored as 0.0, while a
query error stores nothing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_CONCURRENCY = 8


def _locked_alpha_from(lock: Optional[dict]) -> float:
    if not lock:
        return 0.0
    mass = lock.get("locked_mass")
    if mass is None:
        return 0.0
    return float(getattr(mass, "tao", mass))


class LockCollector:
    @staticmethod
    async def collect(subtensor, db: aiosqlite.Connection) -> int:
        """Sweep owner locks for every subnet with a known owner. Returns rows written."""
        from db.database import insert_owner_lock

        cursor = await db.execute(
            """
            SELECT s.netuid, s.owner_coldkey, s.alpha_price_tao, s.alpha_mcap_tao
            FROM snapshots s
            INNER JOIN (
                SELECT netuid, MAX(polled_at) AS mt FROM snapshots GROUP BY netuid
            ) latest ON s.netuid = latest.netuid AND s.polled_at = latest.mt
            WHERE s.owner_coldkey IS NOT NULL
            """
        )
        targets = await cursor.fetchall()

        now = datetime.now(timezone.utc)
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        written = zero = errors = 0

        async def sweep_one(row) -> Optional[tuple]:
            async with semaphore:
                try:
                    lock = await subtensor.get_coldkey_lock(
                        row["owner_coldkey"], row["netuid"])
                except Exception as exc:
                    logger.debug("[COLLECTOR] locks: netuid=%s error=%s",
                                 row["netuid"], exc)
                    return None
            return (row, _locked_alpha_from(lock))

        results = await asyncio.gather(*(sweep_one(r) for r in targets))
        for result in results:
            if result is None:
                errors += 1
                continue
            row, locked_alpha = result
            price = row["alpha_price_tao"]
            mcap = row["alpha_mcap_tao"]
            locked_tao = locked_pct = None
            if price and price > 0:
                locked_tao = locked_alpha * price
                if mcap and mcap > 0:
                    supply = mcap / price
                    if supply > 0:
                        locked_pct = locked_alpha / supply
            await insert_owner_lock(
                db, row["netuid"], now,
                locked_alpha=round(locked_alpha, 6),
                locked_tao=round(locked_tao, 6) if locked_tao is not None else None,
                locked_pct=round(locked_pct, 6) if locked_pct is not None else None,
            )
            written += 1
            if locked_alpha == 0.0:
                zero += 1

        logger.info("[COLLECTOR] name=locks ok=%d zero=%d errors=%d",
                    written, zero, errors)
        return written
```

- [ ] **2.3 Daily job** in `main.py` (function next to `pump_scan`, registration with the other jobs):

```python
async def lock_sweep() -> None:
    """Daily: owner locked-alpha sweep (history cannot be backfilled)."""
    from collectors.chain import _subtensor
    from collectors.locks import LockCollector
    if _subtensor:
        await LockCollector.collect(_subtensor, _db)
```

```python
    scheduler.add_job(
        lock_sweep, "interval", hours=24,
        max_instances=1, id="locks"
    )
```

Also run one sweep at startup so history starts today, next to the other
startup tasks: `asyncio.create_task(lock_sweep())` — but only after
`init_subtensor()` has completed (place it with the existing
`asyncio.create_task(registry_refresh_and_prune())` line, which has the same
dependency).

- [ ] **2.4** Suite green; commit: `feat: daily owner locked-alpha sweep`

---

## Task 3: Subnet page display + finish

**Files:** Modify `web/routes.py`, `web/templates/subnet.html`; Test `tests/web/test_routes.py`

- [ ] **3.1 Failing test** — append to `tests/web/test_routes.py`:

```python
async def test_subnet_page_shows_owner_lock(app, db):
    from db.database import insert_owner_lock
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=51, polled_at=now,
                                             alpha_price_tao=0.05))
    await insert_owner_lock(db, 51, now, locked_alpha=340_000.0,
                            locked_tao=17_000.0, locked_pct=0.20)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/51")
    assert "Owner lock" in resp.text
    assert "20.0%" in resp.text
```

- [ ] **3.2 Route**: in `subnet_detail`, next to the pump_events fetch:

```python
        owner_locks = await get_owner_locks_for_netuid(db, netuid, limit=2)
```

(import `get_owner_locks_for_netuid` in the db import block) and pass
`"owner_locks": owner_locks,` into the template context.

- [ ] **3.3 Template** — in `web/templates/subnet.html`, directly above the "Pump record" card:

```html
    {% if owner_locks %}
    {% set lk = owner_locks[0] %}
    <div class="card full-width" style="margin-top:16px">
      <h3>Owner lock</h3>
      <div class="alert-item">
        {% if lk.locked_alpha > 0 %}
        <div class="alert-type">🔒 {{ '{:,.0f}'.format(lk.locked_alpha) }} α
          {% if lk.locked_tao is not none %}(~{{ '{:,.0f}'.format(lk.locked_tao) }} τ{% if lk.locked_pct is not none %} · {{ "%.1f"|format(lk.locked_pct * 100) }}% of supply{% endif %}){% endif %}</div>
        {% else %}
        <div class="alert-type" style="color:#555">🔓 none — owner holds no lock</div>
        {% endif %}
        {% if owner_locks|length > 1 %}
        {% set delta = lk.locked_alpha - owner_locks[1].locked_alpha %}
        <div class="alert-desc">Δ {{ '{:+,.0f}'.format(delta) }} α since previous sweep</div>
        {% endif %}
        <div class="alert-time">{{ lk.checked_at[:16] }} UTC</div>
      </div>
    </div>
    {% endif %}
```

- [ ] **3.4** Suite green; live one-shot sweep against the chain (read-only) to seed today's history and eyeball SN51 ≈ 340k α; update TODOS (v1 done; consumers deferred); commit `feat: owner lock display on subnet page` → finishing-a-development-branch.
