# TAO Swing Signal Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broad yield/health/momentum composite with an explicit 1-2 week swing signal model.

**Architecture:** Add `engine/signals.py` as the policy module for flow, relative value, tradability, catalyst, risk, and swing score calculations. Keep `engine/scorer.py` as a compatibility adapter that writes existing persisted fields while setting `composite_score` from the new swing model. Update `engine/recommendations.py` to use the new signal gates and clearer reasons.

**Tech Stack:** Python 3.13, dataclasses, pytest, existing FastAPI/Jinja2/SQLite app.

---

## File Map

- Create: `engine/signals.py`
  - Owns score primitives, risk/catalyst classification, and `SwingSignal` output.
- Create: `tests/engine/test_signals.py`
  - Unit tests for the signal model.
- Modify: `engine/scorer.py`
  - Delegates scoring to `engine.signals` and keeps legacy fields populated.
- Modify: `tests/engine/test_scorer.py`
  - Updates expectations for swing-score compatibility.
- Modify: `engine/recommendations.py`
  - Uses signal output and gates for buy/add/trim/sell decisions.
- Modify: `tests/engine/test_recommendations.py`
  - Covers new buy/add/sell/trim behavior with flow, catalyst, tradability, and risk.
- Modify: `web/routes.py`
  - Small wording/data glue only if needed for detail-page swing reasons.
- Modify: `TODOS.md`
  - Adds backtesting/calibration follow-up.

## Task 1: Add Signal Model Tests

**Files:**
- Create: `tests/engine/test_signals.py`

- [ ] **Step 1: Write failing tests**

Create `tests/engine/test_signals.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from engine.signals import (
    MODERATE_RISK_ALERTS,
    SEVERE_RISK_ALERTS,
    SwingSignal,
    compute_catalyst_score,
    compute_flow_score,
    compute_relative_value_scores,
    compute_risk_penalty,
    compute_swing_signal,
    compute_tradability_score,
)
from models import SubnetSnapshot


def make_snap(netuid: int = 1, **overrides) -> SubnetSnapshot:
    data = {
        "netuid": netuid,
        "polled_at": datetime.now(timezone.utc),
        "alpha_mcap_tao": 1_000.0,
        "alpha_mcap_usd": 300_000.0,
        "tao_in_tao": 1_000.0,
        "daily_emission_tao": 10.0,
        "tao_usd_price": 300.0,
        "emission_rank": 10,
        "volume_24h_alpha": 50_000.0,
        "alpha_price_tao": 0.002,
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def history_flow(values: list[float], *, hours_step: int = 6) -> list[SubnetSnapshot]:
    now = datetime.now(timezone.utc)
    rows = []
    for i, value in enumerate(values, start=1):
        rows.append(
            make_snap(
                polled_at=now - timedelta(hours=i * hours_step),
                net_tao_flow_tao=value,
                alpha_mcap_tao=1_000.0,
                emission_rank=12,
            )
        )
    return rows


def test_flow_score_rewards_persistent_recent_inflow():
    current = make_snap(emission_rank=8)
    positive = compute_flow_score(current, history_flow([20.0, 15.0, 10.0, 8.0]))
    flat = compute_flow_score(current, history_flow([0.0, 0.0, 0.0, 0.0]))

    assert positive.score > flat.score
    assert positive.is_positive
    assert "positive net TAO flow" in positive.reasons


def test_flow_score_penalizes_negative_flow_faster_than_positive():
    current = make_snap()
    positive = compute_flow_score(current, history_flow([20.0, 20.0]))
    negative = compute_flow_score(current, history_flow([-20.0, -20.0]))

    assert positive.score - 50.0 < 50.0 - negative.score
    assert negative.is_negative
    assert "sustained net TAO outflow" in negative.risks


def test_relative_value_scores_reward_emission_discount():
    cheap = make_snap(netuid=1, daily_emission_tao=20.0, alpha_mcap_usd=300_000.0, emission_rank=5)
    rich = make_snap(netuid=2, daily_emission_tao=5.0, alpha_mcap_usd=3_000_000.0, emission_rank=40)

    scores = compute_relative_value_scores([cheap, rich])

    assert scores[1].score > scores[2].score
    assert "cheap emissions versus market cap" in scores[1].reasons


def test_tradability_score_blocks_illiquid_subnet():
    liquid = compute_tradability_score(make_snap(volume_24h_alpha=50_000.0, alpha_price_tao=0.002, alpha_mcap_tao=1_000.0))
    illiquid = compute_tradability_score(make_snap(volume_24h_alpha=1.0, alpha_price_tao=0.001, alpha_mcap_tao=100_000.0))

    assert liquid.score > illiquid.score
    assert illiquid.blocks_new_buy
    assert "liquidity below swing threshold" in illiquid.risks


def test_catalyst_score_weights_convergence_highest():
    score = compute_catalyst_score({"convergence", "analyst_mention", "github_spike"}, covered=True, has_milestone=False)

    assert score.score >= 80.0
    assert score.is_strong
    assert "fresh convergence catalyst" in score.reasons


def test_risk_penalty_severe_risk_blocks_new_exposure():
    penalty = compute_risk_penalty({"liquidity_floor", "tao_outflow"}, flow_negative=True)

    assert penalty.penalty >= 40.0
    assert penalty.has_severe_risk
    assert "severe liquidity/emission risk" in penalty.risks


def test_swing_signal_high_yield_cannot_overcome_negative_flow_and_risk():
    current = make_snap(daily_emission_tao=200.0, alpha_mcap_usd=300_000.0, emission_rank=2)
    relative_scores = compute_relative_value_scores([current])
    signal = compute_swing_signal(
        current,
        history_flow([-40.0, -30.0, -20.0]),
        relative_scores[current.netuid],
        {"tao_outflow", "dead_github"},
        covered=False,
        has_milestone=False,
    )

    assert isinstance(signal, SwingSignal)
    assert signal.swing_score < 60.0
    assert signal.flow.is_negative
    assert signal.risk.penalty > 0
```

- [ ] **Step 2: Run tests to verify RED**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'engine.signals'`.

## Task 2: Implement Signal Model

**Files:**
- Create: `engine/signals.py`

- [ ] **Step 1: Add implementation**

Create `engine/signals.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from models import SubnetSnapshot

SEVERE_RISK_ALERTS = {"emission_near_zero", "liquidity_floor"}
MODERATE_RISK_ALERTS = {
    "ownership_transfer",
    "hyperparameter_change",
    "tao_outflow",
    "dead_github",
}
CATALYST_ALERTS = {
    "convergence",
    "analyst_mention",
    "milestone",
    "github_spike",
    "whale_inflow",
}


@dataclass
class SignalComponent:
    score: Optional[float]
    reasons: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    is_positive: bool = False
    is_negative: bool = False
    is_strong: bool = False
    blocks_new_buy: bool = False


@dataclass
class RiskSignal:
    penalty: float
    risks: list[str] = field(default_factory=list)
    has_severe_risk: bool = False
    moderate_count: int = 0


@dataclass
class SwingSignal:
    netuid: int
    flow: SignalComponent
    relative_value: SignalComponent
    tradability: SignalComponent
    catalyst: SignalComponent
    risk: RiskSignal
    swing_score: float
    reasons: list[str]
    risks: list[str]


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _pool_size(snap: SubnetSnapshot) -> Optional[float]:
    for value in (snap.tao_in_tao, snap.alpha_mcap_tao):
        if value is not None and value > 0:
            return value
    return None


def _history_since(history: list[SubnetSnapshot], cutoff: datetime) -> list[SubnetSnapshot]:
    return [row for row in history if row.polled_at >= cutoff]


def _flow_rate(rows: list[SubnetSnapshot], pool: float) -> Optional[float]:
    flows = [row.net_tao_flow_tao for row in rows if row.net_tao_flow_tao is not None]
    if not flows:
        return None
    return sum(flows) / pool


def compute_flow_score(snap: SubnetSnapshot,
                       history: list[SubnetSnapshot]) -> SignalComponent:
    if not history:
        return SignalComponent(score=None, risks=["insufficient flow history"])

    now = snap.polled_at or datetime.now(timezone.utc)
    pool = _pool_size(snap)
    if pool is None:
        return SignalComponent(score=None, risks=["missing pool size"])

    rows_24h = _history_since(history, now - timedelta(hours=24))
    rows_7d = _history_since(history, now - timedelta(days=7))
    rate_24h = _flow_rate(rows_24h, pool)
    rate_7d = _flow_rate(rows_7d, pool)

    if rate_24h is None and rate_7d is None:
        return SignalComponent(score=None, risks=["missing net flow data"])

    def contribution(rate: Optional[float], scale: float) -> float:
        if rate is None:
            return 0.0
        if rate >= 0:
            return min(30.0, rate * scale)
        return max(-45.0, rate * scale * 1.5)

    score = 50.0
    score += 0.60 * contribution(rate_24h, 600.0)
    score += 0.30 * contribution(rate_7d, 300.0)

    rank_reason = None
    rank_delta = None
    ranked_history = [row for row in history if row.emission_rank is not None]
    if snap.emission_rank is not None and ranked_history:
        ref = ranked_history[-1]
        rank_delta = ref.emission_rank - snap.emission_rank
        score += 0.10 * max(-15.0, min(15.0, rank_delta * 3.0))
        if rank_delta > 0:
            rank_reason = "emission rank confirming flow"

    recent_rate = rate_24h if rate_24h is not None else rate_7d or 0.0
    reasons: list[str] = []
    risks: list[str] = []
    is_positive = recent_rate > 0
    is_negative = recent_rate < 0
    if is_positive:
        reasons.append("positive net TAO flow")
    if rank_reason:
        reasons.append(rank_reason)
    if is_negative:
        risks.append("sustained net TAO outflow")

    return SignalComponent(
        score=round(_clamp(score), 2),
        reasons=reasons,
        risks=risks,
        is_positive=is_positive,
        is_negative=is_negative,
        is_strong=score >= 70.0,
    )


def _raw_yield(snap: SubnetSnapshot) -> Optional[float]:
    if (snap.daily_emission_tao is None
            or snap.tao_usd_price is None
            or not snap.alpha_mcap_usd
            or snap.alpha_mcap_usd < config.YIELD_MIN_MCAP_USD):
        return None
    return (snap.daily_emission_tao * snap.tao_usd_price * 365) / snap.alpha_mcap_usd


def compute_relative_value_scores(
    snapshots: list[SubnetSnapshot],
) -> dict[int, SignalComponent]:
    yields = {snap.netuid: raw for snap in snapshots if (raw := _raw_yield(snap)) is not None}
    valid_mcap = [
        (snap.netuid, snap.alpha_mcap_tao)
        for snap in snapshots
        if snap.alpha_mcap_tao is not None
    ]
    valid_mcap.sort(key=lambda item: item[1], reverse=True)
    mcap_rank = {netuid: rank for rank, (netuid, _) in enumerate(valid_mcap, start=1)}

    min_yield = min(yields.values()) if yields else None
    max_yield = max(yields.values()) if yields else None
    result: dict[int, SignalComponent] = {}

    for snap in snapshots:
        score_parts: list[float] = []
        reasons: list[str] = []
        risks: list[str] = []

        raw = yields.get(snap.netuid)
        if raw is not None and min_yield is not None and max_yield is not None:
            if max_yield == min_yield:
                yield_score = 50.0
            else:
                yield_score = (raw - min_yield) / (max_yield - min_yield) * 100.0
            score_parts.append(yield_score)

        mc_rank = mcap_rank.get(snap.netuid)
        if snap.emission_rank is not None and mc_rank is not None and snap.emission_rank > 0:
            ratio = mc_rank / snap.emission_rank
            rank_score = _clamp(50.0 + (ratio - 1.0) * 35.0)
            score_parts.append(rank_score)
            if ratio >= 1.3:
                reasons.append("cheap emissions versus market cap")
            elif ratio <= 0.7:
                risks.append("rich market cap versus emissions")

        if not score_parts:
            result[snap.netuid] = SignalComponent(score=None, risks=["missing relative value data"])
            continue

        score = sum(score_parts) / len(score_parts)
        result[snap.netuid] = SignalComponent(
            score=round(_clamp(score), 2),
            reasons=reasons,
            risks=risks,
            is_positive=score >= 65.0,
            is_negative=score <= 35.0,
        )

    return result


def compute_tradability_score(snap: SubnetSnapshot) -> SignalComponent:
    if (snap.volume_24h_alpha is None
            or snap.alpha_price_tao is None
            or snap.alpha_mcap_tao is None
            or snap.alpha_mcap_tao <= 0):
        return SignalComponent(score=None, risks=["missing liquidity data"])

    turnover = (snap.volume_24h_alpha * snap.alpha_price_tao) / snap.alpha_mcap_tao
    if turnover < config.LIQUIDITY_FLOOR_RATIO:
        return SignalComponent(
            score=10.0,
            risks=["liquidity below swing threshold"],
            is_negative=True,
            blocks_new_buy=True,
        )
    if turnover >= 0.05:
        score = 95.0
    elif turnover >= 0.02:
        score = 80.0
    elif turnover >= 0.01:
        score = 65.0
    else:
        score = 45.0
    return SignalComponent(
        score=score,
        reasons=["tradable daily turnover"] if score >= 65.0 else [],
        risks=[] if score >= 45.0 else ["thin swing liquidity"],
        is_positive=score >= 65.0,
        is_negative=score < 45.0,
    )


def compute_catalyst_score(alert_types: set[str],
                           covered: bool,
                           has_milestone: bool) -> SignalComponent:
    score = 0.0
    reasons: list[str] = []
    if "convergence" in alert_types:
        score += 50.0
        reasons.append("fresh convergence catalyst")
    if "whale_inflow" in alert_types:
        score += 25.0
        reasons.append("large net inflow catalyst")
    if "analyst_mention" in alert_types or covered:
        score += 18.0
        reasons.append("fresh analyst coverage")
    if "milestone" in alert_types or has_milestone:
        score += 18.0
        reasons.append("fresh product/research milestone")
    if "github_spike" in alert_types:
        score += 8.0
        reasons.append("GitHub attention spike")

    score = _clamp(score)
    return SignalComponent(
        score=round(score, 2),
        reasons=reasons,
        is_positive=score > 0,
        is_strong=score >= 50.0,
    )


def compute_risk_penalty(alert_types: set[str], flow_negative: bool) -> RiskSignal:
    severe = SEVERE_RISK_ALERTS & alert_types
    moderate = MODERATE_RISK_ALERTS & alert_types
    penalty = 0.0
    risks: list[str] = []

    if severe:
        penalty += 45.0
        risks.append("severe liquidity/emission risk")
    if moderate:
        penalty += min(30.0, 12.0 * len(moderate))
        risks.append("multiple moderate risk alerts" if len(moderate) >= 2 else "moderate risk alert")
    if flow_negative:
        penalty += 15.0
        risks.append("negative flow risk")

    return RiskSignal(
        penalty=round(_clamp(penalty, 0.0, 70.0), 2),
        risks=risks,
        has_severe_risk=bool(severe),
        moderate_count=len(moderate),
    )


def compute_swing_signal(
    snap: SubnetSnapshot,
    history: list[SubnetSnapshot],
    relative_value: SignalComponent,
    alert_types: set[str],
    covered: bool,
    has_milestone: bool,
) -> SwingSignal:
    flow = compute_flow_score(snap, history)
    tradability = compute_tradability_score(snap)
    catalyst = compute_catalyst_score(alert_types, covered, has_milestone)
    risk = compute_risk_penalty(alert_types, flow.is_negative)

    weighted = [
        (flow.score, 0.40),
        (relative_value.score, 0.25),
        (tradability.score, 0.20),
        (catalyst.score, 0.15),
    ]
    available = [(score, weight) for score, weight in weighted if score is not None]
    if available:
        total_weight = sum(weight for _, weight in available)
        base = sum(score * weight for score, weight in available) / total_weight
    else:
        base = 0.0

    swing_score = round(_clamp(base - risk.penalty), 2)
    reasons = flow.reasons + relative_value.reasons + tradability.reasons + catalyst.reasons
    risks = flow.risks + relative_value.risks + tradability.risks + risk.risks

    return SwingSignal(
        netuid=snap.netuid,
        flow=flow,
        relative_value=relative_value,
        tradability=tradability,
        catalyst=catalyst,
        risk=risk,
        swing_score=swing_score,
        reasons=reasons,
        risks=risks,
    )
```

- [ ] **Step 2: Run signal tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py -q
```

Expected: PASS.

## Task 3: Adapt Scorer to Swing Signals

**Files:**
- Modify: `engine/scorer.py`
- Modify: `tests/engine/test_scorer.py`

- [ ] **Step 1: Write failing scorer compatibility tests**

Append to `tests/engine/test_scorer.py`:

```python
def test_score_snapshots_composite_is_swing_score_from_flow_value_and_tradability():
    now = datetime.now(timezone.utc)
    snap = make_snap(
        1,
        daily_emission_tao=50.0,
        alpha_mcap_usd=500_000.0,
        tao_usd_price=300.0,
        alpha_mcap_tao=1_000.0,
        tao_in_tao=1_000.0,
        volume_24h_alpha=50_000.0,
        alpha_price_tao=0.002,
        emission_rank=4,
    )
    hist = [
        make_snap(
            1,
            alpha_mcap_tao=1_000.0,
            tao_in_tao=1_000.0,
            net_tao_flow_tao=30.0,
            emission_rank=8,
        )
    ]
    hist[0].polled_at = now - timedelta(hours=4)

    score_snapshots([snap], history_by_netuid={1: hist})

    assert snap.composite_score is not None
    assert snap.composite_score > 60.0
    assert snap.momentum_score == snap.composite_score
```

- [ ] **Step 2: Run scorer test to verify RED**

Run:

```bash
.venv/bin/pytest tests/engine/test_scorer.py::test_score_snapshots_composite_is_swing_score_from_flow_value_and_tradability -q
```

Expected: FAIL because scorer still uses the old composite semantics.

- [ ] **Step 3: Update scorer adapter**

In `engine/scorer.py`:

- import `compute_flow_score`, `compute_relative_value_scores`, `compute_swing_signal`, `compute_tradability_score`
- keep `compute_yield_scores`, `compute_health_score`, `compute_momentum_score`, and `compute_hype_score` import-compatible for existing tests
- update `score_snapshots()` to:
  - compute relative value scores once
  - compute a swing signal per subnet with empty alert/catalyst context
  - set `yield_score = relative_value.score`
  - set `health_score = tradability.score`
  - set `momentum_score = swing_signal.swing_score`
  - set `composite_score = swing_signal.swing_score`

- [ ] **Step 4: Run scorer tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_scorer.py -q
```

Expected: PASS after updating existing expectations if needed.

## Task 4: Refactor Portfolio Recommendations

**Files:**
- Modify: `engine/recommendations.py`
- Modify: `tests/engine/test_recommendations.py`

- [ ] **Step 1: Add failing recommendation tests**

Append to `tests/engine/test_recommendations.py`:

```python
def test_new_buy_requires_positive_flow_or_strong_catalyst(monkeypatch):
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
        make_snapshot(
            netuid=14,
            name="Macro",
            category="AI Training",
            composite_score=90.0,
            momentum_score=45.0,
        ),
    ]

    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["new_candidates"] == []


def test_trim_on_weak_swing_score_and_outflow_risk(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_HOLD_FLOOR_SCORE", 55.0)
    positions = {
        3: {
            "netuid": 3,
            "subnet_name": "Templar",
            "category": "AI Training",
            "tao_value": 10.0,
            "allocation_pct": 0.10,
        }
    }
    snapshots = [make_snapshot(netuid=3, composite_score=48.0, momentum_score=35.0)]

    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={3: {"tao_outflow"}},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["table_actions"][3]["action"] == "trim"
    assert "swing score deteriorating with outflow risk" in result["table_actions"][3]["reasons"]
```

- [ ] **Step 2: Run recommendation tests to verify RED**

Run:

```bash
.venv/bin/pytest tests/engine/test_recommendations.py -q
```

Expected: FAIL on the new recommendation behavior.

- [ ] **Step 3: Update recommendation gates**

In `engine/recommendations.py`:

- rename local score reads to `swing_score = snapshot.get("composite_score") or 0.0`
- make `_has_positive_catalyst()` treat `momentum_score >= 70` as positive flow confirmation
- add `_has_positive_confirmation(snapshot, alert_types, covered, has_milestone)` requiring catalyst or `momentum_score >= 70`
- add held trim rule before default hold:

```python
if (
    score < config.PORTFOLIO_HOLD_FLOOR_SCORE
    and ("tao_outflow" in alert_types or (snapshot.get("momentum_score") or 0.0) < 40.0)
):
    card = _card(
        snapshot,
        "trim",
        "medium",
        ["swing score deteriorating with outflow risk"],
        position["allocation_pct"],
    )
```

- require positive confirmation for `new_buy`
- preserve severe sell behavior.

- [ ] **Step 4: Run recommendation tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_recommendations.py -q
```

Expected: PASS.

## Task 5: Update Detail Wording and TODO

**Files:**
- Modify: `web/routes.py`
- Modify: `TODOS.md`

- [ ] **Step 1: Update wording**

In `web/routes.py`, adjust comment/verdict strings only where they refer to generic momentum. Keep route behavior stable.

Replace:

```python
# Single synthesised call for the investor: entry / caution / exit / risk
```

with:

```python
# Single 1-2 week swing call for the investor: entry / caution / exit / risk
```

- [ ] **Step 2: Add calibration follow-up**

Add this section near the top of `TODOS.md`:

```markdown
## P1 — Calibration Follow-up

### Backtest swing signals over 7d/14d forward windows
**What:** Replay historical snapshots and measure whether high swing scores, catalysts,
sell/trim alerts, and flow reversals predict 7d/14d forward TAO returns.

**Why:** The signal refactor encodes the current Taoflow thesis, but thresholds still need
empirical calibration before recommendations should be treated as decision-grade.

**Effort:** M
**Priority:** P1
```

- [ ] **Step 3: Run web and route tests**

Run:

```bash
.venv/bin/pytest tests/web tests/test_main.py -q
```

Expected: PASS.

## Task 6: Full Verification and Commit

**Files:**
- All changed files

- [ ] **Step 1: Run full test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: PASS.

- [ ] **Step 2: Review git diff**

Run:

```bash
git diff -- engine/signals.py engine/scorer.py engine/recommendations.py tests/engine/test_signals.py tests/engine/test_scorer.py tests/engine/test_recommendations.py web/routes.py TODOS.md
```

Expected: Diff only contains the swing signal refactor and TODO follow-up.

- [ ] **Step 3: Commit**

Run:

```bash
git add engine/signals.py engine/scorer.py engine/recommendations.py tests/engine/test_signals.py tests/engine/test_scorer.py tests/engine/test_recommendations.py web/routes.py TODOS.md docs/superpowers/plans/2026-04-23-tao-swing-signal-refactor.md
git commit -m "feat: refactor tao swing signals"
```

Expected: commit created.
