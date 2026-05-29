# TAO Signal Reliability and Policy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the TAO swing system decision-grade by wiring real catalyst/risk context into scoring, persisting explicit signal outputs, adding slippage-based tradability, backtesting the model, and unifying dashboard and portfolio policy.

**Architecture:** Keep the current flow-first thesis, but stop treating the scoreboard as a single opaque number. Introduce explicit signal fields and a shared policy layer so the dashboard, subnet detail page, and portfolio page all explain the same swing thesis. Add a backtest path over existing snapshots so thresholds can be calibrated against forward 7d/14d outcomes before more signals are added.

**Tech Stack:** Python 3.13, FastAPI, Jinja2, SQLite/aiosqlite, pytest, existing bittensor SDK integration.

---

## File Map

- Modify: `engine/signals.py`
  - Accept catalyst/risk context, add explicit signal fields, add slippage-aware tradability hooks.
- Modify: `engine/scorer.py`
  - Pass alert context into swing scoring and persist explicit signal outputs.
- Modify: `engine/recommendations.py`
  - Consume the explicit swing/policy outputs instead of interpreting `composite_score` ad hoc.
- Modify: `db/database.py`
  - Add signal columns and any helper queries needed for backtests and freshness metrics.
- Modify: `collectors/chain.py`
  - Capture the reserve/price data needed to estimate slippage and swing exit cost.
- Modify: `web/routes.py`
  - Replace duplicate verdict logic with a shared policy helper and add data-quality metrics.
- Modify: `web/templates/index.html`, `web/templates/subnet.html`, `web/templates/portfolio.html`
  - Show the same signal explanations and freshness indicators everywhere.
- Create: `engine/backtest.py`
  - Backtest score buckets, alert types, and recommendations against forward returns.
- Create: `tests/engine/test_signals.py`
  - Expand signal tests to cover catalyst/risk context and slippage-driven tradability.
- Create: `tests/engine/test_backtest.py`
  - Cover backtest output shape and forward-return calculations.
- Modify: `tests/engine/test_scorer.py`
  - Verify scoring uses catalyst/risk context and persists explicit signals.
- Modify: `tests/engine/test_recommendations.py`
  - Verify policy uses the same swing signals as the dashboard.
- Modify: `tests/web/test_routes.py`, `tests/web/test_portfolio_route.py`
  - Verify UI shows the new signal explanations and freshness state.

## Task 1: Wire Real Catalyst and Risk Context Into Scoring

**Files:**
- Modify: `engine/signals.py`
- Modify: `engine/scorer.py`
- Modify: `tests/engine/test_signals.py`
- Modify: `tests/engine/test_scorer.py`

- [ ] **Step 1: Write failing tests for context-aware swing scoring**

Add tests that prove the score changes when recent alerts and active coverage are present.

```python
def test_swing_score_uses_catalyst_and_risk_context():
    current = make_snap(emission_rank=4)
    history = history_flow([20.0, 10.0, 5.0])
    relative = compute_relative_value_scores([current])[current.netuid]

    neutral = compute_swing_signal(
        current,
        history,
        relative,
        alert_types=set(),
        covered=False,
        has_milestone=False,
    )
    catalyzed = compute_swing_signal(
        current,
        history,
        relative,
        alert_types={"convergence", "analyst_mention"},
        covered=True,
        has_milestone=True,
    )

    assert catalyzed.swing_score > neutral.swing_score
    assert "fresh convergence catalyst" in catalyzed.reasons
```

Add a second test proving risk lowers the score:

```python
def test_swing_score_penalizes_recent_outflow_and_liquidity_risk():
    current = make_snap(volume_24h_alpha=1.0, alpha_price_tao=0.001, alpha_mcap_tao=100_000.0)
    history = history_flow([-30.0, -20.0, -10.0])
    relative = compute_relative_value_scores([current])[current.netuid]

    signal = compute_swing_signal(
        current,
        history,
        relative,
        alert_types={"tao_outflow", "liquidity_floor"},
        covered=False,
        has_milestone=False,
    )

    assert signal.swing_score < 50.0
    assert signal.risk.has_severe_risk
    assert signal.tradability.blocks_new_buy
```

- [ ] **Step 2: Run the tests to verify they fail for the current implementation**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py tests/engine/test_scorer.py -q
```

Expected: failures showing that catalyst/risk context is not yet wired through the scoring path.

- [ ] **Step 3: Update the scorer adapter to pass real context**

Update `engine/scorer.py` so `score_snapshots()` accepts:

```python
def score_snapshots(
    snapshots: list[SubnetSnapshot],
    history_by_netuid: dict[int, list[SubnetSnapshot]],
    alert_types_by_netuid: dict[int, set[str]] | None = None,
    coverage_netuids: set[int] | None = None,
    milestone_netuids: set[int] | None = None,
    owner_changes_by_netuid: Optional[dict[int, int]] = None,
    reg_cost_7d_by_netuid: Optional[dict[int, Optional[float]]] = None,
) -> None:
```

Then pass those values into `compute_swing_signal()` instead of empty placeholders.

- [ ] **Step 4: Update the tests to assert the new path is used**

Update `tests/engine/test_scorer.py` so the swing score changes when alert context changes. Keep the compatibility checks for `yield_score`, `health_score`, `momentum_score`, and `composite_score`, but make them assert the new explicit signal semantics rather than the old opaque composite behavior.

- [ ] **Step 5: Re-run the signal and scorer tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py tests/engine/test_scorer.py -q
```

Expected: PASS.

## Task 2: Persist Explicit Signal Outputs

**Files:**
- Modify: `db/database.py`
- Modify: `models.py`
- Modify: `engine/scorer.py`
- Modify: `tests/test_db_schema.py`
- Modify: `tests/engine/test_scorer.py`

- [ ] **Step 1: Write failing schema and persistence tests**

Add a schema test that checks the snapshots table has explicit signal columns:

```python
@pytest.mark.asyncio
async def test_snapshots_schema_includes_explicit_signal_columns(db):
    cursor = await db.execute("PRAGMA table_info(snapshots)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert {"flow_score", "relative_value_score", "tradability_score", "catalyst_score", "risk_penalty", "swing_score"} <= cols
```

Add a persistence test that inserts a scored snapshot and reads those fields back.

- [ ] **Step 2: Add the new columns to the snapshots schema**

Update `SCHEMA_SQL` and the migration path in `db/database.py` so each snapshot stores:

```sql
flow_score REAL
relative_value_score REAL
tradability_score REAL
catalyst_score REAL
risk_penalty REAL
swing_score REAL
```

If `models.py` is used to represent persisted signal output in memory, add a small dataclass or extend `SubnetSnapshot` with those fields so the write path stays explicit.

- [ ] **Step 3: Write signal outputs during scoring**

Update `engine/scorer.py` so the new explicit signal fields are set on each `SubnetSnapshot` before insert:

```python
snap.flow_score = swing.flow.score
snap.relative_value_score = swing.relative_value.score
snap.tradability_score = swing.tradability.score
snap.catalyst_score = swing.catalyst.score
snap.risk_penalty = swing.risk.penalty
snap.swing_score = swing.swing_score
snap.composite_score = swing.swing_score
```

- [ ] **Step 4: Re-run schema and scorer tests**

Run:

```bash
.venv/bin/pytest tests/test_db_schema.py tests/engine/test_scorer.py -q
```

Expected: PASS.

## Task 3: Add Slippage-Based Tradability

**Files:**
- Modify: `collectors/chain.py`
- Modify: `models.py`
- Modify: `engine/signals.py`
- Modify: `tests/engine/test_signals.py`

- [ ] **Step 1: Write failing tradability tests for slippage**

Add tests that prove tradability uses estimated exit cost, not just turnover:

```python
def test_tradability_penalizes_high_slippage_for_large_positions():
    snap = make_snap(
        volume_24h_alpha=10_000.0,
        alpha_price_tao=0.002,
        alpha_mcap_tao=1_000.0,
    )
    snap.alpha_out_tao = 20.0
    snap.alpha_in_tao = 20.0

    signal = compute_tradability_score(snap)

    assert signal.score is not None
    assert signal.score < 80.0
```

Add a second test that protects tiny slippage estimates from being over-penalized.

- [ ] **Step 2: Capture the raw reserve fields in chain collection**

Update `collectors/chain.py` to store the raw pool reserves separately, not only `alpha_mcap_tao`:

```python
alpha_in_tao = dyn.alpha_in.tao
alpha_out_tao = dyn.alpha_out.tao
```

Add those fields to `SubnetSnapshot` and persist them in SQLite.

- [ ] **Step 3: Update tradability scoring to estimate slippage**

In `engine/signals.py`, use the reserve fields to estimate exit cost for a configurable trade size. Keep turnover as a fallback, but let a large expected TAO exit cost lower the score even when 24h volume looks fine.

If the SDK slippage helpers are available in the current bittensor version, prefer them; otherwise compute a conservative reserve-based proxy and label it as such in the returned reasons.

- [ ] **Step 4: Re-run signal tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_signals.py -q
```

Expected: PASS.

## Task 4: Unify Dashboard and Portfolio Policy

**Files:**
- Create: `engine/policy.py`
- Modify: `web/routes.py`
- Modify: `engine/recommendations.py`
- Modify: `tests/engine/test_recommendations.py`
- Modify: `tests/web/test_routes.py`
- Modify: `tests/web/test_portfolio_route.py`

- [ ] **Step 1: Write failing policy tests**

Add tests that prove the same swing policy returns the same action/reasoning when called from the dashboard path and the portfolio path.

```python
from engine.policy import action_for_position, verdict_for_subnet
from engine.signals import RiskSignal, SignalComponent, SwingSignal

def make_signal(swing_score=82.0, flow_positive=True, catalyst_strong=True):
    signal = SwingSignal(
        netuid=3,
        flow=SignalComponent(score=82.0, reasons=["positive net TAO flow"], is_positive=True),
        relative_value=SignalComponent(score=74.0, reasons=["cheap emissions versus market cap"], is_positive=True),
        tradability=SignalComponent(score=88.0, reasons=["tradable daily turnover"], is_positive=True),
        catalyst=SignalComponent(score=76.0, reasons=["fresh convergence catalyst"], is_positive=True, is_strong=True),
        risk=RiskSignal(penalty=0.0),
        swing_score=swing_score,
        reasons=[],
        risks=[],
    )
    signal.flow.is_positive = flow_positive
    signal.catalyst.is_strong = catalyst_strong
    return signal

def test_policy_returns_same_verdict_for_dashboard_and_portfolio():
    signal = make_signal()
    assert verdict_for_subnet(signal) == "Entry signal"
    assert action_for_position(signal) == "add"
```

- [ ] **Step 2: Extract shared policy logic**

Move the decision tree from `web/routes.py` and `engine/recommendations.py` into `engine/policy.py`.

The shared module should expose at least:

```python
def verdict_for_subnet(signal: SwingSignal) -> str: ...
def action_for_position(signal: SwingSignal, allocation_pct: float) -> dict[str, Any]: ...
```

- [ ] **Step 3: Update the dashboard and portfolio routes to consume policy outputs**

Replace the duplicated if/elif verdict logic in `web/routes.py` with `engine.policy`.

Use the same policy helper in `engine/recommendations.py` for add/trim/sell/new buy decisions so the portfolio page and subnet detail page tell the same story.

- [ ] **Step 4: Re-run route and recommendation tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_recommendations.py tests/web/test_routes.py tests/web/test_portfolio_route.py -q
```

Expected: PASS.

## Task 5: Add Backtesting and Calibration

**Files:**
- Create: `engine/backtest.py`
- Create: `tests/engine/test_backtest.py`
- Modify: `TODOS.md`
- Optional: `scripts/backtest_signals.py`

- [ ] **Step 1: Write failing backtest tests**

Add tests for a small synthetic history fixture:

```python
def test_backtest_emits_7d_and_14d_bucket_metrics():
    report = run_backtest(rows)
    assert report["bucket_90"]["count"] > 0
    assert "forward_7d_return" in report["bucket_90"]
    assert "forward_14d_return" in report["bucket_90"]
```

- [ ] **Step 2: Implement a backtest engine over stored snapshots**

Create `engine/backtest.py` to:
- bucket snapshots by `swing_score`
- compute forward 7d and 14d TAO return using later snapshots
- compare returns for held names, new-buy candidates, and sell/trim names
- export a compact dict or JSON report for local review

Prefer a pure function API first so it can be tested without the CLI.

- [ ] **Step 3: Add a runnable script or command**

If useful, add `scripts/backtest_signals.py` that prints a short summary table and writes a JSON file with the backtest output.

- [ ] **Step 4: Update the calibration TODO**

Document that threshold tuning must happen only after at least one backtest run on historical data.

- [ ] **Step 5: Re-run backtest tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_backtest.py -q
```

Expected: PASS.

## Task 6: Surface Data Quality and Freshness

**Files:**
- Modify: `web/routes.py`
- Modify: `web/templates/index.html`
- Modify: `web/templates/subnet.html`
- Modify: `tests/web/test_routes.py`

- [ ] **Step 1: Write failing UI tests for freshness and coverage**

Add tests that check the dashboard renders:
- last chain poll age
- number of subnets with stale or missing flow data
- GitHub coverage or analyst coverage counts

- [ ] **Step 2: Add helper queries for freshness**

In `db/database.py`, add a helper that returns:
- last successful poll per collector
- count of snapshots missing `net_tao_flow_tao`
- count of subnets missing GitHub/X/milestone coverage

- [ ] **Step 3: Render the data-quality panel**

Update the dashboard template to show a compact operational panel above the leaderboard or alongside the alert feed. Keep it utilitarian, not decorative.

- [ ] **Step 4: Re-run the web tests**

Run:

```bash
.venv/bin/pytest tests/web/test_routes.py -q
```

Expected: PASS.

## Task 7: Full Verification and Commit

**Files:**
- All changed files

- [ ] **Step 1: Run the full test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: PASS.

- [ ] **Step 2: Review the diff**

Run:

```bash
git diff -- engine/signals.py engine/scorer.py engine/recommendations.py engine/backtest.py db/database.py collectors/chain.py web/routes.py web/templates/index.html web/templates/subnet.html web/templates/portfolio.html TODOS.md models.py tests/engine/test_signals.py tests/engine/test_scorer.py tests/engine/test_recommendations.py tests/engine/test_backtest.py tests/web/test_routes.py tests/web/test_portfolio_route.py tests/test_db_schema.py
```

Expected: the diff should only contain the reliability, policy, and calibration work described in this plan.

- [ ] **Step 3: Commit**

Run:

```bash
git add engine/signals.py engine/scorer.py engine/recommendations.py engine/backtest.py db/database.py collectors/chain.py web/routes.py web/templates/index.html web/templates/subnet.html web/templates/portfolio.html TODOS.md models.py tests/engine/test_signals.py tests/engine/test_scorer.py tests/engine/test_recommendations.py tests/engine/test_backtest.py tests/web/test_routes.py tests/web/test_portfolio_route.py tests/test_db_schema.py docs/superpowers/plans/2026-04-29-tao-signal-reliability-plan.md
git commit -m "feat: harden tao signal reliability"
```

Expected: commit created.
