# Subnet Flow Impulse Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add snapshot-level `important_buy` and `important_sell` alerts for unusually large emission-adjusted subnet net flow, while keeping the interface open for later wallet or event attribution.

**Architecture:** Add a pure `engine/flow_impulse.py` classifier that turns current and previous `SubnetSnapshot` objects into a typed `FlowImpulse`. Keep DB schema unchanged by converting impulses into existing `AlertRecord` rows in `engine/alerts.py`. Treat new alert types as aliases of the old flow alerts in scoring, policy, convergence, Telegram, and route alert-type queries.

**Tech Stack:** Python 3.13, dataclasses, aiosqlite, sqlite3, pytest, existing FastAPI/Jinja dashboard and Telegram bot integration.

---

## File Structure

- Create `engine/flow_impulse.py`
  - Owns `FlowImpulse`, threshold checks, price/turnover context, impact score, and `classify_flow_impulse`.
  - Has no DB, Telegram, dashboard, or registry dependency.
- Create `tests/engine/test_flow_impulse.py`
  - Unit tests for pure impulse classification.
- Modify `config.py`
  - Adds flow impulse thresholds and cooldown.
- Modify `engine/alerts.py`
  - Adds `check_flow_impulse`, converts `FlowImpulse` to `AlertRecord`, wires it into `evaluate_alerts`, and prevents duplicate legacy flow alerts from the same evaluation pass.
- Modify `tests/engine/test_alerts.py`
  - Integration tests for new alert persistence, duplicate suppression, and cooldown.
- Modify `engine/signals.py`
  - Adds `important_buy` as a large-flow catalyst alias and `important_sell` as a moderate risk alias.
- Modify `engine/policy.py`
  - Uses the alias sets in persisted-snapshot policy reconstruction.
- Modify `main.py`
  - Includes new alert types in recent-alert context passed into scoring.
- Modify `web/routes.py`
  - Includes new alert types in subnet detail and portfolio recent-alert context.
- Modify `tests/engine/test_signals.py`, `tests/engine/test_policy.py`, and `tests/test_convergence.py`
  - Verifies alias behavior and convergence use.
- Modify `bot/telegram.py`
  - Adds Telegram labels for `important_buy` and `important_sell`.
- Modify `tests/bot/test_telegram.py`
  - Verifies formatting for new alert types.
- Modify `models.py`
  - Updates `AlertRecord.alert_type` comment.
- Create `scripts/backtest_flow_impulses.py`
  - Offline calibration script for alert volume.
- Create `tests/test_backtest_flow_impulses_script.py`
  - Script tests with a temp SQLite DB.

---

### Task 1: Pure Flow Impulse Classifier

**Files:**
- Create: `engine/flow_impulse.py`
- Create: `tests/engine/test_flow_impulse.py`
- Modify: `config.py`

- [ ] **Step 1: Add failing classifier tests**

Create `tests/engine/test_flow_impulse.py`:

```python
from datetime import datetime, timezone

import pytest

import config
from engine.flow_impulse import classify_flow_impulse
from models import SubnetSnapshot


def make_snap(**overrides) -> SubnetSnapshot:
    data = {
        "netuid": 101,
        "polled_at": datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
        "alpha_price_tao": 1.0,
        "alpha_mcap_tao": 1_000.0,
        "alpha_mcap_usd": 100_000.0,
        "tao_in_tao": 1_000.0,
        "volume_24h_alpha": 100.0,
        "buy_slippage_pct": 3.4,
        "sell_slippage_pct": 4.2,
        "net_tao_flow_tao": 0.0,
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def test_important_buy_fires_above_relative_and_absolute_thresholds():
    previous = make_snap(alpha_price_tao=1.0)
    current = make_snap(net_tao_flow_tao=60.0, alpha_price_tao=1.02)

    impulse = classify_flow_impulse(current, previous)

    assert impulse is not None
    assert impulse.alert_type == "important_buy"
    assert impulse.direction == "buy"
    assert impulse.source == "snapshot_net_flow"
    assert impulse.flow_tao == pytest.approx(60.0)
    assert impulse.relative_flow_pct == pytest.approx(0.06)
    assert impulse.threshold_pct == pytest.approx(config.FLOW_IMPULSE_BUY_PCT)
    assert impulse.price_move_pct == pytest.approx(2.0)
    assert impulse.buy_slippage_pct == pytest.approx(3.4)
    assert impulse.impact_score > 50.0


def test_important_sell_fires_above_relative_and_absolute_thresholds():
    previous = make_snap(alpha_price_tao=1.0)
    current = make_snap(net_tao_flow_tao=-40.0, alpha_price_tao=0.985)

    impulse = classify_flow_impulse(current, previous)

    assert impulse is not None
    assert impulse.alert_type == "important_sell"
    assert impulse.direction == "sell"
    assert impulse.flow_tao == pytest.approx(-40.0)
    assert impulse.relative_flow_pct == pytest.approx(0.04)
    assert impulse.threshold_pct == pytest.approx(config.FLOW_IMPULSE_SELL_PCT)
    assert impulse.price_move_pct == pytest.approx(-1.5)
    assert impulse.sell_slippage_pct == pytest.approx(4.2)


def test_small_relative_flow_is_suppressed():
    current = make_snap(net_tao_flow_tao=30.0, alpha_mcap_tao=1_000.0)

    assert classify_flow_impulse(current) is None


def test_tiny_absolute_flow_on_micro_pool_is_suppressed():
    current = make_snap(
        net_tao_flow_tao=5.0,
        alpha_mcap_tao=50.0,
        alpha_mcap_usd=None,
    )

    assert classify_flow_impulse(current) is None


def test_below_minimum_usd_market_cap_is_suppressed_when_present():
    current = make_snap(
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1_000.0,
        alpha_mcap_usd=10_000.0,
    )

    assert classify_flow_impulse(current) is None


def test_missing_usd_market_cap_does_not_suppress_tao_denominated_alert():
    current = make_snap(
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1_000.0,
        alpha_mcap_usd=None,
    )

    impulse = classify_flow_impulse(current)

    assert impulse is not None
    assert impulse.alert_type == "important_buy"


def test_price_confirmation_is_not_required():
    previous = make_snap(alpha_price_tao=1.0)
    current = make_snap(net_tao_flow_tao=60.0, alpha_price_tao=0.95)

    impulse = classify_flow_impulse(current, previous)

    assert impulse is not None
    assert impulse.alert_type == "important_buy"
    assert impulse.price_move_pct == pytest.approx(-5.0)
    assert "price moved against impulse" in impulse.risks


def test_volume_turnover_is_included_when_fields_exist():
    current = make_snap(
        net_tao_flow_tao=60.0,
        volume_24h_alpha=100.0,
        alpha_price_tao=0.5,
        alpha_mcap_tao=1_000.0,
    )

    impulse = classify_flow_impulse(current)

    assert impulse is not None
    assert impulse.volume_turnover_pct == pytest.approx(5.0)
```

- [ ] **Step 2: Run classifier tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/engine/test_flow_impulse.py -q
```

Expected: fail during import with `ModuleNotFoundError: No module named 'engine.flow_impulse'`.

- [ ] **Step 3: Add config constants**

In `config.py`, add these lines under the existing capital-protection alert constants:

```python
FLOW_IMPULSE_MIN_TAO: float = 25.0          # minimum absolute net TAO flow in one poll
FLOW_IMPULSE_BUY_PCT: float = 0.05          # net inflow >= 5% of pool in one poll
FLOW_IMPULSE_SELL_PCT: float = 0.03         # net outflow >= 3% of pool in one poll
FLOW_IMPULSE_MIN_MCAP_USD: float = 50_000.0 # suppress tiny USD market caps when known
FLOW_IMPULSE_COOLDOWN_HOURS: int = 2        # direction-specific flow impulse cooldown
```

- [ ] **Step 4: Add `engine/flow_impulse.py`**

Create `engine/flow_impulse.py`:

```python
from dataclasses import dataclass
from typing import Literal

import config
from models import SubnetSnapshot


FlowDirection = Literal["buy", "sell"]
FlowAlertType = Literal["important_buy", "important_sell"]
FlowSource = Literal["snapshot_net_flow"]


@dataclass(frozen=True)
class FlowImpulse:
    netuid: int
    direction: FlowDirection
    alert_type: FlowAlertType
    source: FlowSource
    flow_tao: float
    relative_flow_pct: float
    threshold_pct: float
    impact_score: float
    price_move_pct: float | None = None
    volume_turnover_pct: float | None = None
    buy_slippage_pct: float | None = None
    sell_slippage_pct: float | None = None
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _price_move_pct(
    current: SubnetSnapshot,
    previous: SubnetSnapshot | None,
) -> float | None:
    if previous is None:
        return None
    if current.alpha_price_tao is None or previous.alpha_price_tao is None:
        return None
    if previous.alpha_price_tao <= 0:
        return None
    return ((current.alpha_price_tao - previous.alpha_price_tao) / previous.alpha_price_tao) * 100.0


def _volume_turnover_pct(snap: SubnetSnapshot) -> float | None:
    if (
        snap.volume_24h_alpha is None
        or snap.alpha_price_tao is None
        or snap.alpha_mcap_tao is None
        or snap.alpha_mcap_tao <= 0
    ):
        return None
    return (snap.volume_24h_alpha * snap.alpha_price_tao / snap.alpha_mcap_tao) * 100.0


def _price_confirms(direction: FlowDirection, price_move_pct: float | None) -> bool:
    if price_move_pct is None:
        return False
    if direction == "buy":
        return price_move_pct > 0
    return price_move_pct < 0


def _impact_score(
    *,
    flow_tao: float,
    relative_flow_pct: float,
    threshold_pct: float,
    price_confirmed: bool,
) -> float:
    relative_multiple = relative_flow_pct / threshold_pct
    absolute_multiple = abs(flow_tao) / config.FLOW_IMPULSE_MIN_TAO
    score = 50.0
    score += 25.0 * min(max(relative_multiple - 1.0, 0.0), 2.0) / 2.0
    score += 15.0 * min(max(absolute_multiple - 1.0, 0.0), 4.0) / 4.0
    if price_confirmed:
        score += 10.0
    return round(_clamp(score), 2)


def classify_flow_impulse(
    current: SubnetSnapshot,
    previous: SubnetSnapshot | None = None,
) -> FlowImpulse | None:
    flow = current.net_tao_flow_tao
    pool = current.alpha_mcap_tao
    if flow is None or pool is None or pool <= 0:
        return None
    if flow == 0:
        return None
    if abs(flow) < config.FLOW_IMPULSE_MIN_TAO:
        return None
    if (
        current.alpha_mcap_usd is not None
        and current.alpha_mcap_usd < config.FLOW_IMPULSE_MIN_MCAP_USD
    ):
        return None

    direction: FlowDirection = "buy" if flow > 0 else "sell"
    alert_type: FlowAlertType = "important_buy" if direction == "buy" else "important_sell"
    threshold = (
        config.FLOW_IMPULSE_BUY_PCT
        if direction == "buy"
        else config.FLOW_IMPULSE_SELL_PCT
    )
    relative = abs(flow) / pool
    if relative < threshold:
        return None

    price_move = _price_move_pct(current, previous)
    price_confirmed = _price_confirms(direction, price_move)
    reasons: list[str] = [
        f"{direction} pressure {relative * 100:.1f}% of pool",
        "emission-adjusted net flow",
    ]
    risks: list[str] = []
    if price_confirmed:
        reasons.append("price confirmed impulse direction")
    elif price_move is not None:
        risks.append("price moved against impulse")

    turnover = _volume_turnover_pct(current)
    score = _impact_score(
        flow_tao=flow,
        relative_flow_pct=relative,
        threshold_pct=threshold,
        price_confirmed=price_confirmed,
    )

    return FlowImpulse(
        netuid=current.netuid,
        direction=direction,
        alert_type=alert_type,
        source="snapshot_net_flow",
        flow_tao=round(flow, 6),
        relative_flow_pct=round(relative, 6),
        threshold_pct=threshold,
        impact_score=score,
        price_move_pct=round(price_move, 4) if price_move is not None else None,
        volume_turnover_pct=round(turnover, 4) if turnover is not None else None,
        buy_slippage_pct=current.buy_slippage_pct,
        sell_slippage_pct=current.sell_slippage_pct,
        reasons=tuple(reasons),
        risks=tuple(risks),
    )
```

- [ ] **Step 5: Run classifier tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_flow_impulse.py -q
```

Expected: all tests in `tests/engine/test_flow_impulse.py` pass.

- [ ] **Step 6: Commit classifier**

Run:

```bash
git add config.py engine/flow_impulse.py tests/engine/test_flow_impulse.py
git commit -m "feat: add subnet flow impulse classifier"
```

---

### Task 2: Alert Evaluation Integration

**Files:**
- Modify: `engine/alerts.py`
- Modify: `tests/engine/test_alerts.py`

- [ ] **Step 1: Add failing alert integration tests**

In `tests/engine/test_alerts.py`, add `check_flow_impulse` to the import list:

```python
    check_flow_impulse,
```

Append these tests after `test_whale_inflow_does_not_fire_on_outflow`:

```python
def test_check_flow_impulse_builds_important_buy_alert():
    prev = make_snap(1, alpha_price_tao=1.0)
    curr = make_snap(
        1,
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=1.02,
        buy_slippage_pct=3.4,
    )

    result = check_flow_impulse(curr, prev)

    assert result is not None
    assert result.alert_type == "important_buy"
    assert result.current_value == pytest.approx(0.06)
    assert result.threshold == pytest.approx(0.05)
    assert "Important buy pressure" in result.description
    assert "not wallet-attributed" in result.description


def test_check_flow_impulse_builds_important_sell_alert():
    prev = make_snap(1, alpha_price_tao=1.0)
    curr = make_snap(
        1,
        net_tao_flow_tao=-40.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=0.985,
        sell_slippage_pct=5.2,
    )

    result = check_flow_impulse(curr, prev)

    assert result is not None
    assert result.alert_type == "important_sell"
    assert result.current_value == pytest.approx(0.04)
    assert result.threshold == pytest.approx(0.03)
    assert "Important sell pressure" in result.description
    assert "Impact score" in result.description
```

Append these async tests near the other `evaluate_alerts` integration tests:

```python
async def test_evaluate_alerts_fires_important_buy_without_legacy_duplicate(db):
    snap = make_snap(
        1,
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=1.02,
        buy_slippage_pct=3.4,
    )
    prev = make_snap(1, alpha_price_tao=1.0)
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}

    alerts = await evaluate_alerts(db, [snap], registry, {1: prev}, known_netuids={1})

    alert_types = {alert.alert_type for alert in alerts}
    assert "important_buy" in alert_types
    assert "whale_inflow" not in alert_types


async def test_evaluate_alerts_fires_important_sell_without_legacy_duplicate(db):
    snap = make_snap(
        1,
        net_tao_flow_tao=-40.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=0.985,
        sell_slippage_pct=5.2,
    )
    prev = make_snap(1, alpha_price_tao=1.0)
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}

    alerts = await evaluate_alerts(db, [snap], registry, {1: prev}, known_netuids={1})

    alert_types = {alert.alert_type for alert in alerts}
    assert "important_sell" in alert_types
    assert "tao_outflow" not in alert_types


async def test_evaluate_alerts_respects_flow_impulse_cooldown(db):
    from db.database import insert_alert

    existing = AlertRecord(
        fired_at=now(),
        netuid=1,
        subnet_name="Apex",
        alert_type="important_buy",
        description="existing",
        current_value=0.06,
        threshold=0.05,
    )
    await insert_alert(db, existing)

    snap = make_snap(
        1,
        net_tao_flow_tao=70.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
    )
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}

    alerts = await evaluate_alerts(db, [snap], registry, {}, known_netuids={1})

    assert all(alert.alert_type != "important_buy" for alert in alerts)
```

- [ ] **Step 2: Run alert tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/engine/test_alerts.py -q
```

Expected: fail because `check_flow_impulse` is not exported from `engine.alerts`.

- [ ] **Step 3: Wire `FlowImpulse` into alerts**

In `engine/alerts.py`, add this import:

```python
from engine.flow_impulse import FlowImpulse, classify_flow_impulse
```

Add this helper below `check_whale_inflow`:

```python
def _flow_impulse_description(impulse: FlowImpulse) -> str:
    direction = impulse.direction
    slippage = (
        impulse.buy_slippage_pct
        if direction == "buy"
        else impulse.sell_slippage_pct
    )
    parts = [
        (
            f"Important {direction} pressure: {impulse.flow_tao:+.1f} TAO net flow "
            f"in one poll, {impulse.relative_flow_pct * 100:.1f}% of pool "
            f"(threshold {impulse.threshold_pct * 100:.1f}%)."
        )
    ]
    context: list[str] = []
    if impulse.price_move_pct is not None:
        context.append(f"Price {impulse.price_move_pct:+.1f}% since prior poll")
    if slippage is not None:
        context.append(
            f"{direction.title()} slippage {slippage:.1f}% on reference size"
        )
    if impulse.volume_turnover_pct is not None:
        context.append(f"24h turnover {impulse.volume_turnover_pct:.2f}% of pool")
    context.append(f"Impact score {impulse.impact_score:.0f}/100")
    parts.append(". ".join(context) + ".")
    parts.append("Source: emission-adjusted snapshot net flow, not wallet-attributed.")
    return " ".join(parts)


def check_flow_impulse(
    current: SubnetSnapshot,
    prev: SubnetSnapshot | None = None,
) -> Optional[AlertRecord]:
    """Important buy/sell pressure from emission-adjusted snapshot net flow."""
    impulse = classify_flow_impulse(current, prev)
    if impulse is None:
        return None
    return AlertRecord(
        fired_at=datetime.now(timezone.utc),
        netuid=current.netuid,
        subnet_name=f"SN{current.netuid}",
        alert_type=impulse.alert_type,
        description=_flow_impulse_description(impulse),
        current_value=impulse.relative_flow_pct,
        threshold=impulse.threshold_pct,
    )
```

Add this cooldown helper above `evaluate_alerts`:

```python
def _cooldown_hours_for_alert(alert_type: str) -> int:
    if alert_type == "emergence_watch":
        return config.EMERGENCE_WATCH_COOLDOWN_HOURS
    if alert_type in {"important_buy", "important_sell"}:
        return config.FLOW_IMPULSE_COOLDOWN_HOURS
    return config.ALERT_COOLDOWN_HOURS
```

In `evaluate_alerts`, update the docstring alert lists:

```python
    Capital-protection alerts: important_buy, important_sell,
      emission_near_zero, liquidity_floor, hyperparameter_change
    Legacy helpers kept for compatibility: tao_outflow, whale_inflow
```

In the capital-protection candidate section, replace the two legacy candidate appends:

```python
        # 8. Net TAO outflow (capital flight this poll)
        candidates.append(check_tao_outflow(snap))

        # 9. Whale TAO inflow (large capital entry this poll)
        candidates.append(check_whale_inflow(snap))
```

with:

```python
        # 8. Important buy/sell pressure from emission-adjusted net flow.
        candidates.append(check_flow_impulse(snap, prev))
```

In the dedup block, replace the inline cooldown expression:

```python
            cooldown_hours = (
                config.EMERGENCE_WATCH_COOLDOWN_HOURS
                if alert.alert_type == "emergence_watch"
                else config.ALERT_COOLDOWN_HOURS
            )
```

with:

```python
            cooldown_hours = _cooldown_hours_for_alert(alert.alert_type)
```

- [ ] **Step 4: Run alert tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_alerts.py -q
```

Expected: all tests in `tests/engine/test_alerts.py` pass.

- [ ] **Step 5: Run classifier and alert tests together**

Run:

```bash
.venv/bin/pytest tests/engine/test_flow_impulse.py tests/engine/test_alerts.py -q
```

Expected: both files pass.

- [ ] **Step 6: Commit alert integration**

Run:

```bash
git add engine/alerts.py tests/engine/test_alerts.py
git commit -m "feat: emit important flow impulse alerts"
```

---

### Task 3: Scoring, Policy, Convergence, and Route Aliases

**Files:**
- Modify: `engine/signals.py`
- Modify: `engine/policy.py`
- Modify: `engine/alerts.py`
- Modify: `main.py`
- Modify: `web/routes.py`
- Modify: `tests/engine/test_signals.py`
- Modify: `tests/engine/test_policy.py`
- Modify: `tests/test_convergence.py`

- [ ] **Step 1: Add failing alias tests**

Append to `tests/engine/test_signals.py`:

```python
def test_important_buy_counts_as_large_inflow_catalyst():
    score = compute_catalyst_score({"important_buy"}, covered=False, has_milestone=False)

    assert score.score == 25.0
    assert score.is_positive
    assert "large net inflow catalyst" in score.reasons


def test_important_sell_counts_as_moderate_risk():
    penalty = compute_risk_penalty({"important_sell"}, flow_negative=False)

    assert penalty.penalty == 12.0
    assert penalty.moderate_count == 1
    assert "moderate risk alert" in penalty.risks
```

Append to `tests/engine/test_policy.py` after `test_build_signal_from_snapshot_uses_persisted_fields_and_context`:

```python
def test_build_signal_from_snapshot_treats_important_buy_as_catalyst():
    signal = build_signal_from_snapshot(
        {
            "netuid": 7,
            "flow_score": 55.0,
            "relative_value_score": 60.0,
            "tradability_score": 70.0,
            "catalyst_score": 25.0,
            "risk_penalty": 0.0,
            "swing_score": 68.0,
        },
        {"important_buy"},
        covered=False,
        has_milestone=False,
    )

    assert signal.catalyst.is_positive
    assert "large net inflow catalyst" in signal.catalyst.reasons


def test_build_signal_from_snapshot_treats_important_sell_as_moderate_risk():
    signal = build_signal_from_snapshot(
        {
            "netuid": 7,
            "flow_score": 55.0,
            "relative_value_score": 60.0,
            "tradability_score": 70.0,
            "catalyst_score": None,
            "risk_penalty": 12.0,
            "swing_score": 58.0,
        },
        {"important_sell"},
        covered=False,
        has_milestone=False,
    )

    assert signal.risk.moderate_count == 1
    assert "moderate risk alert" in signal.risk.risks
```

Append to `tests/test_convergence.py`:

```python
@pytest.mark.asyncio
async def test_evaluate_convergence_accepts_important_buy_as_flow_signal(db):
    now = datetime.now(timezone.utc)
    for alert_type in ("milestone", "important_buy"):
        await insert_alert(
            db,
            AlertRecord(
                fired_at=now,
                netuid=3,
                subnet_name="Templar",
                alert_type=alert_type,
                description=alert_type,
            ),
        )

    fired = await evaluate_convergence(db, {3: {"name": "Templar"}})

    assert len(fired) == 1
    assert fired[0].alert_type == "convergence"
```

- [ ] **Step 2: Run alias tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py::test_important_buy_counts_as_large_inflow_catalyst tests/engine/test_signals.py::test_important_sell_counts_as_moderate_risk tests/engine/test_policy.py::test_build_signal_from_snapshot_treats_important_buy_as_catalyst tests/engine/test_policy.py::test_build_signal_from_snapshot_treats_important_sell_as_moderate_risk tests/test_convergence.py::test_evaluate_convergence_accepts_important_buy_as_flow_signal -q
```

Expected: fail because new alert aliases are not wired.

- [ ] **Step 3: Update `engine/signals.py` aliases**

In `engine/signals.py`, replace the alert set definitions with:

```python
SEVERE_RISK_ALERTS = {"emission_near_zero", "liquidity_floor"}
MODERATE_RISK_ALERTS = {
    "ownership_transfer",
    "hyperparameter_change",
    "tao_outflow",
    "important_sell",
    "dead_github",
}
FLOW_CATALYST_ALERTS = {"whale_inflow", "important_buy"}
CATALYST_ALERTS = {
    "convergence",
    "analyst_mention",
    "milestone",
    "github_spike",
    *FLOW_CATALYST_ALERTS,
}
```

In `compute_catalyst_score`, replace:

```python
    if "whale_inflow" in alert_types:
        score += 25.0
        reasons.append("large net inflow catalyst")
```

with:

```python
    if FLOW_CATALYST_ALERTS & alert_types:
        score += 25.0
        reasons.append("large net inflow catalyst")
```

- [ ] **Step 4: Update `engine/policy.py` alias handling**

In `engine/policy.py`, replace the import from `engine.signals` with:

```python
from engine.signals import (
    FLOW_CATALYST_ALERTS,
    MODERATE_RISK_ALERTS,
    RiskSignal,
    SEVERE_RISK_ALERTS,
    SignalComponent,
    SwingSignal,
)
```

In `build_signal_from_snapshot`, replace the `catalyst_positive` assignment with:

```python
    catalyst_positive = (
        "convergence" in alert_types
        or "milestone" in alert_types
        or bool(FLOW_CATALYST_ALERTS & alert_types)
        or covered
        or has_milestone
        or (snapshot.get("momentum_score") or 0.0) >= 70.0
    )
```

Replace the severe-risk expression with:

```python
    severe_risk = bool((SEVERE_RISK_ALERTS & alert_types) or risk_penalty >= 45.0)
```

Replace the moderate-count expression with:

```python
    moderate_count = len(MODERATE_RISK_ALERTS & alert_types)
```

In the `catalyst_reasons` block, add this after the convergence check:

```python
    if FLOW_CATALYST_ALERTS & alert_types:
        catalyst_reasons.append("large net inflow catalyst")
```

- [ ] **Step 5: Update convergence signal types**

In `engine/alerts.py`, update `_CONVERGENCE_SIGNAL_TYPES` to:

```python
_CONVERGENCE_SIGNAL_TYPES = [
    "milestone",
    "analyst_mention",
    "whale_inflow",
    "important_buy",
    "github_spike",
]
```

- [ ] **Step 6: Include new alert types in scoring context**

In `main.py`, in the `get_recent_alert_types_per_netuid` list inside `poll_cycle`, replace:

```python
            "convergence", "analyst_mention", "milestone", "whale_inflow",
            "github_spike", "emission_near_zero", "liquidity_floor",
            "ownership_transfer", "tao_outflow", "dead_github",
```

with:

```python
            "convergence", "analyst_mention", "milestone", "whale_inflow",
            "important_buy", "github_spike", "emission_near_zero",
            "liquidity_floor", "ownership_transfer", "tao_outflow",
            "important_sell", "dead_github",
```

In `web/routes.py`, update both `get_recent_alert_types_per_netuid` lists by adding `important_buy` after `analyst_mention` and `important_sell` after `tao_outflow`:

```python
                "important_buy",
```

```python
                "important_sell",
```

- [ ] **Step 7: Run alias tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py tests/engine/test_policy.py tests/test_convergence.py -q
```

Expected: all tests in these files pass.

- [ ] **Step 8: Verify alert-type query lists include aliases**

Run:

```bash
rg -n "\"important_buy\"|\"important_sell\"" engine main.py web tests
```

Expected: output includes `engine/alerts.py`, `engine/signals.py`, `engine/policy.py`, `main.py`, `web/routes.py`, and the new tests.

- [ ] **Step 9: Commit alias wiring**

Run:

```bash
git add engine/signals.py engine/policy.py engine/alerts.py main.py web/routes.py tests/engine/test_signals.py tests/engine/test_policy.py tests/test_convergence.py
git commit -m "feat: treat important flow alerts as signal aliases"
```

---

### Task 4: Telegram Labels and Alert Type Documentation

**Files:**
- Modify: `bot/telegram.py`
- Modify: `tests/bot/test_telegram.py`
- Modify: `models.py`

- [ ] **Step 1: Add failing Telegram formatting tests**

Append to `tests/bot/test_telegram.py`:

```python
def test_format_important_buy_alert_message():
    alert = make_alert("important_buy")
    alert.description = "Important buy pressure"

    msg = format_alert_message(alert)

    assert ALERT_TYPE_EMOJI["important_buy"] == "🟢"
    assert "Important Buy" in msg
    assert "Important buy pressure" in msg


def test_format_important_sell_alert_message():
    alert = make_alert("important_sell")
    alert.description = "Important sell pressure"

    msg = format_alert_message(alert)

    assert ALERT_TYPE_EMOJI["important_sell"] == "🔴"
    assert "Important Sell" in msg
    assert "Important sell pressure" in msg
```

Also update the import in `tests/bot/test_telegram.py`:

```python
from bot.telegram import ALERT_TYPE_EMOJI, TelegramBot, format_alert_message
```

- [ ] **Step 2: Run Telegram tests before implementation**

Run:

```bash
.venv/bin/pytest tests/bot/test_telegram.py::test_format_important_buy_alert_message tests/bot/test_telegram.py::test_format_important_sell_alert_message -q
```

Expected: fail with `KeyError: 'important_buy'` or `KeyError: 'important_sell'`.

- [ ] **Step 3: Add explicit Telegram alert mappings**

In `bot/telegram.py`, add these entries to `ALERT_TYPE_EMOJI`:

```python
    "important_buy": "🟢",
    "important_sell": "🔴",
```

- [ ] **Step 4: Update `AlertRecord.alert_type` comment**

In `models.py`, replace the `AlertRecord.alert_type` comment block with:

```python
    alert_type: str       # project-monitoring: 'emission_divergence' | 'dead_github' |
                          #   'emission_drop' | 'github_spike' | 'ownership_transfer' |
                          #   'social_silence' | 'new_entry'
                          # capital-protection: 'important_buy' | 'important_sell' |
                          #   'tao_outflow' | 'whale_inflow' | 'emission_near_zero' |
                          #   'liquidity_floor' | 'hyperparameter_change'
                          # watch/catalyst: 'emergence_watch' | 'analyst_mention' |
                          #   'milestone' | 'convergence'
```

- [ ] **Step 5: Run Telegram and model tests**

Run:

```bash
.venv/bin/pytest tests/bot/test_telegram.py tests/test_models.py -q
```

Expected: both files pass.

- [ ] **Step 6: Commit Telegram labels**

Run:

```bash
git add bot/telegram.py tests/bot/test_telegram.py models.py
git commit -m "feat: label important flow alerts in Telegram"
```

---

### Task 5: Flow Impulse Calibration Script

**Files:**
- Create: `scripts/backtest_flow_impulses.py`
- Create: `tests/test_backtest_flow_impulses_script.py`

- [ ] **Step 1: Add failing script tests**

Create `tests/test_backtest_flow_impulses_script.py`:

```python
from datetime import datetime, timedelta, timezone
import sqlite3

from scripts.backtest_flow_impulses import main, run_backtest


def _create_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE snapshots (
            id INTEGER PRIMARY KEY,
            netuid INTEGER NOT NULL,
            polled_at TEXT NOT NULL,
            alpha_price_tao REAL,
            alpha_mcap_tao REAL,
            alpha_mcap_usd REAL,
            volume_24h_alpha REAL,
            buy_slippage_pct REAL,
            sell_slippage_pct REAL,
            net_tao_flow_tao REAL
        )
        """
    )
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    rows = [
        (1, 101, now - timedelta(minutes=15), 1.0, 1000.0, 100_000.0, 100.0, 3.0, 4.0, 0.0),
        (2, 101, now, 1.02, 1000.0, 100_000.0, 100.0, 3.0, 4.0, 60.0),
        (3, 102, now - timedelta(minutes=15), 1.0, 1000.0, 100_000.0, 100.0, 3.0, 4.0, 0.0),
        (4, 102, now, 0.985, 1000.0, 100_000.0, 100.0, 3.0, 4.0, -40.0),
    ]
    conn.executemany(
        """
        INSERT INTO snapshots (
            id, netuid, polled_at, alpha_price_tao, alpha_mcap_tao, alpha_mcap_usd,
            volume_24h_alpha, buy_slippage_pct, sell_slippage_pct, net_tao_flow_tao
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        [(row[0], row[1], row[2].isoformat(), *row[3:]) for row in rows],
    )
    conn.commit()
    conn.close()


def test_run_backtest_reports_buy_and_sell_counts(tmp_path):
    db_path = tmp_path / "flow.db"
    _create_db(db_path)

    report = run_backtest(str(db_path), cooldown_hours=2, limit_examples=5)

    assert report["total_impulses"] == 2
    assert report["by_direction"] == {"buy": 1, "sell": 1}
    assert report["by_netuid"][101] == 1
    assert report["by_netuid"][102] == 1
    assert report["top_examples"][0]["impact_score"] >= report["top_examples"][1]["impact_score"]


def test_main_prints_summary(tmp_path, capsys):
    db_path = tmp_path / "flow.db"
    _create_db(db_path)

    exit_code = main(["--db", str(db_path), "--limit-examples", "2"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Flow impulse backtest" in captured.out
    assert "total_impulses=2" in captured.out
    assert "direction buy=1 sell=1" in captured.out
```

- [ ] **Step 2: Run script tests to verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_backtest_flow_impulses_script.py -q
```

Expected: fail during import with `ModuleNotFoundError: No module named 'scripts.backtest_flow_impulses'`.

- [ ] **Step 3: Add calibration script**

Create `scripts/backtest_flow_impulses.py`:

```python
"""Report historical flow impulse alert volume from stored snapshots.

Usage:
    .venv/bin/python -m scripts.backtest_flow_impulses [--db PATH] [--limit-examples 10]
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

import config
from engine.flow_impulse import FlowImpulse, classify_flow_impulse
from models import SubnetSnapshot

_KNOWN_FIELDS = {field.name for field in fields(SubnetSnapshot)}


@dataclass(frozen=True)
class ImpulseExample:
    netuid: int
    polled_at: str
    alert_type: str
    direction: str
    flow_tao: float
    relative_flow_pct: float
    price_move_pct: float | None
    impact_score: float


def _row_to_snapshot(row: Mapping[str, Any]) -> SubnetSnapshot:
    data = {key: value for key, value in dict(row).items() if key in _KNOWN_FIELDS}
    polled_at = data.get("polled_at")
    if isinstance(polled_at, str):
        data["polled_at"] = datetime.fromisoformat(polled_at)
    return SubnetSnapshot(**data)


def load_snapshots(db_path: str) -> list[SubnetSnapshot]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        table_cols = [row[1] for row in conn.execute("PRAGMA table_info(snapshots)")]
        cols = [col for col in table_cols if col in _KNOWN_FIELDS]
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM snapshots ORDER BY polled_at ASC, netuid ASC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_snapshot(row) for row in rows]


def _cooldown_key(impulse: FlowImpulse) -> tuple[int, str]:
    return (impulse.netuid, impulse.alert_type)


def collect_impulses(
    snapshots: Iterable[SubnetSnapshot],
    *,
    cooldown_hours: int,
) -> list[tuple[SubnetSnapshot, FlowImpulse]]:
    previous_by_netuid: dict[int, SubnetSnapshot] = {}
    last_fired_at: dict[tuple[int, str], datetime] = {}
    impulses: list[tuple[SubnetSnapshot, FlowImpulse]] = []
    cooldown = timedelta(hours=cooldown_hours)

    for snap in snapshots:
        previous = previous_by_netuid.get(snap.netuid)
        impulse = classify_flow_impulse(snap, previous)
        previous_by_netuid[snap.netuid] = snap
        if impulse is None:
            continue
        key = _cooldown_key(impulse)
        fired_at = last_fired_at.get(key)
        if fired_at is not None and snap.polled_at - fired_at < cooldown:
            continue
        last_fired_at[key] = snap.polled_at
        impulses.append((snap, impulse))

    return impulses


def _example(snap: SubnetSnapshot, impulse: FlowImpulse) -> ImpulseExample:
    return ImpulseExample(
        netuid=impulse.netuid,
        polled_at=snap.polled_at.isoformat(),
        alert_type=impulse.alert_type,
        direction=impulse.direction,
        flow_tao=impulse.flow_tao,
        relative_flow_pct=impulse.relative_flow_pct,
        price_move_pct=impulse.price_move_pct,
        impact_score=impulse.impact_score,
    )


def run_backtest(
    db_path: str,
    *,
    cooldown_hours: int = config.FLOW_IMPULSE_COOLDOWN_HOURS,
    limit_examples: int = 10,
) -> dict[str, Any]:
    snapshots = load_snapshots(db_path)
    impulses = collect_impulses(snapshots, cooldown_hours=cooldown_hours)
    by_direction: dict[str, int] = {}
    by_netuid: dict[int, int] = {}
    by_day: dict[str, int] = {}

    for snap, impulse in impulses:
        by_direction[impulse.direction] = by_direction.get(impulse.direction, 0) + 1
        by_netuid[impulse.netuid] = by_netuid.get(impulse.netuid, 0) + 1
        day = snap.polled_at.date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1

    examples = sorted(
        [_example(snap, impulse) for snap, impulse in impulses],
        key=lambda item: item.impact_score,
        reverse=True,
    )[:limit_examples]

    return {
        "db_path": db_path,
        "snapshot_count": len(snapshots),
        "total_impulses": len(impulses),
        "cooldown_hours": cooldown_hours,
        "by_direction": dict(sorted(by_direction.items())),
        "by_netuid": dict(sorted(by_netuid.items())),
        "by_day": dict(sorted(by_day.items())),
        "top_examples": [asdict(example) for example in examples],
    }


def format_report(report: Mapping[str, Any]) -> str:
    buy_count = report["by_direction"].get("buy", 0)
    sell_count = report["by_direction"].get("sell", 0)
    lines = [
        (
            "Flow impulse backtest "
            f"snapshots={report['snapshot_count']} "
            f"total_impulses={report['total_impulses']} "
            f"cooldown_hours={report['cooldown_hours']}"
        ),
        f"direction buy={buy_count} sell={sell_count}",
        "top_examples:",
    ]
    for example in report["top_examples"]:
        lines.append(
            "  "
            f"SN{example['netuid']} {example['polled_at']} {example['alert_type']} "
            f"flow={example['flow_tao']:+.1f} "
            f"relative={example['relative_flow_pct'] * 100:.1f}% "
            f"price={example['price_move_pct']} "
            f"impact={example['impact_score']:.0f}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backtest flow impulse alert volume.")
    parser.add_argument("--db", default=config.DB_PATH, help="Path to SQLite DB.")
    parser.add_argument(
        "--limit-examples",
        type=int,
        default=10,
        help="Number of high-impact examples to print.",
    )
    args = parser.parse_args(argv)

    report = run_backtest(args.db, limit_examples=args.limit_examples)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run script tests**

Run:

```bash
.venv/bin/pytest tests/test_backtest_flow_impulses_script.py -q
```

Expected: both script tests pass.

- [ ] **Step 5: Run the calibration script against the local monitor DB**

Run:

```bash
.venv/bin/python -m scripts.backtest_flow_impulses --db data/monitor.db --limit-examples 10
```

Expected: prints a `Flow impulse backtest` summary. Record the `total_impulses`, buy count, and sell count in the final implementation summary.

- [ ] **Step 6: Commit calibration script**

Run:

```bash
git add scripts/backtest_flow_impulses.py tests/test_backtest_flow_impulses_script.py
git commit -m "feat: add flow impulse calibration script"
```

---

### Task 6: Final Verification

**Files:**
- No new code files beyond prior tasks.

- [ ] **Step 1: Run targeted flow impulse suite**

Run:

```bash
.venv/bin/pytest tests/engine/test_flow_impulse.py tests/engine/test_alerts.py tests/engine/test_signals.py tests/engine/test_policy.py tests/test_convergence.py tests/bot/test_telegram.py tests/test_backtest_flow_impulses_script.py -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run full test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: full suite passes.

- [ ] **Step 3: Run whitespace and status checks**

Run:

```bash
git diff --check
git status --short
```

Expected:
- `git diff --check` exits 0 with no output.
- `git status --short` shows only expected local files. Existing user-owned note changes may still appear and must not be reverted.

- [ ] **Step 4: Confirm recent commits**

Run:

```bash
git log --oneline -6
```

Expected: output includes these new commits in order:

```text
feat: add flow impulse calibration script
feat: label important flow alerts in Telegram
feat: treat important flow alerts as signal aliases
feat: emit important flow impulse alerts
feat: add subnet flow impulse classifier
```

- [ ] **Step 5: Final implementation summary**

Report:
- commits created
- targeted test command and result
- full test command and result
- calibration script output summary from `data/monitor.db`
- any unrelated dirty files left untouched

Do not claim completion until the verification commands in this task have been run and read.
