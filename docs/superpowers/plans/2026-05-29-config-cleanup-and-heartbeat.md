# Config Cleanup + Monitor Heartbeat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove dead/duplicate config and add a self-checking heartbeat so a stalled or erroring poll cycle becomes visible instead of failing silently.

**Architecture:** Two independent tasks. Task 1 deletes dead config lines and adds an AST-based regression test that forbids duplicate module-level definitions in `config.py`. Task 2 adds a pure age helper (`utils.poll_age_minutes`), a DB query for the newest poll time (`db.database.get_last_poll_time`), and an APScheduler job (`heartbeat_check` in `main.py`) that sends a Telegram warning when no successful poll has landed within a configurable window. The two logic pieces are unit-tested; the scheduler glue is kept thin and is not unit-tested.

**Tech Stack:** Python 3.13, FastAPI, APScheduler, aiosqlite, python-telegram-bot, pytest (`asyncio_mode = auto`, so plain `async def test_*` works without a marker).

**Context for a cold start:**
- The monitor polls all Bittensor subnets every `POLL_INTERVAL_MINUTES` (default 15) via `poll_cycle()` in `main.py`, inserting one `snapshots` row per subnet per cycle (`polled_at` is an ISO-8601 string).
- APScheduler runs each job independently with `max_instances=1`; if `poll_cycle` raises, the exception is logged to `logs/monitor.log` but nothing alerts. A separate heartbeat job catches this because no new `snapshots` rows get inserted, so `MAX(polled_at)` ages.
- **Known limitation (document, do not try to solve here):** an in-process heartbeat cannot detect total process death — if the process exits, the heartbeat job dies with it. True process-death detection needs an external watchdog (e.g. `KeepAlive` in `com.taomonitor.plist.example`). Task 2 only catches a *stalled or repeatedly-erroring* poll cycle while the process is alive.
- Telegram already exposes `TelegramBot.send_health_warning(message: str)` (`bot/telegram.py:100`), used today for the >50%-None-emission check in `main.py`. Reuse it.
- DB tests use an in-memory fixture: `aiosqlite.connect(":memory:")` + `await conn.executescript(SCHEMA_SQL)` (see `tests/test_database.py:13-21`). Only `netuid` and `polled_at` are NOT NULL in the `snapshots` table, so `SubnetSnapshot(netuid=..., polled_at=...)` inserts cleanly.

---

## File Map

- Modify: `config.py` — delete dead `TRADABILITY_REFERENCE_TAO = 10.0` (line 61) and dead `TRADABILITY_SLIPPAGE_BLOCK_PCT` (line 62); delete the stray duplicate `# ── Bittensor ──` header (line 53); add `HEARTBEAT_MAX_AGE_MINUTES` and `HEARTBEAT_CHECK_MINUTES`.
- Modify: `tests/test_config.py` — add an AST test forbidding duplicate module-level definitions.
- Modify: `utils.py` — add pure `poll_age_minutes()` helper.
- Modify: `tests/test_utils.py` — test `poll_age_minutes()`.
- Modify: `db/database.py` — add `get_last_poll_time()`.
- Modify: `tests/test_database.py` — test `get_last_poll_time()`.
- Modify: `main.py` — add `heartbeat_check()` job + register it on the scheduler.

---

## Task 1: Remove Dead / Duplicate Config + Add Regression Guard

**Why:** `config.py` defines `TRADABILITY_REFERENCE_TAO` twice — `10.0` (line 61, dead, shadowed) and `5.0` (line 102, live). The live value is the one read by `collectors/chain.py` and `engine/signals.py`; the `10.0` line is misleading dead weight. `TRADABILITY_SLIPPAGE_BLOCK_PCT` (line 62) is defined but referenced nowhere in the repo (verified: `grep -rn TRADABILITY_SLIPPAGE_BLOCK_PCT --include="*.py"` returns only `config.py`). Line 53 is a duplicate `# ── Bittensor ──` header sitting directly above the `# ── Portfolio tracking ──` header. For a product whose entire value is threshold-driven judgment, the config file must be unambiguous.

**Files:**
- Modify: `tests/test_config.py`
- Modify: `config.py`

- [ ] **Step 1: Write the failing regression test**

Add to `tests/test_config.py`:

```python
def test_config_has_no_duplicate_module_level_definitions():
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent / "config.py"
    tree = ast.parse(source.read_text())

    names: list[str] = []
    for node in tree.body:  # module level only
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)

    duplicates = sorted({name for name in names if names.count(name) > 1})
    assert duplicates == [], f"duplicate config definitions: {duplicates}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py::test_config_has_no_duplicate_module_level_definitions -v`
Expected: FAIL with `AssertionError: duplicate config definitions: ['TRADABILITY_REFERENCE_TAO']`

- [ ] **Step 3: Delete the dead duplicate lines in `config.py`**

Delete these two lines (currently lines 61-62), keeping the surrounding lines:

```python
TRADABILITY_REFERENCE_TAO: float = 10.0   # standardized round-trip size for slippage estimates
TRADABILITY_SLIPPAGE_BLOCK_PCT: float = 0.10  # >10% expected exit slippage blocks new buys
```

After deletion the block at the top of the Bittensor section reads:

```python
# ── Bittensor ────────────────────────────────────────────────────────────────
BITTENSOR_NETWORK: str = "finney"
BLOCKS_PER_DAY: int = 7200
X_SCRAPE_MAX_PER_CYCLE: int = 30            # max subnets per XCollector run
X_SCRAPE_DELAY_SECONDS: float = 2.0         # delay between X scrapes
```

Leave the live definitions in the `# ── Tradability scoring ──` section untouched:

```python
TRADABILITY_REFERENCE_TAO: float = 5.0           # reference swing trade size
TRADABILITY_MAX_SLIPPAGE_PCT: float = 8.0        # beyond this, new buys are blocked
```

- [ ] **Step 4: Delete the stray duplicate `Bittensor` header**

Delete the duplicate header line (currently line 53) that sits directly above `# ── Portfolio tracking ──`:

```python
# ── Bittensor ────────────────────────────────────────────────────────────────
```

Verify only ONE `# ── Bittensor ──` header remains:
Run: `grep -c "── Bittensor ──" config.py`
Expected: `1`

- [ ] **Step 5: Run the config tests + full suite**

Run: `.venv/bin/pytest tests/test_config.py -v && .venv/bin/pytest -q`
Expected: all PASS (the dead `TRADABILITY_SLIPPAGE_BLOCK_PCT` had no readers, so nothing else breaks).

- [ ] **Step 6: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "cleanup: remove dead/duplicate config + guard against duplicate definitions

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Monitor Heartbeat Alert

**Why:** If `poll_cycle()` starts raising every cycle (bad chain SDK upgrade, network, etc.), the only signal today is a log line in `logs/monitor.log` that nobody is watching — a silent failure. A heartbeat job that checks the age of the newest snapshot and pushes a Telegram warning makes a stalled pipeline visible.

**Files:**
- Modify: `config.py`
- Modify: `utils.py`
- Modify: `tests/test_utils.py`
- Modify: `db/database.py`
- Modify: `tests/test_database.py`
- Modify: `main.py`

- [ ] **Step 1: Write the failing test for the pure age helper**

Add to `tests/test_utils.py`:

```python
def test_poll_age_minutes_returns_none_when_no_poll():
    from datetime import datetime, timezone
    from utils import poll_age_minutes
    assert poll_age_minutes(None, datetime(2026, 5, 29, tzinfo=timezone.utc)) is None


def test_poll_age_minutes_computes_minutes_since_last_poll():
    from datetime import datetime, timezone, timedelta
    from utils import poll_age_minutes
    now = datetime(2026, 5, 29, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(minutes=42)
    assert poll_age_minutes(last, now) == pytest.approx(42.0)
```

If `pytest` is not already imported at the top of `tests/test_utils.py`, add `import pytest`.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_utils.py -k poll_age_minutes -v`
Expected: FAIL with `ImportError: cannot import name 'poll_age_minutes' from 'utils'`

- [ ] **Step 3: Implement `poll_age_minutes` in `utils.py`**

Add to `utils.py` (add `from datetime import datetime` to the imports if not present):

```python
def poll_age_minutes(last_polled_at: "datetime | None", now: "datetime") -> "float | None":
    """Minutes between the most recent poll and now, or None if there has been no poll."""
    if last_polled_at is None:
        return None
    return (now - last_polled_at).total_seconds() / 60.0
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/pytest tests/test_utils.py -k poll_age_minutes -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the DB query**

Add to `tests/test_database.py` (the `db` fixture and imports already exist at the top of that file; add `get_last_poll_time` to the `from db.database import ...` line):

```python
async def test_get_last_poll_time_returns_none_on_empty_db(db):
    from db.database import get_last_poll_time
    assert await get_last_poll_time(db) is None


async def test_get_last_poll_time_returns_newest_polled_at(db):
    from db.database import get_last_poll_time
    older = datetime(2026, 5, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 5, 2, tzinfo=timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=older))
    await insert_snapshot(db, SubnetSnapshot(netuid=2, polled_at=newer))
    assert await get_last_poll_time(db) == newer
```

- [ ] **Step 6: Run to verify it fails**

Run: `.venv/bin/pytest tests/test_database.py -k get_last_poll_time -v`
Expected: FAIL with `ImportError: cannot import name 'get_last_poll_time' from 'db.database'`

- [ ] **Step 7: Implement `get_last_poll_time` in `db/database.py`**

Add near `get_latest_snapshots` (around line 237). `polled_at` is stored as an ISO string by `_dt_to_str`, so parse it back with `datetime.fromisoformat`. `db/database.py` already imports `datetime` (used elsewhere); if not, add `from datetime import datetime`.

```python
async def get_last_poll_time(db: aiosqlite.Connection) -> "datetime | None":
    """Timestamp of the newest snapshot across all subnets, or None if the table is empty."""
    cursor = await db.execute("SELECT MAX(polled_at) FROM snapshots")
    row = await cursor.fetchone()
    if not row or row[0] is None:
        return None
    return datetime.fromisoformat(row[0])
```

- [ ] **Step 8: Run to verify it passes, then the full suite**

Run: `.venv/bin/pytest tests/test_database.py -k get_last_poll_time -v && .venv/bin/pytest -q`
Expected: all PASS

- [ ] **Step 9: Add heartbeat config**

Add to `config.py` in the `# ── Alert thresholds ──` section (near `HEALTH_CHECK_NONE_THRESHOLD`):

```python
# Heartbeat: warn if no successful poll has landed within this window. Defaults to
# 3x the 15-min poll interval so a couple of missed cycles don't false-alarm.
HEARTBEAT_MAX_AGE_MINUTES: int = int(os.getenv("HEARTBEAT_MAX_AGE_MINUTES", "45"))
HEARTBEAT_CHECK_MINUTES: int = int(os.getenv("HEARTBEAT_CHECK_MINUTES", "15"))
```

- [ ] **Step 10: Add the `heartbeat_check` job and register it in `main.py`**

In `main.py`, add `get_last_poll_time` to the existing `from db.database import (...)` block, and add `from utils import poll_age_minutes` near the other imports.

Add this function next to the other scheduled jobs (after `registry_refresh_and_prune`):

```python
async def heartbeat_check() -> None:
    """Warn via Telegram if no successful poll has landed within the heartbeat window.

    Catches a stalled or repeatedly-erroring poll cycle while the process is alive.
    Total process death needs an external watchdog (launchd KeepAlive) — see plist.
    """
    last = await get_last_poll_time(_db)
    age = poll_age_minutes(last, datetime.now(timezone.utc))
    if age is None:
        return  # no snapshots yet (fresh start) — nothing to warn about
    if age > config.HEARTBEAT_MAX_AGE_MINUTES:
        logger.warning("[HEARTBEAT] no successful poll in %.0f min", age)
        if _telegram:
            await _telegram.send_health_warning(
                f"⚠️ No successful poll in {age:.0f} min "
                f"(threshold {config.HEARTBEAT_MAX_AGE_MINUTES} min). Poll cycle may be stalled."
            )
```

Register it alongside the other `scheduler.add_job(...)` calls in `main()`:

```python
    scheduler.add_job(
        heartbeat_check, "interval", minutes=config.HEARTBEAT_CHECK_MINUTES,
        max_instances=1, id="heartbeat"
    )
```

- [ ] **Step 11: Verify the app imports and the full suite passes**

Run: `.venv/bin/python -c "import main"` (requires `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` in `.env`; if it exits with the startup error, that is `validate_config` working — set the vars or skip this check and rely on the test suite).
Run: `.venv/bin/pytest -q`
Expected: all PASS. `heartbeat_check` is thin glue over two unit-tested functions, so it is intentionally not unit-tested.

- [ ] **Step 12: Commit**

```bash
git add config.py utils.py db/database.py main.py tests/test_utils.py tests/test_database.py
git commit -m "feat: add monitor heartbeat alert for stalled poll cycles

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 2 (separate plan — do NOT attempt inside this one): Retire the legacy `composite_score` path

This is a subsystem-level refactor, not a cleanup, and it must get its own plan written via the `superpowers:writing-plans` skill before execution.

**Why it exists:** Two scoring vocabularies coexist — legacy (`yield_score` / `health_score` / `momentum_score` / `composite_score`) and new (`flow_score` / `relative_value_score` / `tradability_score` / `catalyst_score` / `risk_penalty` / `swing_score`). `engine/policy.py` silently falls back across them (`flow_score`→`momentum_score`, `swing_score`→`composite_score`), and `verdict_for_subnet` / `action_for_position` each carry a second "legacy scalar" code path. A missing new field silently degrades to a legacy field that means something different — a silent-failure smell.

**Blocked on:** ~30 days of populated `swing_score` history first (the monitor must run; the new columns only fill going forward). Validate the swing model via `scripts/backtest_signals.py` before deleting the legacy fallback, otherwise you remove the only working signal.

**Scope when written:** `composite_score` is referenced in 6 non-test modules — `models.py`, `web/routes.py`, `db/database.py`, `engine/policy.py`, `engine/recommendations.py`, `engine/scorer.py`. The plan should: (1) make every reader prefer the explicit `swing_score`; (2) remove the fallback chains in `engine/policy.py`; (3) remove the legacy scalar code paths in `verdict_for_subnet` / `action_for_position`; (4) decide whether `composite_score` stays as a persisted alias of `swing_score` or is dropped from the schema. Sequence it so each step keeps the suite green.

**Also flip the calibration gate here:** once the swing model is validated against forward returns, set `config.SWING_SIGNAL_VALIDATED = True` (added in commit `dc2186c`) so buy-side confidence labels stop being capped. See `docs/superpowers/plans/2026-04-29-tao-signal-reliability-plan.md` and the `backtest-calibration` memory for the first (legacy-composite) result.
