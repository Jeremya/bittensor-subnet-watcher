# Ownership Transfer Alert Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `check_ownership_transfer` alert that fires when a subnet's owner coldkey changes between polls, completing alert #7 of the original 8-alert spec.

**Architecture:** Add one pure function `check_ownership_transfer(current, prev)` to `engine/alerts.py` following the exact same pattern as `check_emission_drop`. Wire it into `evaluate_alerts()` alongside the other prev-requiring checks. No new config, no schema changes — `owner_coldkey` is already stored in every snapshot row.

**Tech Stack:** Python 3.12, aiosqlite, pytest with `asyncio_mode = auto` (no `@pytest.mark.asyncio` needed), existing `models.SubnetSnapshot` / `models.AlertRecord`

---

## File Map

| File | Change |
|------|--------|
| `engine/alerts.py` | Add `check_ownership_transfer` function; wire into `evaluate_alerts`; update docstring count 6→7 |
| `tests/engine/test_alerts.py` | Add 4 unit tests; add `check_ownership_transfer` to import |
| `TODOS.md` | Split P1 item: mark ownership transfer done (will be done), leave whale inflow as separate pending item |

---

### Task 1: Add `check_ownership_transfer` with tests

**Files:**
- Modify: `engine/alerts.py`
- Modify: `tests/engine/test_alerts.py`

- [ ] **Step 1: Write the four failing tests**

Open `tests/engine/test_alerts.py`. Add `check_ownership_transfer` to the existing import at the top:

```python
from engine.alerts import (
    check_emission_divergence,
    check_dead_github,
    check_emission_drop,
    check_github_spike,
    check_social_silence,
    check_new_entry,
    check_ownership_transfer,   # ← add this
    evaluate_alerts,
)
```

Then append these four tests after `test_new_entry_does_not_fire_for_known` and before the `evaluate_alerts` integration test:

```python
# ── check_ownership_transfer ──────────────────────────────────────────────────

def test_ownership_transfer_fires_on_coldkey_change():
    prev = make_snap(1, owner_coldkey="5ABC123def456789abc")
    curr = make_snap(1, owner_coldkey="5XYZ987uvw654321xyz")
    result = check_ownership_transfer(curr, prev)
    assert result is not None
    assert result.alert_type == "ownership_transfer"
    assert "5ABC123d" in result.description
    assert "5XYZ987u" in result.description


def test_ownership_transfer_no_alert_when_same():
    coldkey = "5ABC123def456789abc"
    prev = make_snap(1, owner_coldkey=coldkey)
    curr = make_snap(1, owner_coldkey=coldkey)
    assert check_ownership_transfer(curr, prev) is None


def test_ownership_transfer_no_alert_when_prev_none():
    """Prevents false alerts on first snapshot after field was populated."""
    curr = make_snap(1, owner_coldkey="5ABC123def456789abc")
    prev = make_snap(1, owner_coldkey=None)
    assert check_ownership_transfer(curr, prev) is None


def test_ownership_transfer_no_alert_when_current_none():
    """Prevents false alerts when chain temporarily omits owner_coldkey."""
    prev = make_snap(1, owner_coldkey="5ABC123def456789abc")
    curr = make_snap(1, owner_coldkey=None)
    assert check_ownership_transfer(curr, prev) is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jeremy/dev/tao-subnet-investigation
pytest tests/engine/test_alerts.py::test_ownership_transfer_fires_on_coldkey_change -v
```

Expected: `FAILED` — `ImportError: cannot import name 'check_ownership_transfer'`

- [ ] **Step 3: Implement `check_ownership_transfer` in `engine/alerts.py`**

Add this function after `check_new_entry` (before `evaluate_alerts`). The existing `check_emission_drop` function is at line ~51 — use it as the style reference:

```python
def check_ownership_transfer(current: SubnetSnapshot,
                              prev: SubnetSnapshot) -> Optional[AlertRecord]:
    if current.owner_coldkey is None or prev.owner_coldkey is None:
        return None
    if current.owner_coldkey == prev.owner_coldkey:
        return None
    return AlertRecord(
        fired_at=datetime.now(timezone.utc),
        netuid=current.netuid,
        subnet_name=f"SN{current.netuid}",
        alert_type="ownership_transfer",
        description=(
            f"Owner changed: {prev.owner_coldkey[:8]}… → {current.owner_coldkey[:8]}…"
        ),
        current_value=None,
        threshold=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/engine/test_alerts.py -v -k "ownership_transfer"
```

Expected: all 4 tests `PASSED`

- [ ] **Step 5: Run full alert test suite to confirm no regressions**

```bash
pytest tests/engine/test_alerts.py -v
```

Expected: all tests `PASSED` (was 10 tests, now 14)

- [ ] **Step 6: Commit**

```bash
git add engine/alerts.py tests/engine/test_alerts.py
git commit -m "feat: add check_ownership_transfer alert function with tests"
```

---

### Task 2: Wire `check_ownership_transfer` into `evaluate_alerts`

**Files:**
- Modify: `engine/alerts.py` (the `evaluate_alerts` function, lines ~130–195)

- [ ] **Step 1: Write a failing integration test**

In `tests/engine/test_alerts.py`, append this test after `test_evaluate_alerts_respects_cooldown`:

```python
async def test_evaluate_alerts_fires_ownership_transfer(db):
    snap = make_snap(
        1,
        owner_coldkey="5XYZnewowner456789xyz",
        emission_rank=5,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
    )
    prev_snap = make_snap(1, owner_coldkey="5ABColdowner123abc")
    registry = {1: {"name": "TestNet", "x_handle": None, "github_url": None}}
    known_netuids = {1}

    alerts = await evaluate_alerts(
        db, [snap], registry, {1: prev_snap}, known_netuids
    )
    transfer_alerts = [a for a in alerts if a.alert_type == "ownership_transfer"]
    assert len(transfer_alerts) == 1
    assert transfer_alerts[0].netuid == 1
```

- [ ] **Step 2: Run test to verify it fails**

```bash
pytest tests/engine/test_alerts.py::test_evaluate_alerts_fires_ownership_transfer -v
```

Expected: `FAILED` — `AssertionError: assert 0 == 1` (function exists but isn't wired yet)

- [ ] **Step 3: Wire into `evaluate_alerts` in `engine/alerts.py`**

Find the section in `evaluate_alerts` that handles prev-dependent checks. It currently looks like this:

```python
        # 3. Emission drop (requires prev snapshot)
        if prev:
            candidates.append(check_emission_drop(snap, prev))

        # 4. GitHub spike (requires prev)
        if prev:
            candidates.append(check_github_spike(snap, prev))

        # 5. Social silence
        candidates.append(check_social_silence(snap))

        # 6. New entry
        candidates.append(check_new_entry(snap, known_netuids))
```

Replace it with:

```python
        # 3. Emission drop (requires prev snapshot)
        if prev:
            candidates.append(check_emission_drop(snap, prev))

        # 4. GitHub spike (requires prev)
        if prev:
            candidates.append(check_github_spike(snap, prev))

        # 5. Ownership transfer (requires prev)
        if prev:
            candidates.append(check_ownership_transfer(snap, prev))

        # 6. Social silence
        candidates.append(check_social_silence(snap))

        # 7. New entry
        candidates.append(check_new_entry(snap, known_netuids))
```

Also update the docstring at the top of `evaluate_alerts` from:

```python
    """
    Evaluate all 6 alert conditions across all snapshots.
```

to:

```python
    """
    Evaluate all 7 alert conditions across all snapshots.
```

- [ ] **Step 4: Run the integration test to verify it passes**

```bash
pytest tests/engine/test_alerts.py::test_evaluate_alerts_fires_ownership_transfer -v
```

Expected: `PASSED`

- [ ] **Step 5: Run full test suite**

```bash
pytest -v
```

Expected: all tests pass (no regressions anywhere)

- [ ] **Step 6: Commit**

```bash
git add engine/alerts.py tests/engine/test_alerts.py
git commit -m "feat: wire check_ownership_transfer into evaluate_alerts"
```

---

### Task 3: Update TODOS.md

**Files:**
- Modify: `TODOS.md`

- [ ] **Step 1: Split the P1 item**

Replace the current P1 section:

```markdown
## P1 — Follow-up (next PR)

### Ownership transfer + whale inflow alerts (alerts 3 & 4)
**What:** Implement the two remaining alert types from the spec:
- `check_ownership_transfer`: compare `owner_coldkey` across consecutive snapshots — fire if it changed
- `check_whale_inflow`: detect when a single wallet stakes >5% of alpha supply in one poll (requires `get_stake_info_for_coldkey()` or `get_delegated()` to enumerate large stakes per subnet)
```

with:

```markdown
## P1 — Follow-up (next PR)

### ✅ Ownership transfer alert (alert #7) — DONE
Implemented in `engine/alerts.py` as `check_ownership_transfer`. Fires when `owner_coldkey`
changes between consecutive snapshots. Guarded against None values on both sides.

---

### Whale inflow alert (alert #8) — PENDING design confirmation
**What:** Detect when a single wallet stakes >5% of alpha supply in one poll.

**Blocked on:** Confirming the correct SDK call to enumerate all stakers for a subnet.
`bt.AsyncSubtensor.get_stake_info_for_coldkey(coldkey)` pulls by coldkey, not by subnet —
there is no apparent `get_all_stakers_for_subnet(netuid)` in the public API.

**Design options to validate before implementing:**
1. Raw substrate: `substrate.query_map("SubtensorModule", "Stake", [netuid])` — undocumented
2. Re-scope: only fire for the two P2 tracked wallets (simpler, misses unknown whales)
3. Defer entirely until P2 portfolio integration provides the tracked-wallet context

**Effort:** M (4 hours once design is confirmed)
**Priority:** P1
**Depends on:** Design confirmation via live chain query experiment
```

- [ ] **Step 2: Verify the file looks right**

```bash
cat TODOS.md
```

- [ ] **Step 3: Commit**

```bash
git add TODOS.md
git commit -m "docs: mark ownership_transfer done, clarify whale inflow design gap"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** CEO review specified: `check_ownership_transfer`, double-None guard, coldkey truncation in description, wire into `evaluate_alerts`. All covered.
- [x] **No placeholders:** Every step has actual code.
- [x] **Type consistency:** `check_ownership_transfer(current: SubnetSnapshot, prev: SubnetSnapshot) -> Optional[AlertRecord]` — same signature style as `check_emission_drop`. `AlertRecord` fields match `models.py` definition.
- [x] **Test covers:** fires on change, no-alert same, no-alert prev-None, no-alert curr-None, integration via evaluate_alerts.
- [x] **Import updated:** `check_ownership_transfer` added to import in test file.
- [x] **Docstring updated:** 6 → 7 in `evaluate_alerts` docstring.
