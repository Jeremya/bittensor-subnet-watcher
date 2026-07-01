# Trust Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the monitor's data and alerts believable: remove the dead X scraping pipeline (replacing it with manual tweet curation), convert chronic alerts to a state machine with a daily digest, add collector-health observability, and fix backup/retention hygiene.

**Architecture:** Five independent workstreams landed in order. New `condition_states` table + pure-function state machine (`engine/conditions.py`) drives chronic alerts; `engine/digest.py` and `engine/health.py` read from it. Manual tweets reuse the existing `analyst_mentions` table and its downstream pipeline unchanged. Spec: `docs/superpowers/specs/2026-07-01-trust-phase-design.md`.

**Tech Stack:** Python 3.13, aiosqlite, FastAPI + Jinja2, APScheduler, python-telegram-bot, pytest (async via pytest-asyncio patterns already in `tests/`).

**Verification command for every task:** `python -m pytest tests/ -q` from the repo root. The suite is fast; run it fully before each commit.

**CRITICAL CONTEXT — retention bug:** `registry_refresh_and_prune()` in `main.py:294` calls `prune_old_snapshots(_db, days=30)` daily. The live DB started 2026-06-15, so on ~2026-07-15 this starts hard-deleting exactly the history the P0 swing-score calibration needs. Task 6 fixes this; if tasks are executed out of order, do Task 6 first.

---

## Task 1: Remove X scraping and hype scoring

The X scraper never produced data (all 129 registry `x_handle` values are empty; anonymous scraping hits X's login wall). Remove the scraper, the hype score, and the `social_silence` alert. Keep `match_subnets` in `collectors/analyst.py` (used by Task 2 tests and potentially future sources). Keep all DB columns.

**Files:**
- Delete: `collectors/x_scraper.py`, `tests/collectors/test_x_scraper.py`
- Modify: `collectors/analyst.py`, `main.py`, `engine/scorer.py`, `engine/alerts.py`, `config.py`, `requirements.txt`, `models.py`
- Tests: `tests/engine/test_scorer.py`, `tests/engine/test_alerts.py`, `tests/test_main.py` (fix fallout only)

- [ ] **Step 1.1: Delete the scraper and its tests**

```bash
git rm collectors/x_scraper.py tests/collectors/test_x_scraper.py
```

- [ ] **Step 1.2: Strip scraping from `collectors/analyst.py`**

Remove the import `from collectors.x_scraper import get_browser_page`, the whole `_scrape_tweets` function (lines ~59–104), and the whole `AnalystCollector` class (lines ~107–149). Keep `_registry_name`, `_name_patterns`, `match_subnets`, `_SN_PATTERN`. Also remove now-unused imports (`asyncio`, `aiosqlite`, `config`, `datetime`/`timezone` if nothing else uses them — after the cut the file needs only `re` and `logging`).

- [ ] **Step 1.3: Remove X wiring from `main.py`**

1. Delete imports: `from collectors.analyst import AnalystCollector` and `from collectors.x_scraper import XCollector, close_browser`.
2. In `poll_cycle`, delete the X block (lines ~112–127): the `x_data = await asyncio.wait_for(XCollector.collect(...))` try/except and the "Merge X data into chain snapshots" loop. Keep `registry = await get_registry(_db)` (used later in the function).
3. Delete the whole `analyst_collect()` function (lines ~272–277) and its `scheduler.add_job(analyst_collect, ...)` registration (lines ~327–330). Keep the `fire_analyst_alerts` import removal too — it was only called from `analyst_collect` (manual mentions in Task 2 insert their alert rows directly).
4. In the `finally` block of `main()`, delete `await close_browser()`.

- [ ] **Step 1.4: Remove hype scoring from `engine/scorer.py`**

Delete `compute_hype_score` (lines ~185–215). In `score_snapshots`, delete the `followers` / `max_followers` computation (lines ~236–238) and the two hype lines (~260–262):

```python
        # Hype is computed for display but intentionally excluded from composite —
        snap.hype_score = compute_hype_score(snap, max_followers=max_followers)
```

`snap.hype_score` stays `None` and the DB column persists NULL from now on (honest NULL, per spec).

- [ ] **Step 1.5: Remove `social_silence` from `engine/alerts.py`**

Delete `check_social_silence` (lines ~113–127) and its call site in `evaluate_alerts` (`# 6. Social silence` / `candidates.append(check_social_silence(snap))`). Update the docstring list in `evaluate_alerts` to drop `social_silence`.

- [ ] **Step 1.6: Clean `config.py`, `models.py`, `requirements.txt`**

- `config.py`: delete `SOCIAL_SILENCE_DAYS`, `X_SCRAPE_MAX_PER_CYCLE`, `X_SCRAPE_DELAY_SECONDS`. Keep `ANALYST_HANDLES` / `MAX_ANALYST_HANDLES` (the `/analysts` watchlist UI still lists them) and `ANALYST_COVERAGE_DECAY_HOURS` (drives catalyst coverage decay). Delete `ANALYST_TWEET_LOOKBACK_HOURS` (scraper-only). Update the comment block at lines 27–28 ("Hype ... displayed as informational on the detail page") to say hype is no longer computed; X columns dormant.
- `models.py`: in the `AlertRecord.alert_type` comment, remove `'social_silence'`.
- `requirements.txt`: remove the `playwright` line (only `x_scraper.py` used it).

- [ ] **Step 1.7: Run tests, fix fallout**

Run: `python -m pytest tests/ -q`
Expected failures to fix (delete or update the specific tests, do not weaken unrelated ones):
- `tests/engine/test_scorer.py`: any `compute_hype_score` / `hype_score` assertions → delete those tests; where a test asserts `snap.hype_score` equals a number after `score_snapshots`, assert it is `None`.
- `tests/engine/test_alerts.py`: `social_silence` tests → delete.
- `tests/test_main.py` / `tests/test_alert_fires.py`: patched references to `XCollector` or `analyst_collect` → remove those patches.
- `tests/web/test_routes.py` / `tests/web/test_portfolio_route.py`: any assertions on `hype_score` values or `hype_why` content → relax to the "no social data" path.
- `tests/test_analyst_matching.py` must still pass unchanged (it only uses `match_subnets`).

Re-run until green: `python -m pytest tests/ -q` → all pass.

- [ ] **Step 1.8: Boot smoke-check + commit**

Run: `python -c "import main"` — expected: no ImportError (config validation may exit if env vars missing; run with `TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=y python -c "import main"`).

```bash
git add -A
git commit -m "feat: remove dead X scraping pipeline and hype scoring"
```

---

## Task 2: Manual tweet curation

Paste a tweet URL on a subnet page (or `/analysts`) → row in `analyst_mentions` (marked `notified=1`) + an `analyst_mention` alert row (also `notified=1`, so Telegram stays silent but convergence/catalyst see it). Downstream pipeline untouched.

**Files:**
- Create: `engine/mentions.py`, `tests/engine/test_mentions.py`
- Modify: `db/database.py` (`insert_analyst_mention` gains `notified` param; new `get_recent_analyst_mentions`), `web/routes.py`, `web/templates/subnet.html`, `web/templates/analysts.html`
- Tests: `tests/engine/test_mentions.py`, `tests/web/test_routes.py`

- [ ] **Step 2.1: Write failing tests for URL parsing + manual add**

Create `tests/engine/test_mentions.py`:

```python
import aiosqlite
import pytest

from db.database import init_db
from engine.mentions import add_manual_mention, parse_tweet_handle


def test_parse_tweet_handle_x_com():
    assert parse_tweet_handle("https://x.com/taoanalyst/status/1234567890") == "taoanalyst"


def test_parse_tweet_handle_twitter_com_and_www():
    assert parse_tweet_handle("https://www.twitter.com/Some_Handle/status/99?s=20") == "Some_Handle"


def test_parse_tweet_handle_rejects_non_status_urls():
    assert parse_tweet_handle("https://x.com/taoanalyst") is None
    assert parse_tweet_handle("https://example.com/x.com/a/status/1") is None
    assert parse_tweet_handle("not a url") is None


@pytest.mark.asyncio
async def test_add_manual_mention_inserts_mention_and_silent_alert(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        ok = await add_manual_mention(
            db, registry={9: {"name": "IOTA"}}, netuid=9,
            tweet_url="https://x.com/analyst/status/123", tweet_text="IOTA looks strong",
        )
        assert ok is True
        cur = await db.execute("SELECT analyst_handle, notified FROM analyst_mentions")
        handle, notified = await cur.fetchone()
        assert handle == "analyst" and notified == 1
        cur = await db.execute("SELECT alert_type, netuid, notified FROM alerts")
        atype, netuid, alert_notified = await cur.fetchone()
        assert atype == "analyst_mention" and netuid == 9 and alert_notified == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_add_manual_mention_dedups_and_rejects_bad_url(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        url = "https://x.com/analyst/status/123"
        assert await add_manual_mention(db, {}, 9, url, "text") is True
        assert await add_manual_mention(db, {}, 9, url, "text") is False   # dedup
        assert await add_manual_mention(db, {}, 9, "https://x.com/analyst", "t") is False
        cur = await db.execute("SELECT COUNT(*) FROM alerts")
        assert (await cur.fetchone())[0] == 1   # no duplicate alert either
    finally:
        await db.close()
```

- [ ] **Step 2.2: Run tests to verify they fail**

Run: `python -m pytest tests/engine/test_mentions.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.mentions'`

- [ ] **Step 2.3: Implement `engine/mentions.py` and DB support**

In `db/database.py`, change `insert_analyst_mention` to accept a notified flag:

```python
async def insert_analyst_mention(db: aiosqlite.Connection,
                                 handle: str,
                                 netuid: int,
                                 tweet_url: str,
                                 tweet_text: str,
                                 mentioned_at: datetime,
                                 notified: bool = False) -> bool:
    try:
        await db.execute(
            """
            INSERT INTO analyst_mentions
                (analyst_handle, netuid, tweet_url, tweet_text, mentioned_at, notified)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (handle.lstrip("@"), netuid, tweet_url, tweet_text,
             mentioned_at.isoformat(), 1 if notified else 0),
        )
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False
```

Add below it:

```python
async def get_recent_analyst_mentions(db: aiosqlite.Connection,
                                      limit: int = 30) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        """
        SELECT m.*, r.name AS subnet_name
        FROM analyst_mentions m
        LEFT JOIN subnet_registry r ON m.netuid = r.netuid
        ORDER BY m.mentioned_at DESC LIMIT ?
        """,
        (limit,),
    )
    return await cursor.fetchall()
```

Create `engine/mentions.py`:

```python
import re
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from db.database import insert_analyst_mention, insert_alert
from engine.alerts import _registry_name
from models import AlertRecord

_TWEET_URL_RE = re.compile(
    r"^https?://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/\d+",
    re.IGNORECASE,
)


def parse_tweet_handle(url: str) -> Optional[str]:
    """Extract the author handle from an x.com/twitter.com status URL, else None."""
    m = _TWEET_URL_RE.match(url.strip())
    return m.group(1) if m else None


async def add_manual_mention(db: aiosqlite.Connection,
                             registry: dict,
                             netuid: int,
                             tweet_url: str,
                             tweet_text: str) -> bool:
    """Store a hand-curated tweet as an analyst mention.

    Both the mention and its alert row are inserted pre-notified: the user just
    typed this, so Telegram must not echo it back — but convergence and catalyst
    scoring read these rows and should see them.
    """
    handle = parse_tweet_handle(tweet_url)
    if handle is None:
        return False
    url = tweet_url.strip()
    now = datetime.now(timezone.utc)
    inserted = await insert_analyst_mention(
        db, handle, netuid, url, tweet_text.strip(), now, notified=True
    )
    if not inserted:
        return False
    text_preview = tweet_text.strip()[:120]
    await insert_alert(db, AlertRecord(
        fired_at=now,
        netuid=netuid,
        subnet_name=_registry_name(registry, netuid),
        alert_type="analyst_mention",
        description=f"@{handle} (curated): \"{text_preview}\"\n→ {url}",
        current_value=None,
        threshold=None,
        notified=True,
    ))
    return True
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `python -m pytest tests/engine/test_mentions.py tests/ -q`
Expected: all PASS.

- [ ] **Step 2.5: Add routes**

In `web/routes.py`, import `get_recent_analyst_mentions` from `db.database` and `from engine.mentions import add_manual_mention`, then add after the `subnet_set_category` route:

```python
    @app.post("/subnet/{netuid}/mention")
    async def subnet_add_mention(netuid: int,
                                 tweet_url: str = Form(...),
                                 tweet_text: str = Form("")):
        from db.database import get_registry
        registry = await get_registry(db)
        await add_manual_mention(db, registry, netuid, tweet_url, tweet_text)
        return RedirectResponse(f"/subnet/{netuid}", status_code=303)

    @app.post("/analysts/mention")
    async def analysts_add_mention(netuid: int = Form(...),
                                   tweet_url: str = Form(...),
                                   tweet_text: str = Form("")):
        from db.database import get_registry
        registry = await get_registry(db)
        await add_manual_mention(db, registry, netuid, tweet_url, tweet_text)
        return RedirectResponse("/analysts", status_code=303)
```

And in `analysts_page`, add recent mentions to the context:

```python
        recent_mentions = await get_recent_analyst_mentions(db, limit=30)
        return templates.TemplateResponse(request, "analysts.html", {
            "db_handles": db_handles,
            "config_handles": config_handles,
            "recent_mentions": recent_mentions,
        })
```

- [ ] **Step 2.6: Add route test**

Append to `tests/web/test_routes.py` (follow the existing test-client fixture pattern in that file for creating the app with a temp DB):

```python
@pytest.mark.asyncio
async def test_post_subnet_mention_creates_row(client_and_db):
    client, db = client_and_db
    resp = await client.post(
        "/subnet/9/mention",
        data={"tweet_url": "https://x.com/analyst/status/42", "tweet_text": "big news"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    cur = await db.execute("SELECT netuid, notified FROM analyst_mentions")
    netuid, notified = await cur.fetchone()
    assert netuid == 9 and notified == 1
```

(Adapt the fixture name/style to what `tests/web/test_routes.py` actually uses — read it first; if it uses a sync `TestClient`, write the sync equivalent.)

Run: `python -m pytest tests/web/ -q` — expected: PASS.

- [ ] **Step 2.7: Templates**

`web/templates/subnet.html`: inside the Analyst Mentions section (around line 371, before the `{% for m in analyst_mentions %}` list — and also render the form when `analyst_mentions` is empty, so move it OUTSIDE the `{% if %}`), add:

```html
<form method="post" action="/subnet/{{ snap.netuid }}/mention" style="display:flex;gap:6px;margin:8px 0;">
  <input name="tweet_url" placeholder="https://x.com/…/status/…" required
         style="flex:2;background:#111;border:1px solid #333;color:#e0e0e0;padding:4px 8px;font-family:monospace;">
  <input name="tweet_text" placeholder="tweet text / note (optional)"
         style="flex:3;background:#111;border:1px solid #333;color:#e0e0e0;padding:4px 8px;font-family:monospace;">
  <button type="submit" style="background:#1a1a2e;border:1px solid #00d4aa;color:#00d4aa;padding:4px 10px;cursor:pointer;font-family:monospace;">+ tweet</button>
</form>
```

`web/templates/analysts.html`: after the Watched Accounts table, add a "Curated Tweets" section:

```html
<h2>Add Tweet</h2>
<form class="add-form" method="post" action="/analysts/mention">
  <input name="netuid" type="number" min="0" max="255" placeholder="netuid" required style="flex:0 0 90px;">
  <input name="tweet_url" placeholder="https://x.com/…/status/…" required>
  <input name="tweet_text" placeholder="tweet text / note (optional)">
  <button type="submit">Add</button>
</form>
<p class="note">Curated tweets feed catalyst scoring and convergence with a 72h decay. Automated X collection is retired.</p>

<h2>Recent Curated Tweets</h2>
<table>
  <tr><th>When</th><th>Subnet</th><th>Handle</th><th>Text</th></tr>
  {% for m in recent_mentions %}
  <tr>
    <td>{{ m.mentioned_at[:16] }}</td>
    <td><a href="/subnet/{{ m.netuid }}" style="color:#00d4aa;">SN{{ m.netuid }}{% if m.subnet_name %} {{ m.subnet_name }}{% endif %}</a></td>
    <td>@{{ m.analyst_handle }}</td>
    <td><a href="{{ m.tweet_url }}" style="color:#888;">{{ (m.tweet_text or m.tweet_url)[:80] }}</a></td>
  </tr>
  {% endfor %}
  {% if not recent_mentions %}<tr><td colspan="4" class="empty">None yet.</td></tr>{% endif %}
</table>
```

Also update the page's watchlist note: handles are informational now (no automated collection).

- [ ] **Step 2.8: Full test run + commit**

Run: `python -m pytest tests/ -q` — expected: all PASS.

```bash
git add -A
git commit -m "feat: manual tweet curation feeding analyst mention pipeline"
```

---

## Task 3: Condition state machine for chronic alerts

Chronic conditions (`emission_near_zero`, `emission_divergence`, `dead_github`, `emission_drop`, `liquidity_floor`) fire once on confirmed entry (2 consecutive breached polls) and once on confirmed recovery (4 consecutive clear polls). Missing data freezes the state. Acute alerts unchanged.

**Refinement vs spec:** the table gains lifecycle columns (`status`, streaks) so hysteresis survives restarts; the spec's `(netuid, condition, entered_at)` identity is preserved via `first_breach_at` in the PK.

**Files:**
- Create: `engine/conditions.py`, `tests/engine/test_conditions.py`
- Modify: `db/database.py` (schema), `engine/alerts.py` (route chronic checks), `config.py`
- Tests: `tests/engine/test_conditions.py`, `tests/engine/test_alerts.py`

- [ ] **Step 3.1: Add schema + config**

In `db/database.py` `SCHEMA_SQL`, before the index block, add:

```sql
CREATE TABLE IF NOT EXISTS condition_states (
    netuid          INTEGER NOT NULL,   -- -1 sentinel rows = collector health, not a subnet
    condition       TEXT NOT NULL,      -- e.g. 'emission_near_zero', 'collector_stale_github'
    status          TEXT NOT NULL,      -- 'pending' | 'active' | 'cleared'
    first_breach_at TEXT NOT NULL,
    entered_at      TEXT,
    cleared_at      TEXT,
    breach_streak   INTEGER NOT NULL DEFAULT 1,
    clear_streak    INTEGER NOT NULL DEFAULT 0,
    last_value      REAL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (netuid, condition, first_breach_at)
);
```

And with the other indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_condition_states_live ON condition_states (netuid, condition, status);
```

(`CREATE TABLE IF NOT EXISTS` in the executescript covers migration — no ALTER needed.)

In `config.py`, add under Alert thresholds:

```python
CONDITION_ENTER_POLLS: int = 2   # consecutive breached polls before 'entered' fires
CONDITION_CLEAR_POLLS: int = 4   # consecutive clear polls before 'recovered' fires
```

- [ ] **Step 3.2: Write failing state-machine tests**

Create `tests/engine/test_conditions.py`:

```python
import pytest

from db.database import init_db
from engine.conditions import advance_condition, get_active_conditions


async def _observe(db, breached, value=1.0, n=1):
    """Advance the same (netuid=5, 'emission_near_zero') condition n times."""
    results = []
    for _ in range(n):
        results.append(await advance_condition(
            db, netuid=5, condition="emission_near_zero",
            breached=breached, value=value,
        ))
    return results


@pytest.mark.asyncio
async def test_single_breach_does_not_enter(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        assert await _observe(db, True, n=1) == [None]
        assert await get_active_conditions(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_two_breaches_enter_once(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        assert await _observe(db, True, n=3) == [None, "entered", None]
        active = await get_active_conditions(db)
        assert len(active) == 1 and active[0]["condition"] == "emission_near_zero"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_flap_pending_is_dropped(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=1)     # pending
        await _observe(db, False, n=1)    # healthy again → pending dropped
        assert await _observe(db, True, n=1) == [None]  # streak restarted at 1
        assert await get_active_conditions(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_recovery_needs_four_clear_polls(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)                       # entered
        assert await _observe(db, False, n=3) == [None] * 3
        assert await _observe(db, False, n=1) == ["recovered"]
        assert await get_active_conditions(db) == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_clear_streak_resets_on_rebreak(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)      # entered
        await _observe(db, False, n=3)     # clearing…
        await _observe(db, True, n=1)      # re-breach resets clear streak
        assert await _observe(db, False, n=3) == [None] * 3   # needs 4 again
        assert len(await get_active_conditions(db)) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_data_freezes_state(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)   # entered
        assert await _observe(db, None, n=10) == [None] * 10
        assert len(await get_active_conditions(db)) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reentry_creates_new_episode(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await _observe(db, True, n=2)      # episode 1 entered
        await _observe(db, False, n=4)     # episode 1 recovered
        assert await _observe(db, True, n=2)[-1] == "entered"   # episode 2
        cur = await db.execute("SELECT COUNT(*) FROM condition_states WHERE netuid=5")
        assert (await cur.fetchone())[0] == 2
    finally:
        await db.close()
```

- [ ] **Step 3.3: Run tests to verify they fail**

Run: `python -m pytest tests/engine/test_conditions.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.conditions'`

- [ ] **Step 3.4: Implement `engine/conditions.py`**

```python
"""Chronic-condition state machine.

Chronic alert conditions route through here instead of firing every poll:
a condition must be breached for CONDITION_ENTER_POLLS consecutive polls to
'enter' (one alert), and clear for CONDITION_CLEAR_POLLS consecutive polls to
'recover' (one alert). breached=None (data missing) freezes the state.

Rows live in condition_states: 'pending' (breaching, unconfirmed),
'active' (confirmed), 'cleared' (historical episode). netuid -1 is a sentinel
for collector-health conditions — netuid 0 is the live root network.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

import config

logger = logging.getLogger(__name__)


async def _live_row(db: aiosqlite.Connection, netuid: int,
                    condition: str) -> Optional[aiosqlite.Row]:
    cursor = await db.execute(
        """
        SELECT * FROM condition_states
        WHERE netuid=? AND condition=? AND status IN ('pending', 'active')
        """,
        (netuid, condition),
    )
    return await cursor.fetchone()


async def advance_condition(db: aiosqlite.Connection,
                            netuid: int,
                            condition: str,
                            breached: Optional[bool],
                            value: Optional[float] = None,
                            now: Optional[datetime] = None) -> Optional[str]:
    """Feed one observation into the state machine.

    Returns 'entered' or 'recovered' on a confirmed transition, else None.
    """
    if breached is None:
        return None   # missing data: freeze
    now = now or datetime.now(timezone.utc)
    now_s = now.isoformat()
    row = await _live_row(db, netuid, condition)

    if row is None:
        if breached:
            await db.execute(
                """
                INSERT INTO condition_states
                    (netuid, condition, status, first_breach_at,
                     breach_streak, clear_streak, last_value, updated_at)
                VALUES (?, ?, 'pending', ?, 1, 0, ?, ?)
                """,
                (netuid, condition, now_s, value, now_s),
            )
            await db.commit()
        return None

    key = (row["netuid"], row["condition"], row["first_breach_at"])

    if row["status"] == "pending":
        if not breached:
            await db.execute(
                "DELETE FROM condition_states WHERE netuid=? AND condition=? AND first_breach_at=?",
                key,
            )
            await db.commit()
            return None
        streak = row["breach_streak"] + 1
        if streak >= config.CONDITION_ENTER_POLLS:
            await db.execute(
                """
                UPDATE condition_states
                SET status='active', entered_at=?, breach_streak=?, last_value=?, updated_at=?
                WHERE netuid=? AND condition=? AND first_breach_at=?
                """,
                (now_s, streak, value, now_s, *key),
            )
            await db.commit()
            logger.info("[CONDITION] entered netuid=%d condition=%s", netuid, condition)
            return "entered"
        await db.execute(
            """
            UPDATE condition_states
            SET breach_streak=?, last_value=?, updated_at=?
            WHERE netuid=? AND condition=? AND first_breach_at=?
            """,
            (streak, value, now_s, *key),
        )
        await db.commit()
        return None

    # status == 'active'
    if breached:
        await db.execute(
            """
            UPDATE condition_states
            SET clear_streak=0, last_value=?, updated_at=?
            WHERE netuid=? AND condition=? AND first_breach_at=?
            """,
            (value, now_s, *key),
        )
        await db.commit()
        return None
    clear_streak = row["clear_streak"] + 1
    if clear_streak >= config.CONDITION_CLEAR_POLLS:
        await db.execute(
            """
            UPDATE condition_states
            SET status='cleared', cleared_at=?, clear_streak=?, updated_at=?
            WHERE netuid=? AND condition=? AND first_breach_at=?
            """,
            (now_s, clear_streak, now_s, *key),
        )
        await db.commit()
        logger.info("[CONDITION] recovered netuid=%d condition=%s", netuid, condition)
        return "recovered"
    await db.execute(
        """
        UPDATE condition_states
        SET clear_streak=?, updated_at=?
        WHERE netuid=? AND condition=? AND first_breach_at=?
        """,
        (clear_streak, now_s, *key),
    )
    await db.commit()
    return None


async def get_active_conditions(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM condition_states WHERE status='active' ORDER BY condition, netuid"
    )
    return await cursor.fetchall()


async def get_condition_transitions_since(db: aiosqlite.Connection,
                                          since_iso: str) -> list[aiosqlite.Row]:
    """Episodes that entered or recovered since `since_iso` (for the digest)."""
    cursor = await db.execute(
        """
        SELECT * FROM condition_states
        WHERE (entered_at IS NOT NULL AND entered_at > ?)
           OR (cleared_at IS NOT NULL AND cleared_at > ?)
        ORDER BY condition, netuid
        """,
        (since_iso, since_iso),
    )
    return await cursor.fetchall()
```

- [ ] **Step 3.5: Run tests to verify they pass**

Run: `python -m pytest tests/engine/test_conditions.py -q` — expected: all PASS.

- [ ] **Step 3.6: Commit the state machine**

```bash
git add engine/conditions.py tests/engine/test_conditions.py db/database.py config.py
git commit -m "feat: chronic-condition state machine with hysteresis"
```

- [ ] **Step 3.7: Route chronic alerts through the state machine — failing test first**

Add to `tests/engine/test_alerts.py` (reuse that file's existing helpers for building snapshots/registry — read it first and match its style):

```python
@pytest.mark.asyncio
async def test_chronic_alert_fires_on_entry_not_every_poll(tmp_path):
    """emission_near_zero: 2nd breached poll fires 'entered', 3rd fires nothing."""
    db = await init_db(str(tmp_path / "t.db"))
    try:
        snap = SubnetSnapshot(
            netuid=7, polled_at=datetime.now(timezone.utc),
            daily_emission_tao=1.0, alpha_mcap_usd=500_000.0,
        )
        for expected_new in (0, 1, 0):
            fired = await evaluate_alerts(db, [snap], {}, {}, {7})
            chronic = [a for a in fired if a.alert_type == "emission_near_zero"]
            assert len(chronic) == expected_new
        cur = await db.execute(
            "SELECT COUNT(*) FROM alerts WHERE alert_type='emission_near_zero'")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_chronic_alert_recovers_once(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        bad = SubnetSnapshot(netuid=7, polled_at=datetime.now(timezone.utc),
                             daily_emission_tao=1.0, alpha_mcap_usd=500_000.0)
        good = SubnetSnapshot(netuid=7, polled_at=datetime.now(timezone.utc),
                              daily_emission_tao=50.0, alpha_mcap_usd=500_000.0)
        for _ in range(2):
            await evaluate_alerts(db, [bad], {}, {}, {7})
        recovered = []
        for _ in range(4):
            fired = await evaluate_alerts(db, [good], {}, {}, {7})
            recovered += [a for a in fired if "recovered" in a.description]
        assert len(recovered) == 1
    finally:
        await db.close()
```

Run: `python -m pytest tests/engine/test_alerts.py -q` — expected: the two new tests FAIL (today an alert fires on every breached poll, cooldown-permitting), existing tests pass.

- [ ] **Step 3.8: Implement chronic routing in `engine/alerts.py`**

Add near the top of `engine/alerts.py`:

```python
from engine.conditions import advance_condition

# Chronic conditions route through the condition state machine: one alert on
# confirmed entry, one on confirmed recovery. Everything else stays acute
# (immediate fire + cooldown): important_buy/sell, whale_inflow, tao_outflow,
# ownership_transfer, new_entry, hyperparameter_change, github_spike,
# emergence_watch, milestone, analyst_mention, convergence.
CHRONIC_ALERT_TYPES = {
    "emission_near_zero",
    "emission_divergence",
    "dead_github",
    "emission_drop",
    "liquidity_floor",
}
```

In `evaluate_alerts`, replace the per-snapshot "Dedup and persist" loop with two-phase handling. The five chronic `check_*` functions are still called the same way, but their results are interpreted as observations, not alerts:

```python
        # Chronic checks: interpret result as an observation for the state machine.
        # check() returned an AlertRecord → breached (value/description carried over).
        # check() returned None → healthy, UNLESS its input data was missing → freeze.
        chronic_observations: list[tuple[str, Optional[bool], Optional[float], Optional[AlertRecord]]] = []

        div_alert = None
        if em_rank is not None and mc_rank is not None:
            div_alert = check_emission_divergence(snap, em_rank, mc_rank)
            chronic_observations.append(
                ("emission_divergence", div_alert is not None,
                 div_alert.current_value if div_alert else None, div_alert))
        else:
            chronic_observations.append(("emission_divergence", None, None, None))

        dg_alert = check_dead_github(snap)
        dg_known = (snap.gh_last_push is not None and snap.alpha_mcap_usd is not None
                    and snap.alpha_mcap_usd >= config.DEAD_GITHUB_MIN_MCAP_USD)
        chronic_observations.append(
            ("dead_github", (dg_alert is not None) if dg_known else None,
             dg_alert.current_value if dg_alert else None, dg_alert))

        ed_alert = check_emission_drop(snap, prev) if prev else None
        ed_known = (prev is not None and snap.emission_rank is not None
                    and prev.emission_rank is not None)
        chronic_observations.append(
            ("emission_drop", (ed_alert is not None) if ed_known else None,
             ed_alert.current_value if ed_alert else None, ed_alert))

        ez_alert = check_emission_near_zero(snap)
        ez_known = (snap.daily_emission_tao is not None and snap.alpha_mcap_usd is not None
                    and snap.alpha_mcap_usd >= config.EMISSION_NEAR_ZERO_MIN_MCAP_USD)
        chronic_observations.append(
            ("emission_near_zero", (ez_alert is not None) if ez_known else None,
             ez_alert.current_value if ez_alert else None, ez_alert))

        lf_alert = check_liquidity_floor(snap)
        lf_known = (snap.volume_24h_alpha is not None and snap.alpha_price_tao is not None
                    and snap.alpha_mcap_tao is not None and snap.alpha_mcap_tao > 0
                    and snap.alpha_mcap_usd is not None
                    and snap.alpha_mcap_usd >= config.LIQUIDITY_MIN_MCAP_USD)
        chronic_observations.append(
            ("liquidity_floor", (lf_alert is not None) if lf_known else None,
             lf_alert.current_value if lf_alert else None, lf_alert))

        for condition, breached, value, source_alert in chronic_observations:
            transition = await advance_condition(db, snap.netuid, condition, breached, value)
            if transition == "entered" and source_alert is not None:
                source_alert.subnet_name = _registry_name(registry, snap.netuid)
                source_alert.description = f"entered: {source_alert.description}"
                await insert_alert(db, source_alert)
                fired.append(source_alert)
                logger.info("[ALERT] netuid=%d type=%s transition=entered",
                            snap.netuid, condition)
            elif transition == "recovered":
                rec = AlertRecord(
                    fired_at=datetime.now(timezone.utc),
                    netuid=snap.netuid,
                    subnet_name=_registry_name(registry, snap.netuid),
                    alert_type=condition,
                    description=f"recovered: {condition.replace('_', ' ')} condition cleared",
                    current_value=value, threshold=None,
                )
                await insert_alert(db, rec)
                fired.append(rec)
                logger.info("[ALERT] netuid=%d type=%s transition=recovered",
                            snap.netuid, condition)
```

The remaining acute candidates (`check_github_spike`, `check_ownership_transfer`, `check_new_entry`, `check_flow_impulse`, `check_hyperparameter_change`, `check_emergence_watch`) keep the existing candidates/cooldown loop. Remove the chronic checks from the `candidates` list — they must not be double-processed. Keep the mcap-rank computation as is (it feeds `check_emission_divergence`).

Note on `emission_drop`: its "recovered" now means "rank no longer >2 below the 15-min-previous snapshot", which self-clears quickly — that is acceptable; the entry alert is the signal.

- [ ] **Step 3.9: Run all tests, fix pre-existing chronic-alert tests**

Run: `python -m pytest tests/ -q`
`tests/engine/test_alerts.py` and `tests/test_alert_fires.py` contain tests asserting that chronic checks fire an alert on a single evaluation — update them: chronic types now need two consecutive `evaluate_alerts` calls to fire, and the alert description gains the `entered: ` prefix. The pure `check_*` unit tests (function returns an AlertRecord) are unchanged and must still pass.

Expected: all PASS after updates.

- [ ] **Step 3.10: Commit**

```bash
git add -A
git commit -m "feat: route chronic alerts through condition state machine"
```

---

## Task 4: Daily digest

One Telegram message at 08:00 local time summarizing active conditions and last-24h transitions. Reads only `condition_states` (+ registry for names). Collector-health lines appear automatically once Task 5 adds `collector_stale_*` sentinel conditions.

**Files:**
- Create: `engine/digest.py`, `tests/engine/test_digest.py`
- Modify: `bot/telegram.py` (`send_digest`), `main.py` (cron job), `config.py` (`DIGEST_HOUR_LOCAL`)

- [ ] **Step 4.1: Write failing digest tests**

Create `tests/engine/test_digest.py`:

```python
from datetime import datetime, timezone

import pytest

from db.database import init_db
from engine.conditions import advance_condition
from engine.digest import build_daily_digest


@pytest.mark.asyncio
async def test_digest_empty_db(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        text = await build_daily_digest(db, registry={})
        assert "all clear" in text.lower()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_digest_groups_by_condition_and_marks_new(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for netuid in (7, 8):
            for _ in range(2):
                await advance_condition(db, netuid, "emission_near_zero", True, 1.0)
        for _ in range(2):
            await advance_condition(db, 9, "dead_github", True, 90.0)
        text = await build_daily_digest(db, registry={7: {"name": "Seven"}})
        assert "emission_near_zero: 2" in text
        assert "dead_github: 1" in text
        assert "Seven" in text          # registry name used
        assert "SN8" in text            # fallback name
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_digest_renders_sentinel_as_collector_health(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for _ in range(2):
            await advance_condition(db, -1, "collector_stale_github", True, 5.0)
        text = await build_daily_digest(db, registry={})
        assert "Collector health" in text
        assert "github" in text
        assert "SN-1" not in text
    finally:
        await db.close()
```

- [ ] **Step 4.2: Run to verify failure**

Run: `python -m pytest tests/engine/test_digest.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.digest'`

- [ ] **Step 4.3: Implement `engine/digest.py`**

```python
"""Daily digest of active chronic conditions and collector health."""
from datetime import datetime, timedelta, timezone

import aiosqlite

from engine.conditions import get_active_conditions, get_condition_transitions_since

SENTINEL_NETUID = -1
COLLECTOR_PREFIX = "collector_stale_"
MAX_NAMES_PER_CONDITION = 8


def _subnet_label(registry: dict, netuid: int) -> str:
    row = registry.get(netuid)
    name = None
    if row is not None:
        try:
            name = row["name"] if not isinstance(row, dict) else row.get("name")
        except (KeyError, TypeError, IndexError):
            name = getattr(row, "name", None)
    return f"SN{netuid} {name}" if name else f"SN{netuid}"


async def build_daily_digest(db: aiosqlite.Connection,
                             registry: dict,
                             now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=24)).isoformat()
    active = await get_active_conditions(db)
    transitions = await get_condition_transitions_since(db, since)

    new_keys = {(r["netuid"], r["condition"]) for r in transitions
                if r["entered_at"] and r["entered_at"] > since}
    recovered = [r for r in transitions
                 if r["cleared_at"] and r["cleared_at"] > since]

    subnet_rows = [r for r in active if r["netuid"] != SENTINEL_NETUID]
    collector_rows = [r for r in active if r["netuid"] == SENTINEL_NETUID]

    lines = [f"📋 TAO Monitor digest — {now.strftime('%Y-%m-%d %H:%M UTC')}"]

    if collector_rows:
        lines.append("\n🩺 Collector health:")
        for row in collector_rows:
            name = row["condition"].removeprefix(COLLECTOR_PREFIX)
            lines.append(f"  ⛔ {name} stale since {row['entered_at'][:16]}")

    if not subnet_rows and not collector_rows:
        lines.append("✅ All clear — no active conditions.")
        return "\n".join(lines)

    by_condition: dict[str, list] = {}
    for row in subnet_rows:
        by_condition.setdefault(row["condition"], []).append(row)

    lines.append("\n⚠️ Active conditions:")
    for condition in sorted(by_condition):
        rows = by_condition[condition]
        new_count = sum(1 for r in rows if (r["netuid"], r["condition"]) in new_keys)
        suffix = f" ({new_count} new)" if new_count else ""
        names = ", ".join(_subnet_label(registry, r["netuid"])
                          for r in rows[:MAX_NAMES_PER_CONDITION])
        if len(rows) > MAX_NAMES_PER_CONDITION:
            names += f", +{len(rows) - MAX_NAMES_PER_CONDITION} more"
        lines.append(f"  {condition}: {len(rows)} subnets{suffix} — {names}")

    sub_recovered = [r for r in recovered if r["netuid"] != SENTINEL_NETUID]
    if sub_recovered:
        names = ", ".join(_subnet_label(registry, r["netuid"]) for r in sub_recovered[:8])
        lines.append(f"\n✅ Recovered in last 24h: {len(sub_recovered)} — {names}")

    return "\n".join(lines)
```

- [ ] **Step 4.4: Run digest tests**

Run: `python -m pytest tests/engine/test_digest.py -q` — expected: PASS.

- [ ] **Step 4.5: Wire Telegram + scheduler**

`bot/telegram.py` — add to `TelegramBot`:

```python
    async def send_digest(self, text: str) -> bool:
        """Send the daily digest as a single message."""
        return await self._try_send(text)
```

`config.py`:

```python
DIGEST_HOUR_LOCAL: int = int(os.getenv("DIGEST_HOUR_LOCAL", "8"))
```

`main.py` — add job function after `registry_refresh_and_prune`:

```python
async def daily_digest() -> None:
    """08:00 local: one-message summary of active conditions + collector health."""
    from engine.digest import build_daily_digest
    registry = await get_registry(_db)
    text = await build_daily_digest(_db, registry)
    if _telegram:
        await _telegram.send_digest(text)
    logger.info("[DIGEST] sent chars=%d", len(text))
```

and register it with the other jobs (APScheduler cron triggers use the machine's local timezone by default, which is what we want):

```python
    scheduler.add_job(
        daily_digest, "cron", hour=config.DIGEST_HOUR_LOCAL, minute=0,
        max_instances=1, id="digest"
    )
```

- [ ] **Step 4.6: Full run + commit**

Run: `python -m pytest tests/ -q` — expected: all PASS.

```bash
git add -A
git commit -m "feat: daily Telegram digest of active conditions"
```

---

## Task 5: Collector health panel

`/api/health` + dashboard panel computed at request time; a per-poll health sweep feeds `collector_stale_*` sentinel conditions (netuid −1) so a dead collector alerts once and appears in every digest.

**Files:**
- Create: `engine/health.py`, `tests/engine/test_health.py`
- Modify: `config.py`, `main.py` (health sweep in poll cycle), `web/routes.py` (`/api/health`, dashboard context), `web/templates/index.html`
- Tests: `tests/engine/test_health.py`, `tests/web/test_routes.py`

- [ ] **Step 5.1: Config thresholds**

Add to `config.py`:

```python
# ── Collector health ──────────────────────────────────────────────────────────
HEALTH_CHAIN_STALE_MINUTES: int = 45        # chain rows expected every 15 min
HEALTH_GITHUB_STALE_HOURS: int = 3          # github refresh runs every 60 min
HEALTH_MILESTONE_STALE_HOURS: int = 13      # milestone poll runs every 6h
HEALTH_NULL_RATE_MAX: float = 0.30          # key-field null-rate ceiling (last 24h)
```

- [ ] **Step 5.2: Write failing health tests**

Create `tests/engine/test_health.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from db.database import init_db, insert_snapshot, set_collector_state
from engine.health import compute_collector_health
from models import SubnetSnapshot


def _snap(netuid=1, age_minutes=0, price=1.0, gh_stars=10):
    return SubnetSnapshot(
        netuid=netuid,
        polled_at=datetime.now(timezone.utc) - timedelta(minutes=age_minutes),
        alpha_price_tao=price, buy_slippage_pct=1.0, gh_stars=gh_stars,
        gh_last_push=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_fresh_data_is_healthy(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(age_minutes=5))
        await set_collector_state(db, "milestone_last_arxiv_check",
                                  datetime.now(timezone.utc).isoformat())
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["chain"].stale is False
        assert health["github"].stale is False
        assert health["milestone"].stale is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_old_rows_mark_chain_stale(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        await insert_snapshot(db, _snap(age_minutes=120))
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["chain"].stale is True
        assert any("stale" in r for r in health["chain"].reasons)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_high_null_rate_marks_chain_stale(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        for i in range(10):
            await insert_snapshot(db, _snap(netuid=i, price=None))
        health = {h.name: h for h in await compute_collector_health(db)}
        assert health["chain"].stale is True
        assert any("null" in r for r in health["chain"].reasons)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_empty_db_reports_all_stale(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        health = await compute_collector_health(db)
        assert all(h.stale for h in health)
    finally:
        await db.close()
```

Run: `python -m pytest tests/engine/test_health.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.health'`

- [ ] **Step 5.3: Implement `engine/health.py`**

```python
"""Collector health: data freshness + key-field null rates, computed from
existing tables at call time (no new collection infrastructure).

Feeds (a) /api/health + dashboard panel, (b) collector_stale_* sentinel
conditions (netuid -1) via the condition state machine, so a silently dead
collector — the failure class behind the never-populated X pipeline — pings
Telegram once and shows in every digest until fixed.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

import config

SENTINEL_NETUID = -1


@dataclass
class CollectorHealth:
    name: str
    last_success: Optional[str]          # ISO timestamp of newest evidence of life
    rows_24h: int
    null_rates: dict[str, float] = field(default_factory=dict)
    stale: bool = False
    reasons: list[str] = field(default_factory=list)


async def _newest(db: aiosqlite.Connection, sql: str, params: tuple = ()) -> Optional[str]:
    cursor = await db.execute(sql, params)
    row = await cursor.fetchone()
    return row[0] if row and row[0] else None


async def _null_rates(db: aiosqlite.Connection, fields: list[str],
                      cutoff: str) -> tuple[int, dict[str, float]]:
    cols = ", ".join(f"SUM({f} IS NULL) AS n_{f}" for f in fields)
    cursor = await db.execute(
        f"SELECT COUNT(*) AS total, {cols} FROM snapshots WHERE polled_at > ?",
        (cutoff,),
    )
    row = await cursor.fetchone()
    total = row["total"]
    if total == 0:
        return 0, {f: 1.0 for f in fields}
    return total, {f: row[f"n_{f}"] / total for f in fields}


def _apply_staleness(h: CollectorHealth, max_age: timedelta,
                     now: datetime) -> None:
    if h.last_success is None:
        h.stale = True
        h.reasons.append("no data ever")
        return
    age = now - datetime.fromisoformat(h.last_success).replace(tzinfo=timezone.utc)
    if age > max_age:
        h.stale = True
        h.reasons.append(f"stale: last success {age.total_seconds() / 3600:.1f}h ago")
    for fld, rate in h.null_rates.items():
        if rate > config.HEALTH_NULL_RATE_MAX:
            h.stale = True
            h.reasons.append(f"null-rate {fld}: {rate * 100:.0f}%")


async def compute_collector_health(db: aiosqlite.Connection,
                                   now: Optional[datetime] = None) -> list[CollectorHealth]:
    now = now or datetime.now(timezone.utc)
    cutoff_24h = (now - timedelta(hours=24)).isoformat()

    total, chain_nulls = await _null_rates(
        db, ["alpha_price_tao", "buy_slippage_pct"], cutoff_24h)
    chain = CollectorHealth(
        name="chain",
        last_success=await _newest(db, "SELECT MAX(polled_at) FROM snapshots"),
        rows_24h=total,
        null_rates=chain_nulls,
    )
    _apply_staleness(chain, timedelta(minutes=config.HEALTH_CHAIN_STALE_MINUTES), now)

    gh_total, gh_nulls = await _null_rates(db, ["gh_last_push"], cutoff_24h)
    github = CollectorHealth(
        name="github",
        last_success=await _newest(
            db, "SELECT MAX(polled_at) FROM snapshots WHERE gh_stars IS NOT NULL"),
        rows_24h=gh_total,
        null_rates=gh_nulls,
    )
    _apply_staleness(github, timedelta(hours=config.HEALTH_GITHUB_STALE_HOURS), now)

    checks = []
    for key in ("milestone_last_arxiv_check", "milestone_last_hf_check"):
        val = await _newest(db, "SELECT value FROM collector_state WHERE key=?", (key,))
        if val:
            checks.append(val)
    milestone = CollectorHealth(
        name="milestone",
        last_success=max(checks) if checks else None,
        rows_24h=0,
    )
    _apply_staleness(milestone, timedelta(hours=config.HEALTH_MILESTONE_STALE_HOURS), now)

    return [chain, github, milestone]


async def sweep_collector_conditions(db: aiosqlite.Connection,
                                     now: Optional[datetime] = None) -> list[str]:
    """Advance collector_stale_* sentinel conditions from current health.

    Returns descriptions of confirmed transitions (for logging)."""
    from engine.conditions import advance_condition
    transitions: list[str] = []
    for h in await compute_collector_health(db, now):
        t = await advance_condition(
            db, SENTINEL_NETUID, f"collector_stale_{h.name}", h.stale,
            value=float(h.rows_24h),
        )
        if t:
            transitions.append(f"{h.name}:{t}")
    return transitions
```

- [ ] **Step 5.4: Run health tests**

Run: `python -m pytest tests/engine/test_health.py -q` — expected: PASS.

- [ ] **Step 5.5: Wire into poll cycle, alerts, and routes**

`main.py` — at the end of `poll_cycle`, after the `evaluate_convergence` call (step 6) add:

```python
    # 6b. Collector-health sweep → collector_stale_* conditions (sentinel netuid -1).
    from engine.health import sweep_collector_conditions
    health_transitions = await sweep_collector_conditions(_db)
    for entry in health_transitions:
        name, transition = entry.split(":")
        from models import AlertRecord
        await insert_alert(_db, AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=-1,
            subnet_name="Collector health",
            alert_type="collector_stale",
            description=f"{transition}: {name} collector "
                        + ("has gone stale/degraded" if transition == "entered"
                           else "is healthy again"),
        ))
```

(Add `insert_alert` to the existing `from db.database import ...` block in `main.py`.)

`bot/telegram.py` — add to `ALERT_TYPE_EMOJI`: `"collector_stale": "🩺",`.

`web/routes.py` — add endpoint before `return app`:

```python
    @app.get("/api/health")
    async def api_health():
        from dataclasses import asdict
        from engine.health import compute_collector_health
        return [asdict(h) for h in await compute_collector_health(db)]
```

and in `dashboard()`, add collector health to the context:

```python
        from engine.health import compute_collector_health
        collector_health = await compute_collector_health(db)
```

pass `"collector_health": collector_health,` into the `index.html` TemplateResponse context.

`web/templates/index.html` — inside the existing data-health strip (after the milestone item ending at line ~119), add one item per collector:

```html
{% for h in collector_health %}
<div class="health-item">
  <span class="health-label">{{ h.name }}</span>
  <span class="health-value {{ 'bad' if h.stale else 'good' }}"
        title="{{ h.reasons | join('; ') if h.reasons else 'ok' }}">
    {{ '⛔' if h.stale else '✓' }}
  </span>
</div>
{% endfor %}
```

(Match the surrounding markup — read lines 85–120 of the template first and copy the exact `health-item` wrapper structure used there.)

- [ ] **Step 5.6: Route test for /api/health**

Append to `tests/web/test_routes.py` (same fixture style as Step 2.6):

```python
@pytest.mark.asyncio
async def test_api_health_reports_collectors(client_and_db):
    client, db = client_and_db
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    names = {c["name"] for c in resp.json()}
    assert names == {"chain", "github", "milestone"}
```

Run: `python -m pytest tests/ -q` — expected: all PASS.

- [ ] **Step 5.7: Commit**

```bash
git add -A
git commit -m "feat: collector health panel and stale-collector conditions"
```

---

## Task 6: Data hygiene — backup-on-change + retention downsampling

Two fixes: (1) `init_db` backs up on every process start (5 `.bak` files on 2026-07-01 = 5 restarts); back up only when the schema will actually change. (2) **Urgent:** replace the daily `prune_old_snapshots(days=30)` hard delete — which would begin destroying calibration history on ~2026-07-15 — with downsampling rows older than 90 days to 1/subnet/hour.

**Files:**
- Modify: `db/database.py`, `main.py`, `config.py`
- Tests: `tests/test_db_backup.py`, `tests/db/test_retention.py` (new)

- [ ] **Step 6.1: Failing test — no backup when schema is current**

Add to `tests/test_db_backup.py` (match its existing style — it already tests `backup_db_file`):

```python
@pytest.mark.asyncio
async def test_no_backup_when_schema_current(tmp_path):
    """Second init_db on an up-to-date DB must not create a new .bak."""
    import glob
    db_path = str(tmp_path / "m.db")
    db = await init_db(db_path)
    await db.close()
    assert glob.glob(f"{db_path}.*.bak") == []   # first init: empty file, no backup
    db = await init_db(db_path)
    await db.close()
    assert glob.glob(f"{db_path}.*.bak") == []   # re-init, schema unchanged: still none
```

Run: `python -m pytest tests/test_db_backup.py -q` — expected: the new test FAILS (a `.bak` appears on the second init).

- [ ] **Step 6.2: Implement schema-change detection**

In `db/database.py`, add above `init_db`:

```python
# Column names init_db() knows how to add via ALTER TABLE. Kept next to the
# migration lists below — extend BOTH when adding a migration.
_EXPECTED_TABLES = {
    "snapshots", "alerts", "subnet_registry", "portfolio_positions",
    "analyst_watchlist", "analyst_mentions", "subnet_milestones",
    "collector_state", "condition_states",
}
_EXPECTED_SNAPSHOT_COLS = {
    "hype_score", "net_tao_flow_tao", "max_allowed_uids", "tao_in_tao",
    "buy_slippage_pct", "sell_slippage_pct", "flow_score",
    "relative_value_score", "tradability_score", "catalyst_score",
    "risk_penalty", "swing_score", "reg_demand_score", "slot_fill_score",
    "flow_accel_score", "emergence_score", "emergence_stage",
    "price_ema_score", "emission_value_score", "protocol_context_score",
    "spec421_score", "health_score",
}
_EXPECTED_REGISTRY_COLS = {"category", "category_confirmed"}
_EXPECTED_MILESTONE_COLS = {"ai_summary", "ai_take"}


def schema_needs_migration(db_path: str) -> bool:
    """True if init_db would CREATE a table or ALTER one (→ worth a backup)."""
    if not os.path.exists(db_path) or os.path.getsize(db_path) == 0:
        return False   # fresh DB: nothing to protect
    conn = sqlite3.connect(db_path)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not _EXPECTED_TABLES <= tables:
            return True
        def cols(table: str) -> set[str]:
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not _EXPECTED_SNAPSHOT_COLS <= cols("snapshots"):
            return True
        if not _EXPECTED_REGISTRY_COLS <= cols("subnet_registry"):
            return True
        if not _EXPECTED_MILESTONE_COLS <= cols("subnet_milestones"):
            return True
        return False
    finally:
        conn.close()
```

Then in `init_db`, replace the unconditional backup:

```python
    if schema_needs_migration(db_path):
        backup_path = backup_db_file(db_path)
        if backup_path:
            logger.info("[DB] pre-migration backup -> %s", backup_path)
```

Run: `python -m pytest tests/test_db_backup.py tests/test_db_schema.py -q` — expected: PASS.

- [ ] **Step 6.3: Failing test — retention downsampling**

Create `tests/db/test_retention.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from db.database import downsample_old_snapshots, init_db, insert_snapshot
from models import SubnetSnapshot


@pytest.mark.asyncio
async def test_downsample_thins_old_rows_keeps_recent(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        # Pin to the top of the hour: bucket counts must not depend on the
        # wall-clock minute the test happens to run at.
        anchor = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        base_old = anchor - timedelta(days=100)
        base_new = anchor - timedelta(days=5)
        for base, n in ((base_old, 8), (base_new, 8)):   # 8 rows over 2 hours, 4/hour
            for i in range(n):
                await insert_snapshot(db, SubnetSnapshot(
                    netuid=3, polled_at=base + timedelta(minutes=15 * i)))
        deleted = await downsample_old_snapshots(db, older_than_days=90)
        assert deleted == 6   # old: 8 rows in 2 hour-buckets → keep 2
        cur = await db.execute(
            "SELECT COUNT(*) FROM snapshots WHERE polled_at < ?",
            ((datetime.now(timezone.utc) - timedelta(days=90)).isoformat(),))
        assert (await cur.fetchone())[0] == 2
        cur = await db.execute("SELECT COUNT(*) FROM snapshots")
        assert (await cur.fetchone())[0] == 10   # recent 8 untouched
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_downsample_keeps_one_row_per_subnet_per_hour(tmp_path):
    db = await init_db(str(tmp_path / "t.db"))
    try:
        base = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
                - timedelta(days=100))
        for netuid in (1, 2):
            for i in range(4):
                await insert_snapshot(db, SubnetSnapshot(
                    netuid=netuid, polled_at=base + timedelta(minutes=15 * i)))
        await downsample_old_snapshots(db, older_than_days=90)
        cur = await db.execute("SELECT netuid, COUNT(*) FROM snapshots GROUP BY netuid")
        assert {tuple(r) for r in await cur.fetchall()} == {(1, 1), (2, 1)}
    finally:
        await db.close()
```

Run: `python -m pytest tests/db/test_retention.py -q`
Expected: FAIL with `ImportError: cannot import name 'downsample_old_snapshots'`

- [ ] **Step 6.4: Implement downsampling; retire the 30-day hard delete**

In `db/database.py`, replace `prune_old_snapshots` with:

```python
async def downsample_old_snapshots(db: aiosqlite.Connection,
                                   older_than_days: int = config.RETENTION_FULL_DAYS) -> int:
    """Thin snapshots older than the cutoff to 1 row per subnet per hour.

    Keeps the earliest row in each (netuid, hour) bucket; never deletes rows
    newer than the cutoff. Replaces the old prune_old_snapshots(days=30) hard
    delete, which would have destroyed swing-score calibration history.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
    cursor = await db.execute("""
        DELETE FROM snapshots
        WHERE polled_at < ?
          AND id NOT IN (
              SELECT MIN(id) FROM snapshots
              WHERE polled_at < ?
              GROUP BY netuid, substr(polled_at, 1, 13)
          )
    """, (cutoff, cutoff))
    await db.commit()
    deleted = cursor.rowcount
    logger.info("Downsampled %d snapshot rows older than %d days", deleted, older_than_days)
    return deleted
```

(`substr(polled_at, 1, 13)` = `YYYY-MM-DDTHH` — the hour bucket; all writes use Python `isoformat()` so the format is uniform.)

`config.py`:

```python
RETENTION_FULL_DAYS: int = 90    # full-resolution snapshot window; older rows downsampled to 1/subnet/hour
```

`main.py`: in the imports change `prune_old_snapshots` → `downsample_old_snapshots`, and in `registry_refresh_and_prune` replace `await prune_old_snapshots(_db, days=30)` with `await downsample_old_snapshots(_db)`.

Also in `db/database.py`: delete the duplicated second definition of `get_all_snapshots` (lines ~325–330 — two identical defs exist back-to-back; keep one).

- [ ] **Step 6.5: Run tests, fix `prune_old_snapshots` references**

Run: `python -m pytest tests/ -q`
`grep -rn "prune_old_snapshots" tests/ scripts/` and update any references to the new function. Expected: all PASS.

- [ ] **Step 6.6: Commit**

```bash
git add -A
git commit -m "fix: backup only on schema change; downsample instead of deleting history"
```

---

## Final verification

- [ ] **F.1:** `python -m pytest tests/ -q` — full suite green.
- [ ] **F.2:** Boot check against a **copy** of the live DB (never the live file):
  `cp data/monitor.db /tmp/monitor_check.db && TELEGRAM_BOT_TOKEN=x TELEGRAM_CHAT_ID=y DB_PATH=/tmp/monitor_check.db timeout 30 python -c "import asyncio; from db.database import init_db; asyncio.run(init_db('/tmp/monitor_check.db'))"` — expected: exits clean, `condition_states` table created, **no** new `.bak` beside `/tmp/monitor_check.db` on a second run.
- [ ] **F.3:** Update `TODOS.md`: mark the alert-noise problem addressed; add deferred items (registry backfill, slippage-null investigation) if not present.
- [ ] **F.4:** Use superpowers:finishing-a-development-branch to integrate.
