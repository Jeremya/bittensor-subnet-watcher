# Portfolio Action Rationale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add concrete decision rationale context to portfolio recommendation cards and table recommendations.

**Architecture:** Keep recommendation policy unchanged. Extend `engine/recommendations.py` so each card includes a `context` list derived from existing inputs, then render that context in `web/templates/portfolio.html`.

**Tech Stack:** Python 3.13, pytest, FastAPI/Jinja2, existing recommendation engine.

---

### Task 1: Add Recommendation Context Builder

**Files:**
- Modify: `engine/recommendations.py`
- Test: `tests/engine/test_recommendations.py`

- [ ] **Step 1: Write failing tests for context generation**

Add tests that verify sell cards include risk context and trim cards include allocation context:

```python
def test_sell_recommendation_includes_risk_context(monkeypatch):
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

    context = result["portfolio_actions"][0]["context"]
    assert {"label": "Risk", "value": "liquidity_floor, tao_outflow", "tone": "danger"} in context
    assert {"label": "Allocation", "value": "30.0% of portfolio", "tone": "neutral"} in context
    assert {"label": "Context", "value": "no recent analyst coverage or milestone", "tone": "muted"} in context
```

```python
def test_trim_recommendation_includes_allocation_context(monkeypatch):
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
        coverage_netuids={3},
        milestone_netuids=set(),
    )

    context = result["table_actions"][3]["context"]
    assert {"label": "Allocation", "value": "34.0% of portfolio", "tone": "warning"} in context
    assert {"label": "Score", "value": "81.0 swing", "tone": "positive"} in context
    assert {"label": "Context", "value": "analyst coverage active", "tone": "positive"} in context
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/pytest tests/engine/test_recommendations.py::test_sell_recommendation_includes_risk_context tests/engine/test_recommendations.py::test_trim_recommendation_includes_allocation_context -q
```

Expected: FAIL because recommendation cards do not include `context`.

- [ ] **Step 3: Implement context helper**

Add a helper that returns context rows:

```python
def _context_rows(snapshot, action, alert_types, allocation_pct, has_coverage, has_milestone):
    rows = []
    if alert_types:
        rows.append({"label": "Risk", "value": ", ".join(sorted(alert_types)), "tone": "danger"})
    if allocation_pct is not None:
        rows.append({
            "label": "Allocation",
            "value": f"{allocation_pct * 100:.1f}% of portfolio",
            "tone": "warning" if action == "trim" else "neutral",
        })
    score = _score(snapshot)
    if score is not None:
        rows.append({
            "label": "Score",
            "value": f"{score:.1f} swing",
            "tone": "danger" if score < 55 else "positive" if score >= 75 else "neutral",
        })
    if has_coverage or has_milestone:
        bits = []
        if has_coverage:
            bits.append("analyst coverage active")
        if has_milestone:
            bits.append("recent milestone")
        rows.append({"label": "Context", "value": " + ".join(bits), "tone": "positive"})
    elif action in ("sell", "trim"):
        rows.append({"label": "Context", "value": "no recent analyst coverage or milestone", "tone": "muted"})
    return rows
```

Update `_card()` to accept `context` and include it in the returned dict.

- [ ] **Step 4: Wire helper into held and candidate card creation**

In `build_portfolio_recommendations()`, pass alert types, allocation, coverage, and milestone state into `_context_rows()` for each held position. For new candidates, use `allocation_pct=None` and the candidate coverage/milestone state.

- [ ] **Step 5: Run focused recommendation tests**

Run:

```bash
.venv/bin/pytest tests/engine/test_recommendations.py -q
```

Expected: PASS.

### Task 2: Render Context in Portfolio Template

**Files:**
- Modify: `web/templates/portfolio.html`
- Test: `tests/web/test_portfolio_route.py`

- [ ] **Step 1: Write failing route tests**

Extend the mocked recommendation payload in `client_with_portfolio()` so a card and table action include:

```python
"context": [
    {"label": "Risk", "value": "tao_outflow", "tone": "danger"},
    {"label": "Allocation", "value": "60.0% of portfolio", "tone": "warning"},
],
```

Add:

```python
def test_portfolio_renders_action_context(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Risk" in resp.text
    assert "tao_outflow" in resp.text
    assert "Allocation" in resp.text
    assert "60.0% of portfolio" in resp.text
```

Add:

```python
def test_portfolio_table_recommendation_prefers_context(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Allocation: 60.0% of portfolio" in resp.text
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
.venv/bin/pytest tests/web/test_portfolio_route.py::test_portfolio_renders_action_context tests/web/test_portfolio_route.py::test_portfolio_table_recommendation_prefers_context -q
```

Expected: FAIL because the template does not render context.

- [ ] **Step 3: Add compact context styles and markup**

In `web/templates/portfolio.html`, add compact `.context-list`, `.context-item`, `.context-label`, and tone classes. Render `rec.context` between the action header and `rec.reasons`.

For table recommendations, render the first context item as `Label: value`, falling back to the first reason.

- [ ] **Step 4: Run route tests**

Run:

```bash
.venv/bin/pytest tests/web/test_portfolio_route.py -q
```

Expected: PASS.

### Task 3: Verify and Commit

**Files:**
- Verify all modified files.

- [ ] **Step 1: Run full test suite**

Run:

```bash
.venv/bin/pytest -q
```

Expected: PASS with existing warnings only.

- [ ] **Step 2: Inspect diff**

Run:

```bash
git diff -- engine/recommendations.py web/templates/portfolio.html tests/engine/test_recommendations.py tests/web/test_portfolio_route.py
```

Expected: Diff is limited to recommendation context generation, rendering, and tests.

- [ ] **Step 3: Commit implementation**

Run:

```bash
git add engine/recommendations.py web/templates/portfolio.html tests/engine/test_recommendations.py tests/web/test_portfolio_route.py docs/superpowers/plans/2026-07-01-portfolio-action-rationale.md
git commit -m "feat: add portfolio action rationale context"
```
