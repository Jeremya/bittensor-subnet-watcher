# Spec 421 Scoring Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the subnet swing score so it reflects Spec 421 price-based emissions instead of the deprecated flow-based emission thesis.

**Architecture:** Add a pure `engine/spec421.py` scoring module that computes an honest Spec 421 opportunity signal from currently collected data: spot alpha price history as an EMA-price proxy, emission value versus market cap, and explicit missing-factor notes for exact root-proportion and miner-burn inputs that are not collected. Persist the new component scores in `snapshots`, wire `spec421_score` into the existing `SwingSignal`, and update dashboard/detail/portfolio copy so users see price-based emission context rather than old flow-share language.

**Tech Stack:** Python 3.13, dataclasses, SQLite/aiosqlite, pytest, existing FastAPI/Jinja dashboard and Telegram alert infrastructure.

---

## Source-Of-Truth Protocol Assumptions

Official Bittensor docs state that Spec 421 is deployed on mainnet and that subnet emissions reverted in June 2026 from the November 2025 flow-based model to price-based emissions. A subnet's block-emission share is proportional to:

```text
root_proportion * SubnetMovingPrice * (1 - MinerBurned)
```

normalized across emission-enabled subnets.

For this implementation:

- `alpha_price_tao` history is used as an EMA-price proxy because the app already collects spot subnet price and does not yet store the exact chain `SubnetMovingPrice`.
- Flow remains useful as short-term demand and risk context, but it is no longer treated as the causal driver of future emission share.
- Root proportion, miner-burn, and exact alpha injection cap are represented as missing exact protocol factors unless current chain collector fields are proven and stored. The score must not invent values for them.
- The first shipped version is a scoring and display refactor, not a transaction/event ingestion project.

## File Structure

- Create `engine/spec421.py`
  - Owns `Spec421Component`, `Spec421Signal`, EMA helpers, price-EMA proxy scoring, emission-value scoring, and aggregate Spec 421 score computation.
  - Has no DB, Telegram, dashboard, or collector dependency.
- Create `tests/engine/test_spec421.py`
  - Unit tests for all pure Spec 421 scoring behavior.
- Modify `models.py`
  - Adds persisted score fields: `price_ema_score`, `emission_value_score`, `protocol_context_score`, `spec421_score`.
- Modify `db/database.py`
  - Adds schema columns, migration columns, insert bindings, and query passthrough for the new fields.
- Modify `engine/signals.py`
  - Adds `spec421` to `SwingSignal`.
  - Reweights `compute_swing_signal()` around Spec 421 price-based emissions while preserving flow as demand/risk context.
- Modify `engine/scorer.py`
  - Calls `compute_spec421_signals()` and persists the component fields.
  - Updates comments that currently describe net flow as the emission-share driver.
- Modify `engine/policy.py`
  - Reconstructs `spec421` from persisted snapshot fields for routes and portfolio recommendations.
  - Updates user-facing reasons from old flow-share phrasing to price-based emission phrasing.
- Modify `web/routes.py`
  - Passes Spec 421 context into subnet detail rendering.
- Modify `web/templates/index.html`, `web/templates/subnet.html`, `web/templates/portfolio.html`
  - Shows `spec421_score` and component fields where they inform decisions.
- Modify `scripts/backtest_signals.py`
  - Adds Spec 421 score coverage to the backtest coverage header.
- Modify tests:
  - `tests/engine/test_signals.py`
  - `tests/engine/test_scorer.py`
  - `tests/engine/test_policy.py`
  - `tests/test_database.py`
  - `tests/test_backtest_script.py`
  - `tests/web/test_routes.py`
  - `tests/web/test_portfolio_route.py`

---

### Task 1: Pure Spec 421 Scoring Module

**Files:**
- Create: `engine/spec421.py`
- Create: `tests/engine/test_spec421.py`

- [ ] **Step 1: Write failing tests for price EMA proxy scoring**

Create `tests/engine/test_spec421.py` with these tests:

```python
from datetime import datetime, timedelta, timezone

import pytest

from engine.spec421 import (
    compute_emission_value_scores,
    compute_price_ema_score,
    compute_protocol_context_score,
    compute_spec421_signals,
)
from models import SubnetSnapshot


def make_snap(netuid: int = 1, **overrides) -> SubnetSnapshot:
    data = {
        "netuid": netuid,
        "polled_at": datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
        "alpha_price_tao": 1.0,
        "alpha_mcap_tao": 1_000.0,
        "alpha_mcap_usd": 300_000.0,
        "daily_emission_tao": 10.0,
        "tao_usd_price": 300.0,
        "emission_rank": 10,
        "emergence_stage": "maturing",
        "emergence_score": 45.0,
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def price_history(values: list[float]) -> list[SubnetSnapshot]:
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    rows: list[SubnetSnapshot] = []
    for i, price in enumerate(values):
        rows.append(
            make_snap(
                polled_at=now - timedelta(hours=(len(values) - i)),
                alpha_price_tao=price,
            )
        )
    return rows


def test_price_ema_score_rewards_price_above_slow_ema():
    current = make_snap(alpha_price_tao=1.35)
    rising = compute_price_ema_score(current, price_history([1.0, 1.05, 1.1, 1.2]))
    flat = compute_price_ema_score(current, price_history([1.35, 1.35, 1.35, 1.35]))

    assert rising.score is not None
    assert flat.score is not None
    assert rising.score > flat.score
    assert rising.is_positive
    assert "price above EMA proxy" in rising.reasons


def test_price_ema_score_penalizes_price_below_slow_ema():
    current = make_snap(alpha_price_tao=0.70)
    signal = compute_price_ema_score(current, price_history([1.0, 0.95, 0.90, 0.82]))

    assert signal.score is not None
    assert signal.score < 50.0
    assert signal.is_negative
    assert "price below EMA proxy" in signal.risks


def test_price_ema_score_missing_history_is_unavailable():
    current = make_snap(alpha_price_tao=1.1)
    signal = compute_price_ema_score(current, [])

    assert signal.score is None
    assert "insufficient price history" in signal.notes
```

- [ ] **Step 2: Run the new test file and verify it fails on missing module**

Run:

```bash
.venv/bin/pytest tests/engine/test_spec421.py -q
```

Expected: fail during import with `ModuleNotFoundError: No module named 'engine.spec421'`.

- [ ] **Step 3: Add failing tests for emission-value scoring**

Append to `tests/engine/test_spec421.py`:

```python
def test_emission_value_scores_reward_emission_discount_under_price_based_model():
    cheap = make_snap(
        netuid=1,
        daily_emission_tao=25.0,
        alpha_mcap_usd=300_000.0,
        emission_rank=5,
    )
    rich = make_snap(
        netuid=2,
        daily_emission_tao=5.0,
        alpha_mcap_usd=3_000_000.0,
        emission_rank=40,
    )

    scores = compute_emission_value_scores([cheap, rich])

    assert scores[1].score is not None
    assert scores[2].score is not None
    assert scores[1].score > scores[2].score
    assert "price-based emission value versus market cap" in scores[1].reasons


def test_emission_value_scores_suppress_micro_caps():
    micro = make_snap(netuid=1, alpha_mcap_usd=10_000.0, daily_emission_tao=50.0)

    scores = compute_emission_value_scores([micro])

    assert scores[1].score is None
    assert "below emission-value market-cap floor" in scores[1].notes
```

- [ ] **Step 4: Add failing tests for protocol-context honesty**

Append to `tests/engine/test_spec421.py`:

```python
def test_protocol_context_does_not_fake_uncollected_exact_factors():
    current = make_snap(emergence_stage="nascent", emergence_score=76.0)

    signal = compute_protocol_context_score(current)

    assert signal.score is not None
    assert signal.is_positive
    assert "newer subnet may benefit from root-proportion weighting" in signal.reasons
    assert "exact root_proportion not collected" in signal.notes
    assert "exact miner_burned not collected" in signal.notes


def test_spec421_signal_combines_available_components_and_notes_missing_factors():
    current = make_snap(alpha_price_tao=1.25, emergence_stage="nascent", emergence_score=75.0)
    peer = make_snap(netuid=2, alpha_price_tao=0.9, alpha_mcap_usd=2_000_000.0, daily_emission_tao=3.0)

    signals = compute_spec421_signals(
        [current, peer],
        {1: price_history([1.0, 1.03, 1.08, 1.15]), 2: price_history([1.0, 0.98, 0.95, 0.92])},
    )

    signal = signals[1]
    assert signal.spec421_score > signals[2].spec421_score
    assert signal.price_ema.score is not None
    assert signal.emission_value.score is not None
    assert signal.protocol_context.score is not None
    assert "Spec 421 score uses EMA-price proxy, not exact SubnetMovingPrice" in signal.notes
```

- [ ] **Step 5: Implement `engine/spec421.py`**

Create `engine/spec421.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import config
from models import SubnetSnapshot


@dataclass(frozen=True)
class Spec421Component:
    score: Optional[float]
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    is_positive: bool = False
    is_negative: bool = False
    is_strong: bool = False


@dataclass(frozen=True)
class Spec421Signal:
    netuid: int
    price_ema: Spec421Component
    emission_value: Spec421Component
    protocol_context: Spec421Component
    spec421_score: float
    reasons: list[str]
    risks: list[str]
    notes: list[str]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _positive(value: Optional[float]) -> Optional[float]:
    if value is None or value <= 0:
        return None
    return float(value)


def _ordered_price_history(
    current: SubnetSnapshot,
    history: list[SubnetSnapshot],
) -> list[float]:
    rows = [
        row
        for row in history
        if row.polled_at is not None
        and row.polled_at < current.polled_at
        and _positive(row.alpha_price_tao) is not None
    ]
    rows.sort(key=lambda row: row.polled_at)
    prices = [float(row.alpha_price_tao) for row in rows]
    current_price = _positive(current.alpha_price_tao)
    if current_price is not None:
        prices.append(current_price)
    return prices


def _ema(values: list[float], alpha: float) -> float:
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1.0 - alpha) * result
    return result


def compute_price_ema_score(
    current: SubnetSnapshot,
    history: list[SubnetSnapshot],
) -> Spec421Component:
    prices = _ordered_price_history(current, history)
    if len(prices) < 4:
        return Spec421Component(score=None, notes=["insufficient price history"])

    current_price = prices[-1]
    slow = _ema(prices, alpha=0.12)
    fast = _ema(prices, alpha=0.35)
    if slow <= 0:
        return Spec421Component(score=None, notes=["invalid EMA price proxy"])

    spot_vs_slow = current_price / slow - 1.0
    fast_vs_slow = fast / slow - 1.0
    score = 50.0
    score += max(-35.0, min(35.0, spot_vs_slow * 500.0))
    score += max(-15.0, min(15.0, fast_vs_slow * 300.0))
    score = _clamp(score)

    reasons: list[str] = []
    risks: list[str] = []
    if spot_vs_slow >= 0.03:
        reasons.append("price above EMA proxy")
    if fast_vs_slow > 0:
        reasons.append("fast EMA proxy above slow EMA proxy")
    if spot_vs_slow <= -0.03:
        risks.append("price below EMA proxy")

    return Spec421Component(
        score=round(score, 2),
        reasons=reasons,
        risks=risks,
        notes=["Spec 421 score uses EMA-price proxy, not exact SubnetMovingPrice"],
        is_positive=score >= 65.0,
        is_negative=score <= 40.0,
        is_strong=score >= 75.0,
    )


def _raw_emission_yield(snap: SubnetSnapshot) -> Optional[float]:
    if (
        snap.daily_emission_tao is None
        or snap.tao_usd_price is None
        or snap.alpha_mcap_usd is None
        or snap.alpha_mcap_usd <= 0
    ):
        return None
    if snap.alpha_mcap_usd < config.YIELD_MIN_MCAP_USD:
        return None
    return (snap.daily_emission_tao * snap.tao_usd_price * 365.0) / snap.alpha_mcap_usd


def compute_emission_value_scores(
    snapshots: list[SubnetSnapshot],
) -> dict[int, Spec421Component]:
    raw_yields = {
        snap.netuid: raw
        for snap in snapshots
        if (raw := _raw_emission_yield(snap)) is not None
    }
    valid_mcap = [
        (snap.netuid, snap.alpha_mcap_tao)
        for snap in snapshots
        if snap.alpha_mcap_tao is not None
    ]
    valid_mcap.sort(key=lambda item: item[1], reverse=True)
    mcap_rank = {netuid: rank for rank, (netuid, _) in enumerate(valid_mcap, start=1)}
    min_yield = min(raw_yields.values()) if raw_yields else None
    max_yield = max(raw_yields.values()) if raw_yields else None

    result: dict[int, Spec421Component] = {}
    for snap in snapshots:
        parts: list[float] = []
        reasons: list[str] = []
        risks: list[str] = []
        notes: list[str] = []

        raw = raw_yields.get(snap.netuid)
        if raw is not None and min_yield is not None and max_yield is not None:
            if max_yield == min_yield:
                yield_score = 50.0
            else:
                yield_score = (raw - min_yield) / (max_yield - min_yield) * 100.0
            parts.append(yield_score)
            if yield_score >= 75.0:
                reasons.append("price-based emission value versus market cap")
        elif snap.alpha_mcap_usd is not None and snap.alpha_mcap_usd < config.YIELD_MIN_MCAP_USD:
            notes.append("below emission-value market-cap floor")

        mc_rank = mcap_rank.get(snap.netuid)
        if snap.emission_rank is not None and mc_rank is not None and snap.emission_rank > 0:
            ratio = mc_rank / snap.emission_rank
            rank_score = _clamp(50.0 + (ratio - 1.0) * 35.0)
            parts.append(rank_score)
            if ratio >= 1.3:
                reasons.append("price-based emission rank discounted by market cap")
            elif ratio <= 0.7:
                risks.append("market cap rich versus price-based emissions")

        if not parts:
            result[snap.netuid] = Spec421Component(
                score=None,
                notes=notes or ["missing emission-value data"],
            )
            continue

        score = sum(parts) / len(parts)
        result[snap.netuid] = Spec421Component(
            score=round(_clamp(score), 2),
            reasons=reasons,
            risks=risks,
            notes=notes,
            is_positive=score >= 65.0,
            is_negative=score <= 35.0,
            is_strong=score >= 75.0,
        )

    return result


def compute_protocol_context_score(snap: SubnetSnapshot) -> Spec421Component:
    score = 50.0
    reasons: list[str] = []
    risks: list[str] = []
    notes = [
        "exact root_proportion not collected",
        "exact miner_burned not collected",
        "exact alpha injection cap not collected",
    ]

    if snap.emergence_stage in {"nascent", "accelerating"}:
        score += 12.0
        reasons.append("newer subnet may benefit from root-proportion weighting")
    if snap.emergence_score is not None and snap.emergence_score >= 70.0:
        score += 13.0
        reasons.append("emergence score supports Spec 421 new-subnet context")
    if snap.emergence_stage == "established":
        score -= 8.0
        risks.append("older subnet may have lower root-proportion support")

    score = _clamp(score)
    return Spec421Component(
        score=round(score, 2),
        reasons=reasons,
        risks=risks,
        notes=notes,
        is_positive=score >= 62.0,
        is_negative=score <= 42.0,
        is_strong=score >= 72.0,
    )


def _weighted_score(components: list[tuple[Optional[float], float]]) -> float:
    available = [(score, weight) for score, weight in components if score is not None]
    if not available:
        return 0.0
    total_weight = sum(weight for _, weight in available)
    return sum(score * weight for score, weight in available) / total_weight


def compute_spec421_signals(
    snapshots: list[SubnetSnapshot],
    history_by_netuid: dict[int, list[SubnetSnapshot]],
) -> dict[int, Spec421Signal]:
    emission_values = compute_emission_value_scores(snapshots)
    result: dict[int, Spec421Signal] = {}
    for snap in snapshots:
        price_ema = compute_price_ema_score(snap, history_by_netuid.get(snap.netuid, []))
        emission_value = emission_values[snap.netuid]
        protocol_context = compute_protocol_context_score(snap)
        score = _weighted_score([
            (price_ema.score, 0.45),
            (emission_value.score, 0.40),
            (protocol_context.score, 0.15),
        ])
        reasons = price_ema.reasons + emission_value.reasons + protocol_context.reasons
        risks = price_ema.risks + emission_value.risks + protocol_context.risks
        notes = sorted(set(price_ema.notes + emission_value.notes + protocol_context.notes))
        result[snap.netuid] = Spec421Signal(
            netuid=snap.netuid,
            price_ema=price_ema,
            emission_value=emission_value,
            protocol_context=protocol_context,
            spec421_score=round(_clamp(score), 2),
            reasons=reasons,
            risks=risks,
            notes=notes,
        )
    return result
```

- [ ] **Step 6: Run Spec 421 tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_spec421.py -q
```

Expected: all tests in `tests/engine/test_spec421.py` pass.

- [ ] **Step 7: Commit Task 1**

```bash
git add engine/spec421.py tests/engine/test_spec421.py
git commit -m "feat: add spec 421 scoring primitives"
```

---

### Task 2: Persist Spec 421 Score Fields

**Files:**
- Modify: `models.py`
- Modify: `db/database.py`
- Modify: `tests/test_database.py`

- [ ] **Step 1: Add failing database persistence test**

Append to `tests/test_database.py`:

```python
async def test_insert_snapshot_persists_spec421_fields(db):
    now = datetime.now(timezone.utc)
    snap = SubnetSnapshot(
        netuid=9,
        polled_at=now,
        price_ema_score=71.0,
        emission_value_score=64.0,
        protocol_context_score=58.0,
        spec421_score=67.5,
    )

    await insert_snapshot(db, snap)
    rows = await get_latest_snapshots(db)

    assert rows[0]["price_ema_score"] == pytest.approx(71.0)
    assert rows[0]["emission_value_score"] == pytest.approx(64.0)
    assert rows[0]["protocol_context_score"] == pytest.approx(58.0)
    assert rows[0]["spec421_score"] == pytest.approx(67.5)
```

- [ ] **Step 2: Run the database test and verify it fails on missing dataclass fields**

Run:

```bash
.venv/bin/pytest tests/test_database.py::test_insert_snapshot_persists_spec421_fields -q
```

Expected: fail with `TypeError: SubnetSnapshot.__init__() got an unexpected keyword argument 'price_ema_score'`.

- [ ] **Step 3: Add fields to `SubnetSnapshot`**

In `models.py`, add these fields after `risk_penalty` and before `swing_score`:

```python
    price_ema_score: Optional[float] = None
    emission_value_score: Optional[float] = None
    protocol_context_score: Optional[float] = None
    spec421_score: Optional[float] = None
```

- [ ] **Step 4: Add schema columns and migrations**

In `db/database.py`, add these columns to `SCHEMA_SQL` inside `CREATE TABLE IF NOT EXISTS snapshots` after `risk_penalty`:

```sql
    price_ema_score   REAL,
    emission_value_score REAL,
    protocol_context_score REAL,
    spec421_score     REAL,
```

In `init_db()`, add these entries to the `for col, definition in [...]` migration list after `("risk_penalty", "REAL")`:

```python
        ("price_ema_score", "REAL"),
        ("emission_value_score", "REAL"),
        ("protocol_context_score", "REAL"),
        ("spec421_score", "REAL"),
```

- [ ] **Step 5: Update snapshot insert bindings**

In `insert_snapshot()`, add the new column names after `risk_penalty`:

```sql
            risk_penalty, price_ema_score, emission_value_score,
            protocol_context_score, spec421_score, swing_score, composite_score,
```

Add the matching values after `snap.risk_penalty`:

```python
        snap.risk_penalty, snap.price_ema_score, snap.emission_value_score,
        snap.protocol_context_score, snap.spec421_score, snap.swing_score,
```

The number of SQL `?` bind slots must match the number of inserted columns. Count them before running tests.

- [ ] **Step 6: Run database tests**

Run:

```bash
.venv/bin/pytest tests/test_database.py -q
```

Expected: all database tests pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add models.py db/database.py tests/test_database.py
git commit -m "feat: persist spec 421 score fields"
```

---

### Task 3: Wire Spec 421 Into Swing Scoring

**Files:**
- Modify: `engine/signals.py`
- Modify: `engine/scorer.py`
- Modify: `tests/engine/test_signals.py`
- Modify: `tests/engine/test_scorer.py`

- [ ] **Step 1: Add failing signal integration tests**

Append to `tests/engine/test_signals.py`:

```python
from engine.spec421 import Spec421Component, Spec421Signal


def make_spec421_signal(score: float) -> Spec421Signal:
    component = Spec421Component(score=score, reasons=["price-based emission setup"])
    return Spec421Signal(
        netuid=1,
        price_ema=component,
        emission_value=component,
        protocol_context=component,
        spec421_score=score,
        reasons=["price-based emission setup"],
        risks=[],
        notes=[],
    )


def test_swing_signal_weights_spec421_above_flow():
    current = make_snap(emission_rank=4)
    history = history_flow([-25.0, -20.0, -10.0])
    relative = compute_relative_value_scores([current])[current.netuid]

    weak_protocol = compute_swing_signal(
        current,
        history,
        relative,
        set(),
        covered=False,
        has_milestone=False,
        spec421=make_spec421_signal(35.0),
    )
    strong_protocol = compute_swing_signal(
        current,
        history,
        relative,
        set(),
        covered=False,
        has_milestone=False,
        spec421=make_spec421_signal(85.0),
    )

    assert strong_protocol.swing_score > weak_protocol.swing_score
    assert strong_protocol.spec421.score == 85.0
    assert "price-based emission setup" in strong_protocol.reasons
```

- [ ] **Step 2: Run the new signal test and verify it fails on function signature**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py::test_swing_signal_weights_spec421_above_flow -q
```

Expected: fail with `TypeError` because `compute_swing_signal()` does not accept `spec421`.

- [ ] **Step 3: Update `SwingSignal` and `compute_swing_signal()`**

In `engine/signals.py`, import `Spec421Signal`:

```python
from engine.spec421 import Spec421Signal
```

Add `spec421` to `SwingSignal` after `netuid`:

```python
    spec421: SignalComponent
```

Change the `compute_swing_signal()` signature to:

```python
def compute_swing_signal(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
    relative_value: SignalComponent,
    alert_types: set[str],
    covered: bool,
    has_milestone: bool,
    spec421: Spec421Signal | None = None,
) -> SwingSignal:
```

Inside `compute_swing_signal()`, after `risk = compute_risk_penalty(...)`, add:

```python
    spec421_component = SignalComponent(
        score=spec421.spec421_score if spec421 is not None else None,
        reasons=spec421.reasons if spec421 is not None else [],
        risks=spec421.risks if spec421 is not None else [],
        is_positive=spec421 is not None and spec421.spec421_score >= 65.0,
        is_negative=spec421 is not None and spec421.spec421_score <= 40.0,
        is_strong=spec421 is not None and spec421.spec421_score >= 75.0,
    )
```

Replace the weighted list with:

```python
    weighted = [
        (spec421_component.score, 0.40),
        (flow.score, 0.20),
        (tradability.score, 0.25),
        (catalyst.score, 0.15),
    ]
```

Replace the `reasons` and `risks` assignments with:

```python
    reasons = (
        spec421_component.reasons
        + flow.reasons
        + relative_value.reasons
        + tradability.reasons
        + catalyst.reasons
    )
    risks = (
        spec421_component.risks
        + flow.risks
        + relative_value.risks
        + tradability.risks
        + risk.risks
    )
```

Add `spec421=spec421_component` to the returned `SwingSignal`.

- [ ] **Step 4: Run signal tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py -q
```

Expected: all signal tests pass after updating test helpers that construct `SwingSignal` directly.

- [ ] **Step 5: Add failing scorer test for persisted Spec 421 fields**

Append to `tests/engine/test_scorer.py`:

```python
def test_score_snapshots_populates_spec421_fields():
    now = datetime.now(timezone.utc)
    snap = make_snap(
        1,
        polled_at=now,
        daily_emission_tao=20.0,
        alpha_mcap_usd=600_000,
        tao_usd_price=300.0,
        volume_24h_alpha=50_000.0,
        alpha_price_tao=1.2,
        alpha_mcap_tao=1_000.0,
        emission_rank=5,
        emergence_stage="nascent",
        emergence_score=75.0,
    )
    history = [
        make_snap(
            1,
            polled_at=now - timedelta(hours=4),
            alpha_price_tao=1.0,
            alpha_mcap_tao=1_000.0,
            net_tao_flow_tao=10.0,
            emission_rank=7,
        ),
        make_snap(
            1,
            polled_at=now - timedelta(hours=3),
            alpha_price_tao=1.05,
            alpha_mcap_tao=1_000.0,
            net_tao_flow_tao=12.0,
            emission_rank=7,
        ),
        make_snap(
            1,
            polled_at=now - timedelta(hours=2),
            alpha_price_tao=1.1,
            alpha_mcap_tao=1_000.0,
            net_tao_flow_tao=15.0,
            emission_rank=6,
        ),
    ]

    score_snapshots([snap], history_by_netuid={1: history})

    assert snap.price_ema_score is not None
    assert snap.emission_value_score is not None
    assert snap.protocol_context_score is not None
    assert snap.spec421_score is not None
    assert snap.swing_score is not None
    assert snap.composite_score == snap.swing_score
```

- [ ] **Step 6: Run the scorer test and verify it fails on missing fields**

Run:

```bash
.venv/bin/pytest tests/engine/test_scorer.py::test_score_snapshots_populates_spec421_fields -q
```

Expected: fail because `score_snapshots()` does not assign the new fields.

- [ ] **Step 7: Wire `engine/scorer.py` to Spec 421**

In `engine/scorer.py`, import:

```python
from engine.spec421 import compute_spec421_signals
```

Inside `score_snapshots()`, after `relative_value_by_netuid = compute_relative_value_scores(snapshots)`, add:

```python
    spec421_by_netuid = compute_spec421_signals(snapshots, history_by_netuid)
```

Inside the snapshot loop, before `swing = compute_swing_signal(...)`, add:

```python
        spec421 = spec421_by_netuid.get(snap.netuid)
```

Pass `spec421=spec421` into `compute_swing_signal()`.

After the call, assign:

```python
        if spec421 is not None:
            snap.price_ema_score = spec421.price_ema.score
            snap.emission_value_score = spec421.emission_value.score
            snap.protocol_context_score = spec421.protocol_context.score
            snap.spec421_score = spec421.spec421_score
```

Keep these compatibility assignments:

```python
        snap.yield_score = relative_value.score
        snap.health_score = swing.tradability.score
        snap.momentum_score = swing.swing_score
        snap.swing_score = swing.swing_score
        snap.composite_score = swing.swing_score
```

- [ ] **Step 8: Update outdated scorer comments**

In `engine/scorer.py`, replace comments that say net flow is the actual emission-share driver with wording equivalent to:

```python
    NOTE: Since Spec 421, subnet emission share is price-based. Flow remains a
    short-term demand and risk signal, not the direct emission-share formula.
```

Do not change `compute_momentum_score()` behavior in this task; the behavior remains a demand/momentum component.

- [ ] **Step 9: Run scorer and signal tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_spec421.py tests/engine/test_signals.py tests/engine/test_scorer.py -q
```

Expected: all selected tests pass.

- [ ] **Step 10: Commit Task 3**

```bash
git add engine/signals.py engine/scorer.py tests/engine/test_signals.py tests/engine/test_scorer.py
git commit -m "feat: wire spec 421 into swing scoring"
```

---

### Task 4: Reconstruct Spec 421 Context In Policy And Recommendations

**Files:**
- Modify: `engine/policy.py`
- Modify: `tests/engine/test_policy.py`

- [ ] **Step 1: Add failing policy reconstruction test**

Append to `tests/engine/test_policy.py`:

```python
def test_build_signal_from_snapshot_reconstructs_spec421_context():
    signal = build_signal_from_snapshot(
        {
            "netuid": 7,
            "flow_score": 55.0,
            "relative_value_score": 60.0,
            "tradability_score": 70.0,
            "catalyst_score": None,
            "risk_penalty": 0.0,
            "swing_score": 72.0,
            "spec421_score": 81.0,
            "price_ema_score": 84.0,
            "emission_value_score": 76.0,
            "protocol_context_score": 62.0,
        },
        set(),
        covered=False,
        has_milestone=False,
    )

    assert signal.spec421.score == pytest.approx(81.0)
    assert signal.spec421.is_positive
    assert "price-based emission setup" in signal.spec421.reasons
```

- [ ] **Step 2: Run the policy test and verify it fails**

Run:

```bash
.venv/bin/pytest tests/engine/test_policy.py::test_build_signal_from_snapshot_reconstructs_spec421_context -q
```

Expected: fail because reconstructed `SwingSignal` has no `spec421` field.

- [ ] **Step 3: Update policy reconstruction**

In `engine/policy.py`, add `spec421_score = snapshot.get("spec421_score")` near the existing score extraction.

Add a `spec421_reasons` block:

```python
    spec421_reasons = []
    spec421_risks = []
    if spec421_score is not None and spec421_score >= 65.0:
        spec421_reasons.append("price-based emission setup")
    if spec421_score is not None and spec421_score <= 40.0:
        spec421_risks.append("weak price-based emission setup")
```

In the returned `SwingSignal`, set:

```python
        spec421=SignalComponent(
            score=spec421_score,
            reasons=spec421_reasons,
            risks=spec421_risks,
            is_positive=spec421_score is not None and spec421_score >= 65.0,
            is_negative=spec421_score is not None and spec421_score <= 40.0,
            is_strong=spec421_score is not None and spec421_score >= 75.0,
        ),
```

Include `spec421_reasons` at the front of the returned `reasons` list and `spec421_risks` at the front of the returned `risks` list.

- [ ] **Step 4: Update direct `SwingSignal` test helpers**

In `tests/engine/test_policy.py`, update `make_signal()` to include:

```python
        spec421=SignalComponent(
            score=82.0,
            reasons=["price-based emission setup"],
            is_positive=True,
            is_strong=True,
        ),
```

- [ ] **Step 5: Run policy tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_policy.py -q
```

Expected: all policy tests pass.

- [ ] **Step 6: Commit Task 4**

```bash
git add engine/policy.py tests/engine/test_policy.py
git commit -m "feat: reconstruct spec 421 policy context"
```

---

### Task 5: Dashboard And Detail Display Updates

**Files:**
- Modify: `web/routes.py`
- Modify: `web/templates/index.html`
- Modify: `web/templates/subnet.html`
- Modify: `web/templates/portfolio.html`
- Modify: `tests/web/test_routes.py`
- Modify: `tests/web/test_portfolio_route.py`

- [ ] **Step 1: Add failing route/template assertions**

In `tests/web/test_routes.py`, add a test that creates a latest snapshot with `spec421_score=77.0`, `price_ema_score=82.0`, `emission_value_score=71.0`, and `protocol_context_score=55.0`, requests `/`, and asserts the HTML contains `Spec 421` and `77.0`.

Use the existing route test fixtures and insertion helpers in that file. The assertions must be:

```python
assert "Spec 421" in response.text
assert "77.0" in response.text
```

In the subnet detail route test section, add an assertion that `/subnet/{netuid}` displays `Spec 421` and `Price EMA`.

- [ ] **Step 2: Run the route tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/web/test_routes.py -q
```

Expected: fail because templates do not display Spec 421 labels.

- [ ] **Step 3: Update dashboard table**

In `web/templates/index.html`, add a `Spec 421` column after `Score`:

```html
<th>Spec 421</th>
```

In each row, after the composite score cell, add:

```html
{% set s421 = row.spec421_score %}
{% set s421_cls = "score-high" if s421 and s421 >= 70
                  else ("score-med" if s421 and s421 >= 40
                  else ("score-low" if s421 else "score-null")) %}
<td class="{{ s421_cls }}">{{ "%.1f"|format(s421) if s421 else "—" }}</td>
```

- [ ] **Step 4: Update subnet detail score card**

In `web/templates/subnet.html`, add a score row before `Yield`:

```html
{% set s421 = snap.spec421_score %}
{% set s421_cls = score_cls(s421) %}
<div class="score-row">
  <span class="score-label">Spec 421</span>
  {% if s421 is not none %}
  <div class="score-bar-wrap">
    <div class="score-bar {{ s421_cls }}" style="width:{{ [(s421 or 0)|int, 100]|min }}%; background:{% if s421 >= 70 %}#00c853{% elif s421 >= 40 %}#ffd600{% else %}#ff5252{% endif %}"></div>
  </div>
  <span class="score-val {{ s421_cls }}">{{ "%.1f"|format(s421) }}</span>
  {% else %}
  <div class="score-bar-wrap"></div>
  <span class="score-val" style="color:#555">—</span>
  {% endif %}
  <span class="score-why">price-based emission setup</span>
</div>
```

In the Chain Stats card, add a full-width stat after Alpha Price:

```html
<div class="stat full-width">
  <span class="stat-label">Spec 421 Components</span>
  <span class="stat-value">
    Price EMA {{ "%.0f"|format(snap.price_ema_score) if snap.price_ema_score is not none else "—" }}
    · Emission value {{ "%.0f"|format(snap.emission_value_score) if snap.emission_value_score is not none else "—" }}
    · Protocol context {{ "%.0f"|format(snap.protocol_context_score) if snap.protocol_context_score is not none else "—" }}
  </span>
</div>
```

- [ ] **Step 5: Update portfolio table**

In `web/templates/portfolio.html`, add `Spec 421` after `Score` in the positions table:

```html
<th>Spec 421</th>
```

For each position row, add:

```html
<td>{{ "%.1f"|format(pos.spec421_score) if pos.spec421_score is not none else "—" }}</td>
```

Update `engine/recommendations.py` only if portfolio row dictionaries do not already carry `spec421_score` from the joined latest snapshot query. If needed, copy it through in the row dict without deriving a new value.

- [ ] **Step 6: Run web route tests**

Run:

```bash
.venv/bin/pytest tests/web/test_routes.py tests/web/test_portfolio_route.py -q
```

Expected: all selected web tests pass.

- [ ] **Step 7: Commit Task 5**

```bash
git add web/routes.py web/templates/index.html web/templates/subnet.html web/templates/portfolio.html tests/web/test_routes.py tests/web/test_portfolio_route.py engine/recommendations.py
git commit -m "feat: show spec 421 score context"
```

---

### Task 6: Backtest Coverage And Calibration Output

**Files:**
- Modify: `scripts/backtest_signals.py`
- Modify: `tests/test_backtest_script.py`

- [ ] **Step 1: Add failing coverage test**

In `tests/test_backtest_script.py`, update `test_score_coverage_counts_swing_and_composite_separately()` to include a snapshot with `spec421_score=75.0` and assert:

```python
assert coverage["spec421_score"] == 1
```

- [ ] **Step 2: Run the backtest script tests and verify failure**

Run:

```bash
.venv/bin/pytest tests/test_backtest_script.py -q
```

Expected: fail with `KeyError: 'spec421_score'`.

- [ ] **Step 3: Add Spec 421 coverage count**

In `scripts/backtest_signals.py`, update `score_coverage()`:

```python
    return {
        "total": len(snapshots),
        "spec421_score": sum(1 for s in snapshots if s.spec421_score is not None),
        "swing_score": sum(1 for s in snapshots if s.swing_score is not None),
        "composite_score": sum(1 for s in snapshots if s.composite_score is not None),
    }
```

Update `main()` print output:

```python
    print(
        f"Loaded {coverage['total']} snapshots — "
        f"spec421_score={coverage['spec421_score']}, "
        f"swing_score={coverage['swing_score']}, "
        f"composite_score={coverage['composite_score']}"
    )
```

- [ ] **Step 4: Run script tests**

Run:

```bash
.venv/bin/pytest tests/test_backtest_script.py -q
```

Expected: all backtest script tests pass.

- [ ] **Step 5: Run local script smoke test**

Run:

```bash
.venv/bin/python -m scripts.backtest_signals --db data/monitor.db
```

Expected before a fresh monitor run: output loads snapshots and reports `spec421_score=0` or a low count on the existing DB. That is acceptable because the new fields start populating after the refactor runs in `main.py`.

- [ ] **Step 6: Commit Task 6**

```bash
git add scripts/backtest_signals.py tests/test_backtest_script.py
git commit -m "feat: report spec 421 score coverage"
```

---

### Task 7: Documentation And Thesis Cleanup

**Files:**
- Modify: `TODOS.md`
- Modify: `docs/superpowers/specs/2026-04-01-tao-investment-criteria.md`

- [ ] **Step 1: Update investment criteria language**

In `docs/superpowers/specs/2026-04-01-tao-investment-criteria.md`, add a top note after the header:

```markdown
> **Spec 421 update, 2026-06:** The direct emission-share model is now price-based:
> `root_proportion × SubnetMovingPrice × (1 - MinerBurned)`. Flow remains a demand
> and risk signal, but it is no longer the direct emission-share formula.
```

Replace the section title `Core Mechanic: dTAO Flow-Based Emissions` with:

```markdown
## Current Core Mechanic: Spec 421 Price-Based Emissions
```

Replace the first paragraph under that section with:

```markdown
As of Spec 421 on mainnet, subnet emission share is price-based. The app treats
price EMA strength and emission value versus market cap as primary scoring inputs,
while keeping net TAO flow as demand confirmation and risk context.
```

- [ ] **Step 2: Update active next item in `TODOS.md`**

Add a completed or active item under the calibration section:

```markdown
### Spec 421 scoring refactor
**What:** Refactor swing scoring around price-based emissions after Spec 421.
Flow remains a demand signal; price EMA and price-based emission value become
the protocol thesis inputs.

**Why:** The old flow-based emission-share thesis is deprecated on mainnet.

**Where to start:** `engine/spec421.py`, then `engine/scorer.py` and persisted
snapshot fields.

**Priority:** P0
```

- [ ] **Step 3: Run docs grep to ensure outdated critical phrase is not active**

Run:

```bash
rg -n "actual emission share driver|flow-based model|flow-based emissions" docs engine config.py
```

Expected: any matches either refer to historical behavior, deprecated behavior, or the Spec 421 update note. Update active recommendations if they still state flow is the current direct emission-share formula.

- [ ] **Step 4: Commit Task 7**

```bash
git add TODOS.md docs/superpowers/specs/2026-04-01-tao-investment-criteria.md
git commit -m "docs: update scoring thesis for spec 421"
```

---

### Task 8: Final Verification

**Files:**
- No code changes unless a verification command reveals a defect.

- [ ] **Step 1: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_spec421.py tests/engine/test_signals.py tests/engine/test_scorer.py tests/engine/test_policy.py tests/test_database.py tests/test_backtest_script.py tests/web/test_routes.py tests/web/test_portfolio_route.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: full suite passes. Existing Telegram deprecation warnings are acceptable if no new warnings appear.

- [ ] **Step 3: Run whitespace check**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 4: Run local backtest smoke command**

Run:

```bash
.venv/bin/python -m scripts.backtest_signals --db data/monitor.db
```

Expected: script prints coverage and bucket report without crashing.

- [ ] **Step 5: Inspect recent commits and repo state**

Run:

```bash
git log --oneline -10
git status --short
```

Expected: implementation commits are present. Any unrelated pre-existing local changes are left untouched and called out in the final handoff.

---

## Self-Review

- Spec coverage: The plan updates the scoring thesis, adds pure Spec 421 scoring, persists fields, wires the score into swing recommendations, updates UI, reports calibration coverage, and updates docs.
- Placeholder scan: The plan intentionally avoids exact root-proportion and miner-burn computation because those inputs are not collected. It stores the absence as notes rather than inventing protocol values.
- Type consistency: `price_ema_score`, `emission_value_score`, `protocol_context_score`, and `spec421_score` are introduced in `models.py`, persisted in `db/database.py`, assigned in `engine/scorer.py`, reconstructed in `engine/policy.py`, and displayed in templates.
