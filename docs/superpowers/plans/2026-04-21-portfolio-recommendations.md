# Portfolio Recommendation Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `/portfolio` from a holdings ledger into an action-oriented portfolio review page that surfaces high-conviction `sell`, `trim`, `add`, and `new buy` recommendations for a 1-2 week swing horizon.

**Architecture:** Recommendations are computed server-side on each `/portfolio` request and are not persisted. A new `engine/recommendations.py` module builds a stable portfolio ledger, computes concentration-aware portfolio context, and returns two review sections: `Portfolio Actions` for held names and `New Candidates` for non-held subnets. The route remains glue code; the DB only gains helper queries for recent coverage and milestone activity.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, aiosqlite, pytest, existing `engine/alerts.py` and `db/database.py` helpers.

---

## Scope Decisions Locked In

- Recommendation style: `direction only`
- Recommendation sections: `Portfolio Actions` and `New Candidates`
- Time horizon: `1-2 week swing`
- Voice threshold: `high-conviction only`; default is `hold / no action`
- Concentration handling: `high importance`
- Full `sell`: `thesis break only`
- Policy type: `hybrid` — hard rules for `sell` and `trim`, weighted ranking for `add` and `new buy`
- UI hierarchy: actions first, opportunities second, holdings ledger third

---

## File Map

**Create:**
- `engine/recommendations.py` — portfolio ledger shaping + recommendation policy
- `tests/engine/test_recommendations.py` — unit tests for ledger math and policy outcomes

**Modify:**
- `config.py` — add recommendation policy constants
- `db/database.py` — add recommendation context query helpers
- `tests/db/test_portfolio_db.py` — tests for new recommendation context helpers
- `web/routes.py` — refactor `/portfolio` to call the recommendation engine
- `web/templates/portfolio.html` — add action boards and recommendation column
- `tests/web/test_portfolio_route.py` — rendered-page coverage for recommendations

**No DB schema changes:**
- No new tables
- No recommendation persistence
- No migrations

---

### Task 1: Add Recommendation Config and DB Context Helpers

**Files:**
- Modify: `config.py`
- Modify: `db/database.py`
- Modify: `tests/db/test_portfolio_db.py`

- [ ] **Step 1: Write the failing DB helper tests**

Open `tests/db/test_portfolio_db.py`. Extend the import block:

```python
from datetime import datetime, timezone, timedelta
from db.database import (
    init_db, upsert_portfolio_position, delete_gone_positions,
    get_portfolio_positions, get_staked_netuids,
    insert_analyst_mention, insert_milestone,
    get_active_analyst_coverage_netuids, get_recent_milestone_netuids,
)
```

Append these two tests at the end of the file:

```python
@pytest.mark.asyncio
async def test_get_active_analyst_coverage_netuids_respects_decay_window(db):
    now = datetime.now(timezone.utc)
    await insert_analyst_mention(
        db, "0xai_dev", 3, "https://x.com/0xai_dev/status/1",
        "SN3 is moving", now - timedelta(hours=4)
    )
    await insert_analyst_mention(
        db, "0xai_dev", 8, "https://x.com/0xai_dev/status/2",
        "SN8 is stale", now - timedelta(hours=96)
    )

    result = await get_active_analyst_coverage_netuids(db, decay_hours=72)
    assert result == {3}


@pytest.mark.asyncio
async def test_get_recent_milestone_netuids_filters_old_rows(db):
    now = datetime.now(timezone.utc)
    await insert_milestone(
        db, 14, "arxiv", "Recent Paper",
        "https://arxiv.org/abs/2604.00001", now - timedelta(days=2)
    )
    await insert_milestone(
        db, 9, "arxiv", "Old Paper",
        "https://arxiv.org/abs/2603.00002", now - timedelta(days=12)
    )

    result = await get_recent_milestone_netuids(db, hours=168)
    assert result == {14}


@pytest.mark.asyncio
async def test_get_portfolio_positions_includes_category(db):
    await upsert_portfolio_position(db, "ck1", 3, 100.0, 5.0)
    await db.execute(
        "UPDATE subnet_registry SET category=? WHERE netuid=?",
        ("AI Training", 3),
    )
    await db.commit()

    rows = await get_portfolio_positions(db)
    assert rows[0]["category"] == "AI Training"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/db/test_portfolio_db.py -v -k "coverage_netuids or recent_milestone_netuids"
```

Expected: FAIL with `ImportError` for the two new DB helpers.

- [ ] **Step 3: Add recommendation policy constants in `config.py`**

Add this block after the existing portfolio config:

```python
# ── Portfolio recommendations ────────────────────────────────────────────────
PORTFOLIO_RECOMMENDATION_WINDOW_HOURS: int = 168   # 1 week
PORTFOLIO_TRIM_MAX_ALLOC_PCT: float = 0.25         # >25% single-name concentration
PORTFOLIO_CATEGORY_MAX_ALLOC_PCT: float = 0.45     # >45% category concentration blocks new adds
PORTFOLIO_ADD_MIN_SCORE: float = 75.0
PORTFOLIO_NEW_BUY_MIN_SCORE: float = 78.0
PORTFOLIO_REPLACE_SCORE_MARGIN: float = 8.0
PORTFOLIO_HOLD_FLOOR_SCORE: float = 55.0
```

- [ ] **Step 4: Add DB helper functions in `db/database.py`**

Append these functions after `get_staked_netuids()`:

```python
async def get_active_analyst_coverage_netuids(
    db: aiosqlite.Connection,
    decay_hours: int,
) -> set[int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=decay_hours)).isoformat()
    cursor = await db.execute(
        "SELECT DISTINCT netuid FROM analyst_mentions WHERE mentioned_at > ?",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return {row["netuid"] for row in rows}


async def get_recent_milestone_netuids(
    db: aiosqlite.Connection,
    hours: int,
) -> set[int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cursor = await db.execute(
        "SELECT DISTINCT netuid FROM subnet_milestones WHERE published_at > ?",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return {row["netuid"] for row in rows}
```

Update `get_portfolio_positions()` so the query selects `r.category`:

```python
async def get_portfolio_positions(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """All positions LEFT JOINed with subnet_registry, ordered by coldkey then netuid."""
    cursor = await db.execute("""
        SELECT p.*, r.name, r.category, s.tao_usd_price
        FROM portfolio_positions p
        LEFT JOIN subnet_registry r ON p.netuid = r.netuid
        LEFT JOIN (
            SELECT netuid, tao_usd_price, MAX(polled_at) AS max_ts
            FROM snapshots GROUP BY netuid
        ) s ON p.netuid = s.netuid
        ORDER BY p.coldkey, p.netuid
    """)
    return await cursor.fetchall()
```

- [ ] **Step 5: Verify the config module loads**

Run:

```bash
.venv/bin/python -c "import config; print(config.PORTFOLIO_TRIM_MAX_ALLOC_PCT, config.PORTFOLIO_NEW_BUY_MIN_SCORE)"
```

Expected output:

```text
0.25 78.0
```

- [ ] **Step 6: Run the DB helper tests to verify they pass**

Run:

```bash
.venv/bin/pytest tests/db/test_portfolio_db.py -v -k "coverage_netuids or recent_milestone_netuids or includes_category"
```

Expected: `3 passed`

- [ ] **Step 7: Commit**

```bash
git add config.py db/database.py tests/db/test_portfolio_db.py
git commit -m "feat: add portfolio recommendation config and context queries"
```

---

### Task 2: Build the Portfolio Recommendation Engine

**Files:**
- Create: `engine/recommendations.py`
- Create: `tests/engine/test_recommendations.py`

**Background:** This module has two responsibilities only:
1. Turn joined portfolio rows into a stable ledger with correct totals and allocation percentages
2. Compute high-conviction `sell / trim / add / new_buy` actions from holdings, universe snapshots, recent alert types, analyst coverage, and milestone activity

- [ ] **Step 1: Write the failing unit tests**

Create `tests/engine/test_recommendations.py`:

```python
import pytest
import config

from engine.recommendations import (
    build_portfolio_ledger,
    build_portfolio_recommendations,
)


def make_row(**overrides):
    row = {
        "coldkey": "ck1",
        "netuid": 3,
        "alpha_amount": 100.0,
        "tao_value": 6.0,
        "baseline_tao_value": 5.0,
        "name": "Templar",
        "category": "AI Training",
        "tao_usd_price": 300.0,
    }
    row.update(overrides)
    return row


def make_snapshot(**overrides):
    snap = {
        "netuid": 3,
        "name": "Templar",
        "category": "AI Training",
        "composite_score": 80.0,
        "yield_score": 78.0,
        "health_score": 76.0,
        "momentum_score": 74.0,
    }
    snap.update(overrides)
    return snap


def test_build_portfolio_ledger_uses_single_usd_price_for_all_rows():
    rows = [
        make_row(netuid=3, tao_value=6.0, tao_usd_price=None),
        make_row(netuid=56, name="Gradients", tao_value=4.0, baseline_tao_value=3.0, tao_usd_price=300.0),
    ]

    ledger = build_portfolio_ledger(rows, ["ck1"], ["Main"])
    values = {
        p["netuid"]: p["usd_value"]
        for p in ledger["wallets"][0]["positions"]
    }

    assert values[3] == pytest.approx(1800.0)
    assert values[56] == pytest.approx(1200.0)


def test_build_portfolio_ledger_excludes_zero_baseline_from_pnl_totals():
    rows = [
        make_row(netuid=3, tao_value=6.0, baseline_tao_value=5.0, tao_usd_price=300.0),
        make_row(netuid=56, name="Gradients", tao_value=8.0, baseline_tao_value=0.0, tao_usd_price=300.0),
    ]

    ledger = build_portfolio_ledger(rows, ["ck1"], ["Main"])
    assert ledger["grand_pnl_tao"] == pytest.approx(1.0)
    assert ledger["grand_pnl_pct"] == pytest.approx(20.0)


def test_recommendations_sell_on_thesis_break(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    positions = {
        21: {
            "netuid": 21,
            "subnet_name": "Vector",
            "category": "Infrastructure",
            "tao_value": 12.0,
            "allocation_pct": 0.30,
        }
    }
    snapshots = [make_snapshot(netuid=21, name="Vector", category="Infrastructure", composite_score=41.0)]
    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={21: {"liquidity_floor", "tao_outflow"}},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["table_actions"][21]["action"] == "sell"
    assert result["portfolio_actions"][0]["action"] == "sell"


def test_recommendations_trim_on_concentration_without_thesis_break(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    positions = {
        3: {
            "netuid": 3,
            "subnet_name": "Templar",
            "category": "AI Training",
            "tao_value": 34.0,
            "allocation_pct": 0.34,
        }
    }
    snapshots = [make_snapshot(netuid=3, composite_score=81.0)]
    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["table_actions"][3]["action"] == "trim"


def test_recommendations_emit_new_buy_when_candidate_outranks_weakest_held(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_NEW_BUY_MIN_SCORE", 78.0)
    monkeypatch.setattr(config, "PORTFOLIO_REPLACE_SCORE_MARGIN", 8.0)
    positions = {
        7: {
            "netuid": 7,
            "subnet_name": "Cortex",
            "category": "Infrastructure",
            "tao_value": 8.0,
            "allocation_pct": 0.08,
        }
    }
    snapshots = [
        make_snapshot(netuid=7, name="Cortex", category="Infrastructure", composite_score=62.0),
        make_snapshot(netuid=14, name="Macro", category="AI Training", composite_score=82.0),
    ]
    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={},
        coverage_netuids={14},
        milestone_netuids=set(),
    )

    assert result["new_candidates"][0]["netuid"] == 14
    assert result["new_candidates"][0]["action"] == "new_buy"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/engine/test_recommendations.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'engine.recommendations'`

- [ ] **Step 3: Create `engine/recommendations.py`**

Create the file with this implementation:

```python
from collections import defaultdict
from typing import Any

import config

SEVERE_SELL_ALERTS = {"emission_near_zero", "liquidity_floor"}
MODERATE_SELL_ALERTS = {"ownership_transfer", "hyperparameter_change", "tao_outflow", "dead_github"}


def _sum_or_none(values: list[float | None]) -> float | None:
    materialized = [v for v in values if v is not None]
    if not materialized:
        return None
    return sum(materialized)


def build_portfolio_ledger(rows: list[dict[str, Any]], wallet_coldkeys: list[str], wallet_labels: list[str]) -> dict[str, Any]:
    label_map = {}
    for i, coldkey in enumerate(wallet_coldkeys):
        label_map[coldkey] = wallet_labels[i] if i < len(wallet_labels) else f"Wallet {i + 1}"

    tao_usd_price = next((row.get("tao_usd_price") for row in rows if row.get("tao_usd_price") is not None), None)
    wallets_by_coldkey: dict[str, list[dict[str, Any]]] = defaultdict(list)
    positions_by_netuid: dict[int, dict[str, Any]] = {}

    for raw in rows:
        row = dict(raw)
        row["usd_value"] = row["tao_value"] * tao_usd_price if tao_usd_price is not None else None
        row["subnet_label"] = row.get("name") or f"SN{row['netuid']}"
        if row["baseline_tao_value"] > 0:
            row["pnl_tao"] = row["tao_value"] - row["baseline_tao_value"]
            row["pnl_pct"] = row["pnl_tao"] / row["baseline_tao_value"] * 100
        else:
            row["pnl_tao"] = None
            row["pnl_pct"] = None
        wallets_by_coldkey[row["coldkey"]].append(row)

        aggregate = positions_by_netuid.setdefault(row["netuid"], {
            "netuid": row["netuid"],
            "subnet_name": row["subnet_label"],
            "category": row.get("category") or "Other",
            "tao_value": 0.0,
            "baseline_tao_value": 0.0,
        })
        aggregate["tao_value"] += row["tao_value"]
        if row["baseline_tao_value"] > 0:
            aggregate["baseline_tao_value"] += row["baseline_tao_value"]

    wallets = []
    grand_total_tao = 0.0
    grand_total_usd = 0.0
    priced_baseline_total = 0.0
    priced_pnl_total = 0.0

    for coldkey, positions in wallets_by_coldkey.items():
        positions.sort(key=lambda pos: pos["tao_value"], reverse=True)
        total_tao = sum(pos["tao_value"] for pos in positions)
        total_usd = sum(pos["usd_value"] or 0.0 for pos in positions)
        wallet_priced_baseline = sum(pos["baseline_tao_value"] for pos in positions if pos["baseline_tao_value"] > 0)
        wallet_pnl_tao = _sum_or_none([pos["pnl_tao"] for pos in positions])
        wallet_pnl_pct = (wallet_pnl_tao / wallet_priced_baseline * 100) if wallet_pnl_tao is not None and wallet_priced_baseline > 0 else None

        wallets.append({
            "label": label_map.get(coldkey, coldkey[:12] + "..."),
            "coldkey": coldkey,
            "positions": positions,
            "total_tao": total_tao,
            "total_usd": total_usd if tao_usd_price is not None else None,
            "total_pnl_tao": wallet_pnl_tao,
            "total_pnl_pct": wallet_pnl_pct,
        })

        grand_total_tao += total_tao
        grand_total_usd += total_usd
        priced_baseline_total += wallet_priced_baseline
        if wallet_pnl_tao is not None:
            priced_pnl_total += wallet_pnl_tao

    for position in positions_by_netuid.values():
        position["allocation_pct"] = position["tao_value"] / grand_total_tao if grand_total_tao > 0 else 0.0

    grand_pnl_tao = priced_pnl_total if priced_baseline_total > 0 else None
    grand_pnl_pct = (grand_pnl_tao / priced_baseline_total * 100) if grand_pnl_tao is not None and priced_baseline_total > 0 else None

    return {
        "wallets": wallets,
        "positions_by_netuid": positions_by_netuid,
        "grand_total_tao": grand_total_tao,
        "grand_total_usd": grand_total_usd if tao_usd_price is not None else None,
        "grand_pnl_tao": grand_pnl_tao,
        "grand_pnl_pct": grand_pnl_pct,
        "tao_usd_price": tao_usd_price,
    }


def _has_positive_catalyst(snapshot: dict[str, Any], alert_types: set[str], covered: bool, has_milestone: bool) -> bool:
    return (
        "convergence" in alert_types
        or "milestone" in alert_types
        or covered
        or has_milestone
        or (snapshot.get("momentum_score") or 0.0) >= 70.0
    )


def _has_thesis_break(snapshot: dict[str, Any], alert_types: set[str]) -> bool:
    if SEVERE_SELL_ALERTS & alert_types:
        return True
    moderate_count = len(MODERATE_SELL_ALERTS & alert_types)
    return moderate_count >= 2 and (snapshot.get("composite_score") or 0.0) < config.PORTFOLIO_HOLD_FLOOR_SCORE


def _card(snapshot: dict[str, Any], action: str, confidence: str, reasons: list[str], allocation_pct: float | None) -> dict[str, Any]:
    return {
        "netuid": snapshot["netuid"],
        "subnet_name": snapshot.get("name") or f"SN{snapshot['netuid']}",
        "action": action,
        "confidence": confidence,
        "reasons": reasons,
        "score": snapshot.get("composite_score"),
        "category": snapshot.get("category") or "Other",
        "allocation_pct": allocation_pct,
    }


def build_portfolio_recommendations(
    positions_by_netuid: dict[int, dict[str, Any]],
    snapshots: list[dict[str, Any]],
    alert_types_by_netuid: dict[int, set[str]],
    coverage_netuids: set[int],
    milestone_netuids: set[int],
) -> dict[str, Any]:
    snapshots_by_netuid = {snap["netuid"]: dict(snap) for snap in snapshots}
    category_allocations: dict[str, float] = defaultdict(float)
    for position in positions_by_netuid.values():
        category_allocations[position["category"]] += position["allocation_pct"]

    weakest_held_score = min(
        (snapshots_by_netuid.get(netuid, {}).get("composite_score") or 0.0)
        for netuid in positions_by_netuid
    ) if positions_by_netuid else 0.0

    portfolio_actions: list[dict[str, Any]] = []
    table_actions: dict[int, dict[str, Any]] = {}

    for netuid, position in positions_by_netuid.items():
        snapshot = snapshots_by_netuid.get(netuid, {
            "netuid": netuid,
            "name": position["subnet_name"],
            "category": position["category"],
            "composite_score": None,
            "momentum_score": None,
        })
        alert_types = alert_types_by_netuid.get(netuid, set())
        catalyst = _has_positive_catalyst(snapshot, alert_types, netuid in coverage_netuids, netuid in milestone_netuids)
        score = snapshot.get("composite_score") or 0.0

        if _has_thesis_break(snapshot, alert_types):
            card = _card(snapshot, "sell", "high", ["thesis break: severe or repeated risk alerts"], position["allocation_pct"])
            table_actions[netuid] = card
            portfolio_actions.append(card)
            continue

        if position["allocation_pct"] >= config.PORTFOLIO_TRIM_MAX_ALLOC_PCT:
            reasons = [f"position is {position['allocation_pct'] * 100:.1f}% of book"]
            if not catalyst:
                reasons.append("no fresh positive catalyst")
            card = _card(snapshot, "trim", "high", reasons, position["allocation_pct"])
            table_actions[netuid] = card
            portfolio_actions.append(card)
            continue

        if (
            score >= config.PORTFOLIO_ADD_MIN_SCORE
            and catalyst
            and category_allocations[position["category"]] < config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT
        ):
            card = _card(snapshot, "add", "medium", ["strong held winner with room to add"], position["allocation_pct"])
            table_actions[netuid] = card
            portfolio_actions.append(card)
            continue

        table_actions[netuid] = _card(snapshot, "hold", "low", [], position["allocation_pct"])

    new_candidates: list[dict[str, Any]] = []
    for snapshot in sorted(snapshots, key=lambda snap: snap.get("composite_score") or 0.0, reverse=True):
        netuid = snapshot["netuid"]
        if netuid in positions_by_netuid:
            continue
        score = snapshot.get("composite_score") or 0.0
        if score < config.PORTFOLIO_NEW_BUY_MIN_SCORE:
            continue
        if score < weakest_held_score + config.PORTFOLIO_REPLACE_SCORE_MARGIN:
            continue

        alert_types = alert_types_by_netuid.get(netuid, set())
        if _has_thesis_break(snapshot, alert_types):
            continue

        category = snapshot.get("category") or "Other"
        if category_allocations[category] >= config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT:
            continue

        catalyst = _has_positive_catalyst(snapshot, alert_types, netuid in coverage_netuids, netuid in milestone_netuids)
        if not catalyst:
            continue

        new_candidates.append(
            _card(snapshot, "new_buy", "medium", ["outranks weakest held name with a fresh catalyst"], None)
        )

    return {
        "portfolio_actions": portfolio_actions,
        "new_candidates": new_candidates[:3],
        "table_actions": table_actions,
    }
```

- [ ] **Step 4: Run the unit tests to verify they pass**

Run:

```bash
.venv/bin/pytest tests/engine/test_recommendations.py -v
```

Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add engine/recommendations.py tests/engine/test_recommendations.py
git commit -m "feat: add portfolio recommendation engine"
```

---

### Task 3: Integrate Recommendations Into the Portfolio Route

**Files:**
- Modify: `web/routes.py`

- [ ] **Step 1: Wire the new imports into `web/routes.py`**

Add these imports:

```python
from db.database import (
    get_portfolio_positions, get_staked_netuids,
    get_latest_snapshots_with_registry, get_recent_alert_types_per_netuid,
    get_active_analyst_coverage_netuids, get_recent_milestone_netuids,
)
from engine.recommendations import (
    build_portfolio_ledger,
    build_portfolio_recommendations,
)
```

- [ ] **Step 2: Replace the current `/portfolio` route body**

Replace the current portfolio handler with:

```python
    @app.get("/portfolio", response_class=HTMLResponse)
    async def portfolio(request: Request):
        rows = [dict(row) for row in await get_portfolio_positions(db)]
        ledger = build_portfolio_ledger(rows, config.WALLET_COLDKEYS, config.WALLET_LABELS)

        latest_snaps = [dict(row) for row in await get_latest_snapshots_with_registry(db)]
        alert_types = await get_recent_alert_types_per_netuid(
            db,
            [
                "convergence", "milestone", "analyst_mention",
                "liquidity_floor", "emission_near_zero",
                "ownership_transfer", "hyperparameter_change",
                "tao_outflow", "dead_github",
            ],
            config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS,
        )
        coverage_netuids = await get_active_analyst_coverage_netuids(
            db, config.ANALYST_COVERAGE_DECAY_HOURS
        )
        milestone_netuids = await get_recent_milestone_netuids(
            db, config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS
        )

        recs = build_portfolio_recommendations(
            positions_by_netuid=ledger["positions_by_netuid"],
            snapshots=latest_snaps,
            alert_types_by_netuid=alert_types,
            coverage_netuids=coverage_netuids,
            milestone_netuids=milestone_netuids,
        )

        for wallet in ledger["wallets"]:
            for pos in wallet["positions"]:
                aggregate = ledger["positions_by_netuid"][pos["netuid"]]
                pos["allocation_pct"] = aggregate["allocation_pct"] * 100
                snap = next((s for s in latest_snaps if s["netuid"] == pos["netuid"]), None)
                pos["score"] = snap.get("composite_score") if snap else None
                pos["recommendation"] = recs["table_actions"].get(pos["netuid"], {
                    "action": "hold",
                    "confidence": "low",
                    "reasons": [],
                })

        return templates.TemplateResponse(request, "portfolio.html", {
            **ledger,
            **recs,
        })
```

- [ ] **Step 3: Verify the route imports cleanly**

Run:

```bash
TELEGRAM_BOT_TOKEN=test TELEGRAM_CHAT_ID=test .venv/bin/python -c "from web.routes import create_app; print('ok')"
```

Expected output:

```text
ok
```

- [ ] **Step 4: Commit**

```bash
git add web/routes.py
git commit -m "feat: wire portfolio recommendation engine into route"
```

---

### Task 4: Update the Portfolio Screen and Rendered Route Tests

**Files:**
- Modify: `web/templates/portfolio.html`
- Modify: `tests/web/test_portfolio_route.py`

- [ ] **Step 1: Write the failing rendered route tests**

Replace the current `tests/web/test_portfolio_route.py` content with:

```python
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from web.routes import create_app


def make_row(**overrides):
    row = {
        "coldkey": "ck1",
        "netuid": 3,
        "alpha_amount": 100.0,
        "tao_value": 6.0,
        "baseline_tao_value": 5.0,
        "name": "Templar",
        "category": "AI Training",
        "tao_usd_price": 300.0,
    }
    row.update(overrides)
    return row


@pytest.fixture
def client_with_portfolio():
    db = AsyncMock()
    with patch("web.routes.get_portfolio_positions", new=AsyncMock(return_value=[
        make_row(netuid=3, name="Templar", tao_value=6.0, baseline_tao_value=5.0),
        make_row(netuid=56, name="Gradients", tao_value=4.0, baseline_tao_value=3.0),
    ])), \
         patch("web.routes.get_latest_snapshots_with_registry", new=AsyncMock(return_value=[
             {"netuid": 3, "name": "Templar", "category": "AI Training", "composite_score": 81.0, "momentum_score": 68.0},
             {"netuid": 56, "name": "Gradients", "category": "Infrastructure", "composite_score": 86.0, "momentum_score": 76.0},
             {"netuid": 14, "name": "Macro", "category": "Data / Retrieval", "composite_score": 82.0, "momentum_score": 72.0},
         ])), \
         patch("web.routes.get_recent_alert_types_per_netuid", new=AsyncMock(return_value={})), \
         patch("web.routes.get_active_analyst_coverage_netuids", new=AsyncMock(return_value={14})), \
         patch("web.routes.get_recent_milestone_netuids", new=AsyncMock(return_value={56})), \
         patch("web.routes.build_portfolio_recommendations", return_value={
             "portfolio_actions": [
                 {
                     "netuid": 3,
                     "subnet_name": "Templar",
                     "action": "trim",
                     "confidence": "high",
                     "reasons": ["position is 60.0% of book"],
                     "score": 81.0,
                     "allocation_pct": 0.60,
                 }
             ],
             "new_candidates": [
                 {
                     "netuid": 14,
                     "subnet_name": "Macro",
                     "action": "new_buy",
                     "confidence": "medium",
                     "reasons": ["outranks weakest held name with a fresh catalyst"],
                     "score": 82.0,
                     "allocation_pct": None,
                 }
             ],
             "table_actions": {
                 3: {"action": "trim", "confidence": "high", "reasons": ["position is 60.0% of book"]},
                 56: {"action": "hold", "confidence": "low", "reasons": []},
             },
         }), \
         patch("web.routes.get_staked_netuids", new=AsyncMock(return_value={3, 56})), \
         patch("web.routes.get_last_50_alerts", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_emission_rank_24h_ago", new=AsyncMock(return_value={})), \
         patch("web.routes.config") as mock_config:
        mock_config.WALLET_COLDKEYS = ["ck1"]
        mock_config.WALLET_LABELS = ["Main"]
        mock_config.MOMENTUM_HISTORY_LIMIT = 10
        mock_config.ANALYST_COVERAGE_DECAY_HOURS = 72
        mock_config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS = 168
        mock_config.PORTFOLIO_TRIM_MAX_ALLOC_PCT = 0.25
        mock_config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT = 0.45
        mock_config.PORTFOLIO_ADD_MIN_SCORE = 75.0
        mock_config.PORTFOLIO_NEW_BUY_MIN_SCORE = 78.0
        mock_config.PORTFOLIO_REPLACE_SCORE_MARGIN = 8.0
        mock_config.PORTFOLIO_HOLD_FLOOR_SCORE = 55.0
        app = create_app(db)
        yield TestClient(app)


def test_portfolio_empty_state():
    db = AsyncMock()
    with patch("web.routes.get_portfolio_positions", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_latest_snapshots_with_registry", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_recent_alert_types_per_netuid", new=AsyncMock(return_value={})), \
         patch("web.routes.get_active_analyst_coverage_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_recent_milestone_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_staked_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_last_50_alerts", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_emission_rank_24h_ago", new=AsyncMock(return_value={})), \
         patch("web.routes.config") as mock_config:
        mock_config.WALLET_COLDKEYS = []
        mock_config.WALLET_LABELS = []
        mock_config.MOMENTUM_HISTORY_LIMIT = 10
        mock_config.ANALYST_COVERAGE_DECAY_HOURS = 72
        mock_config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS = 168
        app = create_app(db)
        client = TestClient(app)
        resp = client.get("/portfolio")
    assert resp.status_code == 200
    assert "No portfolio positions found" in resp.text


def test_portfolio_renders_action_sections(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert resp.status_code == 200
    assert "Portfolio Actions" in resp.text
    assert "New Candidates" in resp.text


def test_portfolio_renders_table_recommendations(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Recommendation" in resp.text
    assert "trim" in resp.text.lower()
    assert "hold" in resp.text.lower()


def test_portfolio_renders_new_buy_candidate(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Macro" in resp.text
    assert "New Buy" in resp.text or "NEW BUY" in resp.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/web/test_portfolio_route.py -v
```

Expected: FAIL because the template has no action sections or recommendation column yet.

- [ ] **Step 3: Replace `web/templates/portfolio.html` with the action-first hierarchy**

Replace the template with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="60">
  <title>Portfolio — TAO Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: monospace; background: #0f0f0f; color: #e0e0e0; }
    header { background: #1a1a2e; padding: 12px 20px; display: flex; gap: 24px; align-items: center; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.2rem; color: #00d4aa; }
    header a { color: #666; font-size: 0.85rem; text-decoration: none; margin-left: auto; }
    header a:hover { color: #00d4aa; }
    .content { max-width: 1200px; margin: 0 auto; padding: 24px 16px; }
    .summary { display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 24px; }
    .summary-card { background:#151515; border:1px solid #2a2a2a; border-radius:6px; padding:14px; }
    .summary-label { color:#666; font-size:0.72rem; text-transform:uppercase; letter-spacing:1px; }
    .summary-value { color:#f0f0f0; font-size:1.1rem; margin-top:6px; }
    .section-title { color:#888; text-transform:uppercase; letter-spacing:1px; font-size:0.85rem; margin:26px 0 12px; }
    .action-list { display:flex; flex-direction:column; gap:10px; }
    .action-card { border:1px solid #2a2a2a; border-left-width:4px; border-radius:6px; padding:12px; background:#121212; }
    .action-card.sell { border-left-color:#ff5252; }
    .action-card.trim { border-left-color:#ffd600; }
    .action-card.add, .action-card.new_buy { border-left-color:#00c853; }
    .action-head { display:flex; justify-content:space-between; gap:12px; }
    .action-name { color:#f0f0f0; font-weight:bold; }
    .action-meta { color:#666; font-size:0.75rem; }
    .action-reasons { color:#aaa; font-size:0.82rem; margin-top:6px; }
    .wallet-block { margin-top: 28px; }
    .wallet-header { font-size: 0.9rem; color: #888; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 10px; border-bottom: 1px solid #333; padding-bottom: 6px; }
    .wallet-header span { color: #444; font-size: 0.75rem; margin-left: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; }
    th { text-align: left; padding: 6px 10px; color: #666; border-bottom: 1px solid #333; }
    td { padding: 7px 10px; border-bottom: 1px solid #1a1a1a; }
    .sn-link { color: #00d4aa; text-decoration: none; }
    .rec-pill { display:inline-block; padding:2px 6px; border-radius:4px; font-size:0.72rem; text-transform:uppercase; letter-spacing:0.6px; }
    .rec-pill.sell { background:#331717; color:#ff8a8a; }
    .rec-pill.trim { background:#332d15; color:#ffe16a; }
    .rec-pill.add, .rec-pill.new_buy { background:#173322; color:#6ef5a1; }
    .rec-pill.hold { background:#1f1f1f; color:#777; }
    .pnl-pos { color: #00c853; }
    .pnl-neg { color: #ff5252; }
    .pnl-null { color: #555; }
    .empty { color: #555; font-style: italic; padding: 40px 0; text-align: center; }
    .note { color: #555; font-size: 0.75rem; margin-top: 16px; }
  </style>
</head>
<body>
{% macro fmt_tao(v) -%}{%- if v is none %}—{%- else %}{{ "%.2f"|format(v) }} τ{%- endif %}{%- endmacro %}
{% macro fmt_usd(v) -%}{%- if v is none %}—{%- elif v >= 1000000 %}${{ "%.1f"|format(v / 1000000) }}M{%- elif v >= 1000 %}${{ "%.0f"|format(v / 1000) }}K{%- else %}${{ "%.0f"|format(v) }}{%- endif %}{%- endmacro %}
{% macro fmt_pnl(v, pct) -%}
{%- if v is none %}<span class="pnl-null">—</span>
{%- elif v >= 0 %}<span class="pnl-pos">+{{ "%.2f"|format(v) }} τ (+{{ "%.1f"|format(pct) }}%)</span>
{%- else %}<span class="pnl-neg">{{ "%.2f"|format(v) }} τ ({{ "%.1f"|format(pct) }}%)</span>
{%- endif %}
{%- endmacro %}
<header>
  <h1>Portfolio</h1>
  {% if tao_usd_price %}<span style="color:#999; font-size:0.85rem;">TAO = ${{ "%.2f"|format(tao_usd_price) }}</span>{% endif %}
  <a href="/">← Dashboard</a>
</header>
<div class="content">
{% if not wallets %}
  <p class="empty">No portfolio positions found. Add WALLET_COLDKEYS to .env and wait for the next poll.</p>
{% else %}
  <div class="summary">
    <div class="summary-card"><div class="summary-label">Grand Total</div><div class="summary-value">{{ fmt_usd(grand_total_usd) }}</div></div>
    <div class="summary-card"><div class="summary-label">Total TAO</div><div class="summary-value">{{ fmt_tao(grand_total_tao) }}</div></div>
    <div class="summary-card"><div class="summary-label">Portfolio P&amp;L</div><div class="summary-value">{{ fmt_pnl(grand_pnl_tao, grand_pnl_pct) }}</div></div>
    <div class="summary-card"><div class="summary-label">Actions</div><div class="summary-value">{{ portfolio_actions|length }} actions · {{ new_candidates|length }} candidates</div></div>
  </div>

  <div class="section-title">Portfolio Actions</div>
  {% if portfolio_actions %}
  <div class="action-list">
    {% for rec in portfolio_actions %}
    <div class="action-card {{ rec.action }}">
      <div class="action-head">
        <div class="action-name">{{ rec.action|replace("_", " ")|upper }} · {{ rec.subnet_name }} (SN{{ rec.netuid }})</div>
        <div class="action-meta">{{ rec.confidence }} confidence{% if rec.allocation_pct is not none %} · {{ "%.1f"|format(rec.allocation_pct * 100) }}% alloc{% endif %}</div>
      </div>
      <div class="action-reasons">{{ rec.reasons|join(" · ") }}</div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <p class="empty">No high-conviction portfolio actions right now.</p>
  {% endif %}

  <div class="section-title">New Candidates</div>
  {% if new_candidates %}
  <div class="action-list">
    {% for rec in new_candidates %}
    <div class="action-card {{ rec.action }}">
      <div class="action-head">
        <div class="action-name">NEW BUY · {{ rec.subnet_name }} (SN{{ rec.netuid }})</div>
        <div class="action-meta">{{ rec.confidence }} confidence · score {{ "%.1f"|format(rec.score) if rec.score is not none else "—" }}</div>
      </div>
      <div class="action-reasons">{{ rec.reasons|join(" · ") }}</div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <p class="empty">No new candidates cleared the conviction threshold.</p>
  {% endif %}

  {% for wallet in wallets %}
  <div class="wallet-block">
    <div class="wallet-header">{{ wallet.label }}<span>{{ wallet.coldkey[:12] }}...</span></div>
    <table>
      <thead>
        <tr>
          <th>Subnet</th>
          <th>Alloc</th>
          <th>Score</th>
          <th>TAO Value</th>
          <th>USD Value</th>
          <th>P&amp;L</th>
          <th>Recommendation</th>
        </tr>
      </thead>
      <tbody>
        {% for pos in wallet.positions %}
        <tr>
          <td><a class="sn-link" href="/subnet/{{ pos.netuid }}">{{ pos.subnet_label }}</a><span style="color:#444; font-size:0.85em;"> SN{{ pos.netuid }}</span></td>
          <td>{{ "%.1f"|format(pos.allocation_pct) }}%</td>
          <td>{{ "%.1f"|format(pos.score) if pos.score is not none else "—" }}</td>
          <td>{{ fmt_tao(pos.tao_value) }}</td>
          <td>{{ fmt_usd(pos.usd_value) }}</td>
          <td>{{ fmt_pnl(pos.pnl_tao, pos.pnl_pct) }}</td>
          <td><span class="rec-pill {{ pos.recommendation.action }}">{{ pos.recommendation.action|replace("_", " ") }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endfor %}

  <p class="note">Recommendations are computed, not stored. `Sell` is thesis-break only; `Trim` is concentration-first; `Add` and `New Buy` require a clear edge.</p>
{% endif %}
</div>
</body>
</html>
```

- [ ] **Step 4: Run the rendered route tests to verify they pass**

Run:

```bash
.venv/bin/pytest tests/web/test_portfolio_route.py -v
```

Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add web/templates/portfolio.html tests/web/test_portfolio_route.py
git commit -m "feat: add action-oriented portfolio recommendation view"
```

---

### Task 5: Full Verification

**Files:**
- Verify only

- [ ] **Step 1: Run the new focused test slices**

```bash
.venv/bin/pytest tests/db/test_portfolio_db.py -v -k "coverage_netuids or recent_milestone_netuids"
.venv/bin/pytest tests/engine/test_recommendations.py -v
.venv/bin/pytest tests/web/test_portfolio_route.py -v
```

Expected: all PASS

- [ ] **Step 2: Run the full test suite**

```bash
.venv/bin/pytest -q
```

Expected: full suite PASS with no new failures

- [ ] **Step 3: Import-check the app**

```bash
TELEGRAM_BOT_TOKEN=test TELEGRAM_CHAT_ID=test .venv/bin/python -c "import main"
```

Expected: exit code `0`

---

## Self-Review

**Spec coverage:**
- `portfolio-driven, direction-only recommendations`: Task 2
- `Portfolio Actions + New Candidates`: Tasks 3 and 4
- `high-conviction only`: Task 2 thresholds + Task 4 rendering
- `concentration-aware trim`: Task 2
- `sell only on thesis break`: Task 2
- `no recommendation persistence`: Tasks 1-4; no schema changes
- `portfolio math trust fixes`: Task 2 ledger builder
- `review-friendly screen hierarchy`: Task 4

**Placeholder scan:** No `TODO` / `TBD` markers remain.

**Type consistency:**
- `build_portfolio_ledger(...)` returns `wallets`, `positions_by_netuid`, and grand totals used by the route unchanged
- `build_portfolio_recommendations(...)` returns `portfolio_actions`, `new_candidates`, `table_actions` consumed by the route and template unchanged
