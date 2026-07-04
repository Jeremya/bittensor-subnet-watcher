# Pump Radar Phase 2 — Regime & Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Market tide dial (magnitude + breadth) with Telegram on confirmed regime flips, plus a persisted per-subnet relative-strength percentile.

**Architecture:** New pure-function `engine/regime.py` (conditions.py style); one new `market_state` table + one new snapshot column (`rel_strength_score`); regime flips route through the existing condition state machine (sentinel netuid −1); dashboard banner + digest upgrade. Spec: `docs/superpowers/specs/2026-07-04-pump-radar-phase2-regime-design.md`.

**Tech Stack:** Python 3.13, aiosqlite, FastAPI/Jinja2, pytest. Branch: `pump-radar-phase2`. Verify each task with `python -m pytest tests/ -q` (417 passing at start).

**Codebase facts:** `history_by_netuid` is newest-first; wrap SQLite time comparisons in `datetime()`; netuid −1 = sentinel; `advance_condition(db, netuid, condition, breached, value)` returns `'entered'/'recovered'/None`, `breached=None` freezes; alert emoji map in `bot/telegram.py`.

---

## Task 1: Schema, config, model column

**Files:** Modify `config.py`, `db/database.py`, `models.py`, `scripts/signal_leadlag.py`

- [ ] **1.1** `config.py`, after the Pump radar block:

```python
# ── Market regime (tide + breadth; thresholds tunable via lead/lag harness) ──
REGIME_RISK_ON_TIDE_PCT: float = 0.003     # 24h aggregate net inflow >= 0.3% of total pool
REGIME_RISK_ON_BREADTH: float = 0.55       # AND >= 55% of subnets individually inflowing
REGIME_RISK_OFF_TIDE_PCT: float = -0.003   # tide <= -0.3% -> risk_off
REGIME_RISK_OFF_BREADTH: float = 0.35      # OR breadth <= 35% -> risk_off
```

- [ ] **1.2** `db/database.py`: add to `SCHEMA_SQL` before the index block:

```sql
CREATE TABLE IF NOT EXISTS market_state (
    polled_at     TEXT PRIMARY KEY,
    tide_pct      REAL NOT NULL,
    breadth_pct   REAL NOT NULL,
    flows_24h_tao REAL NOT NULL,
    regime        TEXT NOT NULL
);
```

Add `rel_strength_score REAL` to the snapshots CREATE TABLE (after `spec421_score`), `"market_state"` to `_EXPECTED_TABLES`, `"rel_strength_score"` to `_EXPECTED_SNAPSHOT_COLS` AND the `ADD COLUMN` migration list, and wire `rel_strength_score` into `insert_snapshot` (column list + value tuple: `snap.rel_strength_score`).

- [ ] **1.3** `models.py`: `rel_strength_score: Optional[float] = None` after `spec421_score`.
- [ ] **1.4** `scripts/signal_leadlag.py`: append `"rel_strength_score"` to `SIGNAL_COLUMNS`.
- [ ] **1.5** Run `python -m pytest tests/ -q` (all pass — schema tests cover idempotent migration), commit: `feat: market_state table and rel_strength_score column`

---

## Task 2: engine/regime.py — tide, classify, relative strength

**Files:** Create `engine/regime.py`, `tests/engine/test_regime.py`

- [ ] **2.1** Failing tests — `tests/engine/test_regime.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

import config
from db.database import init_db, insert_snapshot
from engine.regime import (
    TideReading,
    apply_rel_strength,
    classify_regime,
    compute_tide,
)
from models import SubnetSnapshot

NOW = datetime.now(timezone.utc)


def _snap(netuid, *, hours_ago=0.0, price=None, flow=None, tao_in=1000.0):
    return SubnetSnapshot(
        netuid=netuid, polled_at=NOW - timedelta(hours=hours_ago),
        alpha_price_tao=price, net_tao_flow_tao=flow, tao_in_tao=tao_in)


@pytest.mark.asyncio
async def test_compute_tide_magnitude_and_breadth(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        # netuid 1: +30 flow, netuid 2: -10, netuid 3: +2 -> total +22 over pool 3000
        for netuid, flow in ((1, 30.0), (2, -10.0), (3, 2.0)):
            await insert_snapshot(db, _snap(netuid, hours_ago=1, flow=flow))
        reading = await compute_tide(db)
        assert reading.flows_24h_tao == pytest.approx(22.0)
        assert reading.tide_pct == pytest.approx(22.0 / 3000.0)
        assert reading.breadth_pct == pytest.approx(2 / 3)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_compute_tide_none_without_flow_data(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(1, hours_ago=1, flow=None))
        assert await compute_tide(db) is None
    finally:
        await db.close()


def test_classify_regime_boundaries():
    def r(tide, breadth):
        return classify_regime(TideReading(tide, breadth, 0.0, 1000.0))
    assert r(0.004, 0.60) == "risk_on"
    assert r(0.004, 0.50) == "neutral"      # magnitude without breadth
    assert r(0.001, 0.90) == "neutral"
    assert r(-0.004, 0.50) == "risk_off"
    assert r(0.001, 0.30) == "risk_off"     # breadth collapse alone
    assert classify_regime(None) is None


def test_rel_strength_percentile_rank():
    snaps = [
        _snap(1, price=1.10),   # +10% -> strongest
        _snap(2, price=1.00),   # flat
        _snap(3, price=0.90),   # -10% -> weakest
        _snap(4, price=1.00),   # no history -> None
    ]
    history = {
        1: [_snap(1, hours_ago=24, price=1.0)],
        2: [_snap(2, hours_ago=24, price=1.0)],
        3: [_snap(3, hours_ago=24, price=1.0)],
        4: [],
    }
    apply_rel_strength(snaps, history)
    assert snaps[0].rel_strength_score > snaps[1].rel_strength_score > snaps[2].rel_strength_score
    assert snaps[3].rel_strength_score is None
    assert 0.0 <= snaps[2].rel_strength_score <= 100.0


def test_rel_strength_requires_reference_within_tolerance():
    snaps = [_snap(1, price=2.0)]
    history = {1: [_snap(1, hours_ago=60, price=1.0)]}   # too old (> 28h)
    apply_rel_strength(snaps, history)
    assert snaps[0].rel_strength_score is None
```

Run: `python -m pytest tests/engine/test_regime.py -q` → ImportError.

- [ ] **2.2** Implement `engine/regime.py`:

```python
"""Market regime (tide) and relative-strength rotation.

The tide is the aggregate 24h net TAO flow across all subnets, as a fraction
of total pool; breadth is the share of subnets individually inflowing. Both
must agree for risk_on (a single whale in one subnet is not a tide). Regime
flips route through the condition state machine (sentinel netuid -1) so
Telegram fires once per confirmed flip. rel_strength_score is a persisted
0-100 percentile of 24h price return vs the market - backtestable from day 1.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

import config
from models import SubnetSnapshot


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
```

- [ ] **2.3** Run `python -m pytest tests/engine/test_regime.py -q` → pass; full suite; commit: `feat: tide computation, regime classification, relative strength`

---

## Task 3: evaluate_regime + poll_cycle wiring

**Files:** Modify `engine/regime.py`, `main.py`, `bot/telegram.py`; Test `tests/engine/test_regime.py`

- [ ] **3.1** Failing tests — append to `tests/engine/test_regime.py`:

```python
from engine.regime import evaluate_regime, get_latest_market_state


async def _seed_risk_on(db):
    """24h of broad inflows: tide +5% of pool, breadth 100%, RS populated."""
    for netuid in (1, 2, 3):
        s = _snap(netuid, hours_ago=1, flow=50.0, price=1.0, tao_in=1000.0)
        s.rel_strength_score = 50.0 + netuid    # so the flip message has leaders
        await insert_snapshot(db, s)


@pytest.mark.asyncio
async def test_evaluate_regime_records_state_and_fires_once(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _seed_risk_on(db)
        fired = []
        for _ in range(3):                      # 2-poll hysteresis then steady
            fired += await evaluate_regime(db, {1: {"name": "Apex"}})
        flips = [a for a in fired if a.alert_type == "regime_flip"]
        assert len(flips) == 1
        assert "risk-ON" in flips[0].description
        assert "Apex" in flips[0].description        # leaders listed by name
        state = await get_latest_market_state(db)
        assert state["regime"] == "risk_on"
        cur = await db.execute("SELECT COUNT(*) FROM market_state")
        assert (await cur.fetchone())[0] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_evaluate_regime_freezes_without_data(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        fired = await evaluate_regime(db, {})
        assert fired == []
        assert await get_latest_market_state(db) is None
    finally:
        await db.close()
```

(For the top-RS line: `_seed_risk_on` snapshots carry `rel_strength_score` NULL — the message must fall back to plain SN names or omit the list; the test accepts either via the `or` assertion.)

- [ ] **3.2** Append to `engine/regime.py`:

```python
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
                          registry: dict) -> list["AlertRecord"]:
    """Record market_state and fire regime_flip alerts on confirmed transitions."""
    from db.database import insert_alert
    from engine.conditions import advance_condition
    from models import AlertRecord

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
```

- [ ] **3.3** Wire `main.py`: after `score_snapshots(...)` add:

```python
    from engine.regime import apply_rel_strength, evaluate_regime
    apply_rel_strength(chain_snapshots, history_by_netuid)
```

(place the import at module top with the other engine imports, not inline)
and after `await evaluate_ignition(...)`:

```python
    await evaluate_regime(_db, registry)
```

`bot/telegram.py` emoji map: `"regime_flip": "🌊",`.

- [ ] **3.4** Tests pass; full suite; commit: `feat: regime flips through condition machine with rotation leaders`

---

## Task 4: Dashboard banner, RS column, digest upgrade

**Files:** Modify `web/routes.py`, `web/templates/index.html`, `engine/digest.py`; Tests `tests/web/test_routes.py`, `tests/engine/test_digest.py`

- [ ] **4.1** Failing tests. Append to `tests/web/test_routes.py`:

```python
async def test_dashboard_shows_regime_banner(app, db):
    await db.execute(
        "INSERT INTO market_state (polled_at, tide_pct, breadth_pct, flows_24h_tao, regime)"
        " VALUES (?, 0.005, 0.7, 4200.0, 'risk_on')",
        (datetime.now(timezone.utc).isoformat(),))
    await db.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert "RISK-ON" in resp.text
```

Append to `tests/engine/test_digest.py`:

```python
@pytest.mark.asyncio
async def test_digest_tide_line_uses_market_state_when_present(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await db.execute(
            "INSERT INTO market_state (polled_at, tide_pct, breadth_pct, flows_24h_tao, regime)"
            " VALUES (?, 0.005, 0.7, 4200.0, 'risk_on')",
            (datetime.now(timezone.utc).isoformat(),))
        await db.commit()
        text = await build_daily_digest(db, registry={})
        assert "breadth 70%" in text and "RISK-ON" in text
    finally:
        await db.close()
```

- [ ] **4.2** `web/routes.py` `dashboard()`: add

```python
        from engine.regime import get_latest_market_state
        market_state = await get_latest_market_state(db)
```

pass `"market_state": market_state,` into the index context.

`web/templates/index.html`: directly after the `</header>` line (before the health strip) insert:

```html
{% if market_state %}
<div style="padding:6px 16px;font-family:monospace;font-size:0.8rem;border-bottom:1px solid #222;
            background:{{ '#0d2818' if market_state.regime == 'risk_on' else ('#2a0d0d' if market_state.regime == 'risk_off' else '#141414') }};
            color:{{ '#00d4aa' if market_state.regime == 'risk_on' else ('#ff5252' if market_state.regime == 'risk_off' else '#777') }};">
  🌊 {{ market_state.regime.replace('_', '-') | upper }} · tide {{ "%+.2f"|format(market_state.tide_pct * 100) }}% ·
  breadth {{ "%.0f"|format(market_state.breadth_pct * 100) }}% · {{ "%+,.0f"|format(market_state.flows_24h_tao) }} τ/24h
</div>
{% endif %}
```

RS column: run `grep -n "<th" web/templates/index.html` — add `<th>RS</th>` immediately after the score column header, and in the matching row loop add the cell (mirror the neighbouring td's class/format style):

```html
<td>{{ "%.0f"|format(s.rel_strength_score) if s.rel_strength_score is not none else "—" }}</td>
```

- [ ] **4.3** `engine/digest.py`: replace the tide block from Task 7 of Phase 1 — if a `market_state` row exists, use it; else keep the raw-sum fallback (pre-regime DBs and the existing test):

```python
    from engine.regime import get_latest_market_state
    state = await get_latest_market_state(db)
    if state is not None:
        direction = "flowing in" if state["flows_24h_tao"] >= 0 else "flowing out"
        lines.append(
            f"🌊 Tide: {state['flows_24h_tao']:+,.0f} τ {direction} · "
            f"breadth {state['breadth_pct'] * 100:.0f}% · "
            f"{state['regime'].replace('_', '-').upper()}")
    else:
        cursor = await db.execute(
            """SELECT SUM(net_tao_flow_tao) FROM snapshots
               WHERE datetime(polled_at) > datetime('now', '-24 hours')""")
        row = await cursor.fetchone()
        tide = row[0] if row and row[0] is not None else None
        if tide is not None:
            direction = "flowing in" if tide >= 0 else "flowing out"
            lines.append(f"🌊 Tide: {tide:+,.0f} τ {direction} (24h, all subnets)")
```

- [ ] **4.4** Tests pass; full suite; commit: `feat: regime banner, RS column, enriched digest tide line`

---

## Task 5: Final verification

- [ ] **5.1** Full suite green.
- [ ] **5.2** Replay check on a live-DB copy: `cp data/monitor.db /tmp/regime_check.db` then a one-off script call of `compute_tide`/`classify_regime` with `datetime('now', ...)` rebased — simpler: run `evaluate_regime` once against the copy and print the reading; confirm it returns a sane tide/breadth for current data (the Jun 17–18 window can't be replayed directly because compute_tide anchors to now; historical replay is the harness's job once market_state accrues — note this in TODOS as a known limitation).
- [ ] **5.3** Update TODOS.md (Phase 2 done; note market_state accrues from deploy, thresholds tunable once events + state overlap).
- [ ] **5.4** superpowers:finishing-a-development-branch → merge, push. Remind user: restart monitor.
