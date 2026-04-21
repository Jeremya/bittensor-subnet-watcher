# Analyst Tracking, Milestone Detection & Subnet Categories — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add analyst X-account tracking, arXiv/HuggingFace milestone detection with AI summaries, subnet category labels, and a multi-signal convergence alert to surface the "cognitive arbitrage" window described in the spec.

**Architecture:** Three new collectors (AnalystCollector, MilestoneCollector) feed into new DB tables; alerts.py gains three new check functions; main.py schedules the collectors; the dashboard gains a category sidebar filter, coverage badges, and a new /analysts management page.

**Tech Stack:** Python 3.11, aiosqlite, aiohttp, Playwright (reused from x_scraper), huggingface_hub, anthropic SDK, FastAPI + Jinja2 (existing dashboard stack).

---

## File Map

**New files:**
- `collectors/analyst.py` — AnalystCollector: scrapes X analyst accounts, matches tweet text to subnets
- `collectors/milestone.py` — MilestoneCollector: polls arXiv + HuggingFace; calls Claude API for AI summaries
- `tests/test_analyst_matching.py` — unit tests for subnet name/SN-pattern matching logic
- `tests/test_category_suggest.py` — unit tests for README keyword → category mapping
- `tests/test_convergence.py` — unit tests for convergence signal counting logic
- `web/templates/analysts.html` — Analysts watchlist management page

**Modified files:**
- `requirements.txt` — add huggingface_hub, anthropic
- `.env.example` — add ANALYST_HANDLES, ANTHROPIC_API_KEY
- `config.py` — add 7 new config vars
- `db/database.py` — extend SCHEMA_SQL (4 new tables, 2 new registry columns), add migration, add 12 new helper functions
- `collectors/github.py` — add `fetch_readme()`, `suggest_category()`, update `collect()` to return category data
- `engine/alerts.py` — add `fire_analyst_alerts()`, `fire_milestone_alerts()`, `evaluate_convergence()`
- `main.py` — schedule 2 new jobs, wire convergence into poll_cycle
- `web/routes.py` — add /analysts GET/POST, enrich dashboard + detail with new data
- `web/templates/index.html` — category filter sidebar, coverage badge column
- `web/templates/subnet.html` — milestone timeline, analyst mentions feed, category dropdown

---

## Task 1: Dependencies and Environment

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`

- [ ] **Step 1: Add new packages to requirements.txt**

```text
# add after httpx:
huggingface_hub>=0.22.0
anthropic>=0.25.0
```

- [ ] **Step 2: Add new env vars to .env.example**

```text
# add at the bottom:
ANALYST_HANDLES=0xai_dev,taostats
ANTHROPIC_API_KEY=
```

- [ ] **Step 3: Install new packages**

```bash
pip install huggingface_hub>=0.22.0 anthropic>=0.25.0
```

Expected: both packages install without error.

- [ ] **Step 4: Commit**

```bash
git add requirements.txt .env.example
git commit -m "chore: add huggingface_hub and anthropic dependencies"
```

---

## Task 2: Config Additions

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Add new config vars to config.py**

Add the following block after the `BITTENSOR_NETWORK` block at the bottom of config.py:

```python
# ── Analyst tracking ──────────────────────────────────────────────────────────
ANALYST_HANDLES: list[str] = [
    h.strip() for h in os.getenv("ANALYST_HANDLES", "").split(",") if h.strip()
]
ANALYST_TWEET_LOOKBACK_HOURS: int = 25   # slightly > poll interval to avoid gaps
ANALYST_COVERAGE_DECAY_HOURS: int = 72   # coverage badge visible for 72h after mention

# ── AI Signal Interpreter ──────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
AI_INTERPRETER_MODEL: str = "claude-haiku-4-5-20251001"

# ── Signal Convergence ────────────────────────────────────────────────────────
CONVERGENCE_SIGNAL_WINDOW_HOURS: int = 24   # look back this many hours for signal grouping
CONVERGENCE_MIN_SIGNALS: int = 2            # distinct signal types needed to fire
CONVERGENCE_COOLDOWN_HOURS: int = 48        # separate cooldown from standard 6h
```

- [ ] **Step 2: Verify config loads cleanly**

```bash
python -c "import config; print('analyst_handles:', config.ANALYST_HANDLES)"
```

Expected output: `analyst_handles: []` (empty list if env var not set — that's correct).

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add analyst, AI interpreter, and convergence config vars"
```

---

## Task 3: DB Schema, Migrations, and Helper Functions

**Files:**
- Modify: `db/database.py`

- [ ] **Step 1: Write failing test for new tables existing after init_db**

Create `tests/test_db_schema.py`:

```python
import asyncio
import pytest
import aiosqlite
from db.database import init_db

@pytest.mark.asyncio
async def test_new_tables_exist(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert "analyst_watchlist" in tables
    assert "analyst_mentions" in tables
    assert "subnet_milestones" in tables
    assert "collector_state" in tables
    await db.close()

@pytest.mark.asyncio
async def test_registry_has_category_columns(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = await init_db(db_path)
    cursor = await db.execute("PRAGMA table_info(subnet_registry)")
    cols = {row[1] for row in await cursor.fetchall()}
    assert "category" in cols
    assert "category_confirmed" in cols
    await db.close()
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
pytest tests/test_db_schema.py -v
```

Expected: FAIL — tables don't exist yet.

- [ ] **Step 3: Add new tables to SCHEMA_SQL in db/database.py**

In `db/database.py`, append the following to the end of the `SCHEMA_SQL` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS analyst_watchlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    handle    TEXT NOT NULL UNIQUE,
    added_at  TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'dashboard'
);

CREATE TABLE IF NOT EXISTS analyst_mentions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    analyst_handle TEXT NOT NULL,
    netuid         INTEGER NOT NULL,
    tweet_url      TEXT NOT NULL UNIQUE,
    tweet_text     TEXT,
    mentioned_at   TEXT NOT NULL,
    notified       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subnet_milestones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid         INTEGER NOT NULL,
    milestone_type TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL UNIQUE,
    published_at   TEXT NOT NULL,
    ai_summary     TEXT,
    ai_take        TEXT,
    notified       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS collector_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_analyst_mentions_netuid ON analyst_mentions (netuid, mentioned_at);
CREATE INDEX IF NOT EXISTS idx_milestones_netuid ON subnet_milestones (netuid, published_at);
```

- [ ] **Step 4: Add registry column migrations to init_db()**

In `init_db()`, after the existing `await conn.commit()` at the end of the migration block (after the health_score migration), add:

```python
    # Migrate subnet_registry: add category columns introduced in analyst/milestone release
    cursor = await conn.execute("PRAGMA table_info(subnet_registry)")
    registry_cols = {row[1] for row in await cursor.fetchall()}
    for col, definition in [
        ("category", "TEXT"),
        ("category_confirmed", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in registry_cols:
            await conn.execute(f"ALTER TABLE subnet_registry ADD COLUMN {col} {definition}")
    await conn.commit()
```

- [ ] **Step 5: Add new DB helper functions to db/database.py**

Append the following functions to the bottom of `db/database.py`:

```python
# ── Analyst watchlist ─────────────────────────────────────────────────────────

async def get_analyst_watchlist(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM analyst_watchlist ORDER BY added_at DESC"
    )
    return await cursor.fetchall()


async def add_analyst_handle(db: aiosqlite.Connection,
                              handle: str,
                              source: str = "dashboard") -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("""
        INSERT OR IGNORE INTO analyst_watchlist (handle, added_at, source)
        VALUES (?, ?, ?)
    """, (handle.lstrip("@"), now, source))
    await db.commit()


async def remove_analyst_handle(db: aiosqlite.Connection, handle: str) -> None:
    await db.execute(
        "DELETE FROM analyst_watchlist WHERE handle = ? AND source = 'dashboard'",
        (handle.lstrip("@"),)
    )
    await db.commit()


# ── Analyst mentions ──────────────────────────────────────────────────────────

async def insert_analyst_mention(db: aiosqlite.Connection,
                                  handle: str, netuid: int,
                                  tweet_url: str, tweet_text: str,
                                  mentioned_at: datetime) -> bool:
    """Insert a new analyst mention. Returns True if newly inserted, False if duplicate."""
    try:
        await db.execute("""
            INSERT INTO analyst_mentions (analyst_handle, netuid, tweet_url, tweet_text, mentioned_at)
            VALUES (?, ?, ?, ?, ?)
        """, (handle, netuid, tweet_url, tweet_text, mentioned_at.isoformat()))
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_unnotified_analyst_mentions(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM analyst_mentions WHERE notified=0 ORDER BY mentioned_at ASC"
    )
    return await cursor.fetchall()


async def mark_analyst_mentions_notified(db: aiosqlite.Connection, ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE analyst_mentions SET notified=1 WHERE id IN ({placeholders})", ids
    )
    await db.commit()


async def get_analyst_mentions_for_netuid(db: aiosqlite.Connection,
                                           netuid: int,
                                           limit: int = 10) -> list[aiosqlite.Row]:
    cursor = await db.execute("""
        SELECT * FROM analyst_mentions WHERE netuid=?
        ORDER BY mentioned_at DESC LIMIT ?
    """, (netuid, limit))
    return await cursor.fetchall()


async def has_active_analyst_coverage(db: aiosqlite.Connection,
                                       netuid: int,
                                       decay_hours: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=decay_hours)).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM analyst_mentions WHERE netuid=? AND mentioned_at > ?",
        (netuid, cutoff)
    )
    row = await cursor.fetchone()
    return row[0] > 0


# ── Subnet milestones ─────────────────────────────────────────────────────────

async def insert_milestone(db: aiosqlite.Connection,
                            netuid: int, milestone_type: str,
                            title: str, url: str, published_at: datetime,
                            ai_summary: Optional[str] = None,
                            ai_take: Optional[str] = None) -> bool:
    """Insert a new milestone. Returns True if newly inserted, False if duplicate URL."""
    try:
        await db.execute("""
            INSERT INTO subnet_milestones
                (netuid, milestone_type, title, url, published_at, ai_summary, ai_take)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (netuid, milestone_type, title, url, published_at.isoformat(),
              ai_summary, ai_take))
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_unnotified_milestones(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM subnet_milestones WHERE notified=0 ORDER BY published_at ASC"
    )
    return await cursor.fetchall()


async def mark_milestones_notified(db: aiosqlite.Connection, ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE subnet_milestones SET notified=1 WHERE id IN ({placeholders})", ids
    )
    await db.commit()


async def get_milestones_for_netuid(db: aiosqlite.Connection,
                                     netuid: int,
                                     limit: int = 10) -> list[aiosqlite.Row]:
    cursor = await db.execute("""
        SELECT * FROM subnet_milestones WHERE netuid=?
        ORDER BY published_at DESC LIMIT ?
    """, (netuid, limit))
    return await cursor.fetchall()


# ── Collector state ───────────────────────────────────────────────────────────

async def get_collector_state(db: aiosqlite.Connection, key: str) -> Optional[str]:
    cursor = await db.execute(
        "SELECT value FROM collector_state WHERE key=?", (key,)
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_collector_state(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute("""
        INSERT INTO collector_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """, (key, value))
    await db.commit()


# ── Registry category ─────────────────────────────────────────────────────────

async def update_registry_category(db: aiosqlite.Connection,
                                    netuid: int, category: str,
                                    confirmed: bool = False) -> None:
    """Update category. If confirmed=False, only writes when not already confirmed."""
    if confirmed:
        await db.execute("""
            UPDATE subnet_registry SET category=?, category_confirmed=1 WHERE netuid=?
        """, (category, netuid))
    else:
        await db.execute("""
            UPDATE subnet_registry SET category=?
            WHERE netuid=? AND (category_confirmed IS NULL OR category_confirmed=0)
        """, (category, netuid))
    await db.commit()


# ── Convergence signal query ──────────────────────────────────────────────────

async def get_recent_alert_types_per_netuid(db: aiosqlite.Connection,
                                             alert_types: list[str],
                                             hours: int) -> dict[int, set[str]]:
    """Return {netuid: set of distinct alert_types} fired within the last `hours` hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    placeholders = ",".join("?" * len(alert_types))
    cursor = await db.execute(
        f"SELECT netuid, alert_type FROM alerts WHERE alert_type IN ({placeholders}) AND fired_at > ?",
        (*alert_types, cutoff)
    )
    rows = await cursor.fetchall()
    result: dict[int, set[str]] = {}
    for row in rows:
        result.setdefault(row["netuid"], set()).add(row["alert_type"])
    return result
```

- [ ] **Step 6: Run schema tests to confirm they pass**

```bash
pytest tests/test_db_schema.py -v
```

Expected: PASS (2 tests).

- [ ] **Step 7: Commit**

```bash
git add db/database.py tests/test_db_schema.py
git commit -m "feat: add analyst/milestone/category DB schema and helper functions"
```

---

## Task 4: Analyst Subnet Matching Logic + Tests

**Files:**
- Create: `tests/test_analyst_matching.py`
- Create: `collectors/analyst.py` (matching functions only — scraping added in Task 5)

- [ ] **Step 1: Write failing tests for match_subnets()**

Create `tests/test_analyst_matching.py`:

```python
import pytest
from collectors.analyst import match_subnets

# Minimal registry stub
REGISTRY = {
    3:  {"name": "Templar"},
    56: {"name": "Gradients"},
    13: {"name": "Macrocosmos"},
}


def test_matches_sn_pattern():
    result = match_subnets("SN3 is going to pump hard", REGISTRY)
    assert result == {3}


def test_matches_sn_pattern_case_insensitive():
    result = match_subnets("watching sn56 closely", REGISTRY)
    assert result == {56}


def test_matches_subnet_name():
    result = match_subnets("Templar shipped a new model today", REGISTRY)
    assert result == {3}


def test_matches_subnet_name_case_insensitive():
    result = match_subnets("templar is doing great things", REGISTRY)
    assert result == {3}


def test_matches_multiple_subnets():
    result = match_subnets("SN3 and Gradients are my top picks", REGISTRY)
    assert result == {3, 56}


def test_no_match_returns_empty():
    result = match_subnets("Bitcoin is going to 100k", REGISTRY)
    assert result == set()


def test_sn_pattern_not_in_registry_ignored():
    result = match_subnets("SN999 is unknown", REGISTRY)
    assert result == set()


def test_partial_name_not_matched():
    # "Grad" should not match "Gradients" — whole-word boundary required
    result = match_subnets("Grad is a word but not a subnet", REGISTRY)
    assert result == set()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_analyst_matching.py -v
```

Expected: FAIL — `collectors.analyst` not found.

- [ ] **Step 3: Create collectors/analyst.py with matching logic**

```python
# collectors/analyst.py
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Optional
import aiosqlite
from collectors.x_scraper import get_browser_page
import config

logger = logging.getLogger(__name__)

_SN_PATTERN = re.compile(r'\bSN(\d+)\b', re.IGNORECASE)


def _name_patterns(registry: dict) -> list[tuple[int, re.Pattern]]:
    """Compile a whole-word regex pattern for each subnet name in the registry."""
    patterns = []
    for netuid, row in registry.items():
        name = row["name"] if isinstance(row, dict) else getattr(row, "name", None)
        if name:
            patterns.append((netuid, re.compile(rf'\b{re.escape(name)}\b', re.IGNORECASE)))
    return patterns


def match_subnets(text: str, registry: dict) -> set[int]:
    """Return set of netuids mentioned in text via SN{n} pattern or subnet name."""
    matched: set[int] = set()
    for m in _SN_PATTERN.finditer(text):
        netuid = int(m.group(1))
        if netuid in registry:
            matched.add(netuid)
    for netuid, pattern in _name_patterns(registry):
        if pattern.search(text):
            matched.add(netuid)
    return matched
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
pytest tests/test_analyst_matching.py -v
```

Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add collectors/analyst.py tests/test_analyst_matching.py
git commit -m "feat: add analyst subnet matching logic with tests"
```

---

## Task 5: Analyst Collector — Scraping and DB Integration

**Files:**
- Modify: `collectors/analyst.py` (add `AnalystCollector` class)

- [ ] **Step 1: Add AnalystCollector class to collectors/analyst.py**

Append to `collectors/analyst.py` after the existing `match_subnets` function:

```python
async def _scrape_tweets(handle: str, lookback_hours: int) -> list[dict]:
    """
    Scrape up to 10 recent tweets for `handle` within `lookback_hours`.
    Returns list of {url: str, text: str, posted_at: datetime}.
    Silently returns [] on any failure — same best-effort pattern as XCollector.
    """
    page = None
    tweets = []
    cutoff_ts = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    try:
        page = await get_browser_page()
        await page.goto(f"https://x.com/{handle}",
                        wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_selector('[data-testid="primaryColumn"]', timeout=10_000)

        articles = await page.query_selector_all('article')
        for article in articles[:10]:
            time_el = await article.query_selector('time')
            if not time_el:
                continue
            dt_str = await time_el.get_attribute("datetime")
            if not dt_str:
                continue
            posted_at = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if posted_at.timestamp() < cutoff_ts:
                continue

            text_el = await article.query_selector('[data-testid="tweetText"]')
            text = await text_el.text_content() if text_el else ""

            link_el = await time_el.evaluate_handle("el => el.closest('a')")
            href = await link_el.get_attribute("href") if link_el else None
            url = f"https://x.com{href}" if href and href.startswith("/") else href

            if url and text:
                tweets.append({"url": url, "text": text, "posted_at": posted_at})

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[COLLECTOR] analyst: handle=%s error=%s", handle, exc)
    finally:
        if page:
            try:
                await page.context.close()
            except Exception:
                pass
    return tweets


class AnalystCollector:
    @staticmethod
    async def _all_handles(db: aiosqlite.Connection) -> list[str]:
        """Union of ANALYST_HANDLES config and DB analyst_watchlist."""
        from db.database import get_analyst_watchlist
        db_rows = await get_analyst_watchlist(db)
        db_handles = {row["handle"] for row in db_rows}
        return list(set(config.ANALYST_HANDLES) | db_handles)

    @staticmethod
    async def collect(db: aiosqlite.Connection, registry: dict) -> int:
        """
        Scrape analyst handles, match tweets to subnets, insert new analyst_mentions.
        Returns count of newly inserted mentions.
        """
        from db.database import insert_analyst_mention
        handles = await AnalystCollector._all_handles(db)
        if not handles:
            logger.info("[COLLECTOR] name=analyst no_handles_configured")
            return 0

        new_count = 0
        for handle in handles:
            tweets = await _scrape_tweets(handle, config.ANALYST_TWEET_LOOKBACK_HOURS)
            for tweet in tweets:
                for netuid in match_subnets(tweet["text"], registry):
                    inserted = await insert_analyst_mention(
                        db, handle, netuid,
                        tweet["url"], tweet["text"], tweet["posted_at"]
                    )
                    if inserted:
                        new_count += 1
            await asyncio.sleep(2.0)

        logger.info("[COLLECTOR] name=analyst new_mentions=%d handles=%d",
                    new_count, len(handles))
        return new_count
```

- [ ] **Step 2: Verify existing matching tests still pass**

```bash
pytest tests/test_analyst_matching.py -v
```

Expected: PASS (8 tests).

- [ ] **Step 3: Commit**

```bash
git add collectors/analyst.py
git commit -m "feat: add AnalystCollector — scrapes X handles and inserts subnet mentions"
```

---

## Task 6: GitHub README Category Suggest + Tests

**Files:**
- Create: `tests/test_category_suggest.py`
- Modify: `collectors/github.py`

- [ ] **Step 1: Write failing tests for suggest_category()**

Create `tests/test_category_suggest.py`:

```python
import pytest
from collectors.github import suggest_category

def test_ai_training_keyword():
    assert suggest_category("We are training a large language model with distributed gradients") == "AI Training"

def test_rlhf_keyword():
    assert suggest_category("Our subnet implements RLHF alignment fine-tuning") == "Post-Training/RLHF"

def test_quant_keyword():
    assert suggest_category("This subnet provides alpha signals for trading strategies") == "Quant / Finance"

def test_biomedical_keyword():
    assert suggest_category("Protein structure prediction using distributed compute") == "Biomedical"

def test_data_retrieval_keyword():
    assert suggest_category("Decentralized dataset indexing and retrieval network") == "Data / Retrieval"

def test_infrastructure_keyword():
    assert suggest_category("Bandwidth and networking layer for validator communication") == "Infrastructure"

def test_privacy_keyword():
    assert suggest_category("Zero-knowledge proof computation across miners") == "Privacy / Compute"

def test_unknown_returns_other():
    assert suggest_category("This subnet does something vague and undefined") == "Other"

def test_empty_readme_returns_other():
    assert suggest_category("") == "Other"

def test_case_insensitive():
    assert suggest_category("TRAINING a model with GRADIENT compression") == "AI Training"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_category_suggest.py -v
```

Expected: FAIL — `suggest_category` not found in `collectors.github`.

- [ ] **Step 3: Add suggest_category() and fetch_readme() to collectors/github.py**

Add these functions to `collectors/github.py` after the existing `parse_github_url` function:

```python
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "AI Training":        ["training", "finetune", "fine-tune", "gradient", "pretraining", "pre-training", "llm"],
    "Post-Training/RLHF": ["rlhf", "alignment", "reinforcement", "reward model"],
    "Data / Retrieval":   ["dataset", "scraping", "retrieval", "indexing", "storage"],
    "Quant / Finance":    ["trading", "quant", "alpha", "market", "prediction"],
    "Privacy / Compute":  ["zkp", "zero-knowledge", "zero knowledge", "secure compute", "homomorphic"],
    "Biomedical":         ["biomedical", "protein", "genomics", "drug", "clinical"],
    "Infrastructure":     ["bandwidth", "networking", "storage", "compute", "validator"],
}


def suggest_category(readme_text: str) -> str:
    """Return the first category whose keywords appear in readme_text, else 'Other'."""
    lower = readme_text.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return category
    return "Other"


async def fetch_readme(owner: str, repo: str) -> Optional[str]:
    """
    Fetch raw README text from GitHub. Uses raw.githubusercontent.com (no API quota).
    Tries main, then master branch. Returns None on failure.
    """
    for branch in ("main", "master"):
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/README.md"
        try:
            async with aiohttp_session() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        return await resp.text()
        except Exception:
            pass
    return None
```

- [ ] **Step 4: Update GitHubCollector.collect() to also return category data**

Replace the existing `collect()` method in `collectors/github.py` with:

```python
    @staticmethod
    async def collect(registry: dict) -> dict[int, dict]:
        """
        Fetch GitHub data for all subnets in registry that have a github_url.
        Returns {netuid: data_dict} where data_dict includes gh_* keys and optionally 'category'.
        Note: runs sequentially to respect rate limits (60 req/hr unauthenticated).
        """
        results: dict[int, dict] = {}
        for netuid, row in registry.items():
            github_url = row["github_url"] if row["github_url"] else None
            parsed = parse_github_url(github_url)
            if not parsed:
                continue
            owner, repo = parsed
            data = await GitHubCollector.fetch_repo(owner, repo)
            if data is not None:
                # Fetch README and suggest category (best-effort — failure leaves category absent)
                readme = await fetch_readme(owner, repo)
                if readme:
                    data["category"] = suggest_category(readme)
                results[netuid] = data

        ok = len(results)
        total = sum(1 for r in registry.values() if r["github_url"])
        logger.info("[COLLECTOR] name=github ok=%d errors=%d", ok, total - ok)
        return results
```

- [ ] **Step 5: Run category tests to confirm they pass**

```bash
pytest tests/test_category_suggest.py -v
```

Expected: PASS (10 tests).

- [ ] **Step 6: Commit**

```bash
git add collectors/github.py tests/test_category_suggest.py
git commit -m "feat: add GitHub README category auto-suggest with tests"
```

---

## Task 7: Milestone Collector (arXiv + HuggingFace + AI Interpreter)

**Files:**
- Create: `collectors/milestone.py`
- Create: `tests/test_milestone_collector.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_milestone_collector.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime, timezone
from collectors.milestone import parse_arxiv_feed, interpret_milestone


def test_parse_arxiv_feed_extracts_entries():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>SparseLoCo: Gradient Compression</title>
        <id>http://arxiv.org/abs/2603.08163v1</id>
        <published>2026-03-10T12:00:00Z</published>
      </entry>
    </feed>"""
    entries = parse_arxiv_feed(xml)
    assert len(entries) == 1
    assert entries[0]["title"] == "SparseLoCo: Gradient Compression"
    assert entries[0]["url"] == "https://arxiv.org/abs/2603.08163"
    assert entries[0]["published_at"].year == 2026


def test_parse_arxiv_feed_strips_version_suffix():
    xml = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Test Paper</title>
        <id>http://arxiv.org/abs/1234.56789v3</id>
        <published>2026-01-01T00:00:00Z</published>
      </entry>
    </feed>"""
    entries = parse_arxiv_feed(xml)
    assert entries[0]["url"] == "https://arxiv.org/abs/1234.56789"


def test_parse_arxiv_feed_returns_empty_on_bad_xml():
    entries = parse_arxiv_feed("this is not xml")
    assert entries == []


@pytest.mark.asyncio
async def test_interpret_milestone_returns_none_when_no_api_key():
    with patch("collectors.milestone.config") as mock_cfg:
        mock_cfg.ANTHROPIC_API_KEY = ""
        result = await interpret_milestone("Templar", 3, "arxiv", "Test Paper", "http://example.com")
    assert result == (None, None)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_milestone_collector.py -v
```

Expected: FAIL — `collectors.milestone` not found.

- [ ] **Step 3: Create collectors/milestone.py**

```python
# collectors/milestone.py
import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional
import aiohttp
import aiosqlite
from utils import aiohttp_session
import config

logger = logging.getLogger(__name__)

ARXIV_API = "http://export.arxiv.org/api/query"
_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
_VERSION_RE = re.compile(r'v\d+$')


def parse_arxiv_feed(xml_text: str) -> list[dict]:
    """
    Parse arXiv Atom XML into list of {title, url, published_at}.
    Returns [] on any parse error.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        title_el = entry.find("atom:title", _ARXIV_NS)
        id_el = entry.find("atom:id", _ARXIV_NS)
        pub_el = entry.find("atom:published", _ARXIV_NS)
        if title_el is None or id_el is None or pub_el is None:
            continue

        # Normalise URL: strip version suffix, use https
        raw_id = id_el.text.strip()
        url = _VERSION_RE.sub("", raw_id).replace("http://arxiv.org/abs/", "https://arxiv.org/abs/")

        try:
            published_at = datetime.fromisoformat(pub_el.text.strip().replace("Z", "+00:00"))
        except ValueError:
            continue

        entries.append({
            "title": title_el.text.strip(),
            "url": url,
            "published_at": published_at,
        })
    return entries


async def interpret_milestone(subnet_name: str, netuid: int,
                               milestone_type: str, title: str,
                               url: str) -> tuple[Optional[str], Optional[str]]:
    """
    Call Claude Haiku to generate (summary, take) for a new milestone.
    Returns (None, None) if ANTHROPIC_API_KEY is not configured or on any error.
    """
    if not config.ANTHROPIC_API_KEY:
        return None, None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        prompt = (
            f"You are a Bittensor investment analyst. Given a new publication from a "
            f"Bittensor subnet team, write two things:\n"
            f"1. SUMMARY: 1-2 sentences explaining what was published in plain English "
            f"for a non-technical investor.\n"
            f"2. TAKE: 1 sentence on what this means for the subnet's investment thesis.\n\n"
            f"Subnet: {subnet_name} (SN{netuid})\n"
            f"Publication type: {milestone_type}\n"
            f"Title: {title}\n"
            f"URL: {url}\n\n"
            f'Reply in JSON only: {{"summary": "...", "take": "..."}}'
        )
        response = client.messages.create(
            model=config.AI_INTERPRETER_MODEL,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(response.content[0].text)
        return data.get("summary"), data.get("take")
    except Exception as exc:
        logger.warning("[COLLECTOR] milestone: AI interpret failed title=%r error=%s", title, exc)
        return None, None


class MilestoneCollector:
    @staticmethod
    async def _query_arxiv(subnet_name: str, since_iso: Optional[str]) -> list[dict]:
        """Search arXiv for papers mentioning bittensor + subnet_name."""
        query = f'all:"bittensor" AND all:"{subnet_name}"'
        params = {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": "5",
        }
        try:
            async with aiohttp_session() as session:
                async with session.get(
                    ARXIV_API, params=params,
                    timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        return []
                    text = await resp.text()
        except Exception as exc:
            logger.warning("[COLLECTOR] milestone: arxiv failed subnet=%r error=%s", subnet_name, exc)
            return []

        entries = parse_arxiv_feed(text)
        if since_iso:
            entries = [e for e in entries if e["published_at"].isoformat() > since_iso]
        return entries

    @staticmethod
    async def _query_huggingface(subnet_name: str, since_iso: Optional[str]) -> list[dict]:
        """Search HuggingFace Hub for models mentioning subnet_name + bittensor."""
        try:
            from huggingface_hub import HfApi
            api = HfApi()
            models = list(api.list_models(
                search=f"{subnet_name} bittensor",
                limit=5,
                sort="lastModified",
                direction=-1,
            ))
        except Exception as exc:
            logger.warning("[COLLECTOR] milestone: hf failed subnet=%r error=%s", subnet_name, exc)
            return []

        results = []
        for model in models:
            # lastModified is a datetime object from huggingface_hub
            last_mod = model.lastModified
            if last_mod is None:
                continue
            if last_mod.tzinfo is None:
                last_mod = last_mod.replace(tzinfo=timezone.utc)
            if since_iso and last_mod.isoformat() <= since_iso:
                continue
            url = f"https://huggingface.co/{model.id}"
            results.append({
                "title": model.id,
                "url": url,
                "published_at": last_mod,
            })
        return results

    @staticmethod
    async def collect(db: aiosqlite.Connection, registry: dict) -> int:
        """
        Poll arXiv and HuggingFace for each subnet with a github_url.
        Inserts new milestones with AI summaries. Returns count of newly inserted milestones.
        """
        from db.database import (insert_milestone, get_collector_state, set_collector_state)

        arxiv_since = await get_collector_state(db, "milestone_last_arxiv_check")
        hf_since = await get_collector_state(db, "milestone_last_hf_check")
        now_iso = datetime.now(timezone.utc).isoformat()

        new_count = 0
        subnets_with_repo = [
            (netuid, row)
            for netuid, row in registry.items()
            if (row["github_url"] if isinstance(row, dict) else getattr(row, "github_url", None))
        ]

        for netuid, row in subnets_with_repo:
            name = (row["name"] if isinstance(row, dict) else getattr(row, "name", None)) or f"SN{netuid}"

            # arXiv
            for entry in await MilestoneCollector._query_arxiv(name, arxiv_since):
                summary, take = await interpret_milestone(
                    name, netuid, "arxiv", entry["title"], entry["url"]
                )
                inserted = await insert_milestone(
                    db, netuid, "arxiv",
                    entry["title"], entry["url"], entry["published_at"],
                    ai_summary=summary, ai_take=take,
                )
                if inserted:
                    new_count += 1

            # HuggingFace
            for entry in await MilestoneCollector._query_huggingface(name, hf_since):
                summary, take = await interpret_milestone(
                    name, netuid, "huggingface", entry["title"], entry["url"]
                )
                inserted = await insert_milestone(
                    db, netuid, "huggingface",
                    entry["title"], entry["url"], entry["published_at"],
                    ai_summary=summary, ai_take=take,
                )
                if inserted:
                    new_count += 1

            await asyncio.sleep(1.0)  # be polite to arXiv

        await set_collector_state(db, "milestone_last_arxiv_check", now_iso)
        await set_collector_state(db, "milestone_last_hf_check", now_iso)

        logger.info("[COLLECTOR] name=milestone new=%d subnets_checked=%d",
                    new_count, len(subnets_with_repo))
        return new_count
```

- [ ] **Step 4: Run milestone tests**

```bash
pytest tests/test_milestone_collector.py -v
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add collectors/milestone.py tests/test_milestone_collector.py
git commit -m "feat: add MilestoneCollector with arXiv, HuggingFace, and AI interpreter"
```

---

## Task 8: Alert Checks — Analyst Mentions, Milestones, and Convergence

**Files:**
- Create: `tests/test_convergence.py`
- Modify: `engine/alerts.py`

- [ ] **Step 1: Write failing convergence tests**

Create `tests/test_convergence.py`:

```python
import pytest
from engine.alerts import _count_convergence_signals

CONVERGENCE_TYPES = {"milestone", "analyst_mention", "whale_inflow", "github_spike"}


def test_two_distinct_signals_triggers():
    signals_by_netuid = {3: {"milestone", "analyst_mention"}}
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 in result
    assert result[3] == {"milestone", "analyst_mention"}


def test_one_signal_does_not_trigger():
    signals_by_netuid = {3: {"milestone"}}
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 not in result


def test_three_signals_triggers():
    signals_by_netuid = {3: {"milestone", "analyst_mention", "whale_inflow"}}
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 in result


def test_multiple_netuids_filtered_correctly():
    signals_by_netuid = {
        3:  {"milestone", "analyst_mention"},
        56: {"github_spike"},
        13: {"whale_inflow", "milestone", "analyst_mention"},
    }
    result = _count_convergence_signals(signals_by_netuid, min_signals=2)
    assert 3 in result
    assert 56 not in result
    assert 13 in result


def test_empty_input_returns_empty():
    result = _count_convergence_signals({}, min_signals=2)
    assert result == {}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
pytest tests/test_convergence.py -v
```

Expected: FAIL — `_count_convergence_signals` not found.

- [ ] **Step 3: Add new functions to engine/alerts.py**

Append the following to the bottom of `engine/alerts.py`:

```python
# ── Convergence signal counting ───────────────────────────────────────────────

_CONVERGENCE_SIGNAL_TYPES = [
    "milestone", "analyst_mention", "whale_inflow", "github_spike",
]


def _count_convergence_signals(signals_by_netuid: dict[int, set[str]],
                                min_signals: int) -> dict[int, set[str]]:
    """
    Return {netuid: signal_types} for subnets with >= min_signals distinct types.
    Pure function — easy to unit test without DB.
    """
    return {
        netuid: types
        for netuid, types in signals_by_netuid.items()
        if len(types) >= min_signals
    }


async def fire_analyst_alerts(
    db: aiosqlite.Connection,
    registry: dict,
) -> list[AlertRecord]:
    """
    Convert unnotified analyst_mentions into AlertRecords in the alerts table.
    Marks mentions as notified so they're not processed twice.
    Returns newly inserted AlertRecords.
    """
    from db.database import (get_unnotified_analyst_mentions,
                              mark_analyst_mentions_notified, insert_alert)
    rows = await get_unnotified_analyst_mentions(db)
    fired: list[AlertRecord] = []
    notified_ids: list[int] = []

    for row in rows:
        netuid = row["netuid"]
        handle = row["analyst_handle"]
        text_preview = (row["tweet_text"] or "")[:120]
        if len(row["tweet_text"] or "") > 120:
            text_preview += "…"

        subnet_name = _registry_name(registry, netuid)
        alert = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=netuid,
            subnet_name=subnet_name,
            alert_type="analyst_mention",
            description=(
                f"@{handle} mentioned {subnet_name}: \"{text_preview}\"\n"
                f"→ {row['tweet_url']}"
            ),
            current_value=None,
            threshold=None,
        )
        in_cooldown = await is_alert_in_cooldown(
            db, netuid, "analyst_mention", config.ALERT_COOLDOWN_HOURS
        )
        if not in_cooldown:
            await insert_alert(db, alert)
            fired.append(alert)
            logger.info("[ALERT] analyst_mention netuid=%d handle=%s", netuid, handle)
        notified_ids.append(row["id"])

    await mark_analyst_mentions_notified(db, notified_ids)
    return fired


async def fire_milestone_alerts(
    db: aiosqlite.Connection,
    registry: dict,
) -> list[AlertRecord]:
    """
    Convert unnotified subnet_milestones into AlertRecords in the alerts table.
    Marks milestones as notified so they're not processed twice.
    Returns newly inserted AlertRecords.
    """
    from db.database import (get_unnotified_milestones,
                              mark_milestones_notified, insert_alert)
    rows = await get_unnotified_milestones(db)
    fired: list[AlertRecord] = []
    notified_ids: list[int] = []

    for row in rows:
        netuid = row["netuid"]
        subnet_name = _registry_name(registry, netuid)
        type_emoji = "🔬" if row["milestone_type"] == "arxiv" else "🤗"

        # Build description: include AI take if available
        desc_parts = [
            f"{type_emoji} {subnet_name} — new {row['milestone_type']}: {row['title']}",
        ]
        if row["ai_summary"]:
            desc_parts.append(f"Summary: {row['ai_summary']}")
        if row["ai_take"]:
            desc_parts.append(f"Take: {row['ai_take']}")
        desc_parts.append(f"→ {row['url']}")

        alert = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=netuid,
            subnet_name=subnet_name,
            alert_type="milestone",
            description="\n".join(desc_parts),
            current_value=None,
            threshold=None,
        )
        in_cooldown = await is_alert_in_cooldown(
            db, netuid, "milestone", config.ALERT_COOLDOWN_HOURS
        )
        if not in_cooldown:
            await insert_alert(db, alert)
            fired.append(alert)
            logger.info("[ALERT] milestone netuid=%d type=%s title=%r",
                        netuid, row["milestone_type"], row["title"])
        notified_ids.append(row["id"])

    await mark_milestones_notified(db, notified_ids)
    return fired


async def evaluate_convergence(
    db: aiosqlite.Connection,
    registry: dict,
) -> list[AlertRecord]:
    """
    Fire a high-conviction convergence alert when >= CONVERGENCE_MIN_SIGNALS distinct
    signal types hit the same subnet within CONVERGENCE_SIGNAL_WINDOW_HOURS.
    Uses a separate CONVERGENCE_COOLDOWN_HOURS cooldown.
    Returns newly fired convergence alerts.
    """
    from db.database import (get_recent_alert_types_per_netuid, insert_alert,
                              is_alert_in_cooldown)

    signals_by_netuid = await get_recent_alert_types_per_netuid(
        db, _CONVERGENCE_SIGNAL_TYPES, config.CONVERGENCE_SIGNAL_WINDOW_HOURS
    )
    triggered = _count_convergence_signals(
        signals_by_netuid, config.CONVERGENCE_MIN_SIGNALS
    )

    fired: list[AlertRecord] = []
    for netuid, signal_types in triggered.items():
        in_cooldown = await is_alert_in_cooldown(
            db, netuid, "convergence", config.CONVERGENCE_COOLDOWN_HOURS
        )
        if in_cooldown:
            continue

        subnet_name = _registry_name(registry, netuid)
        type_lines = "\n".join(f"  • {t}" for t in sorted(signal_types))
        alert = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=netuid,
            subnet_name=subnet_name,
            alert_type="convergence",
            description=(
                f"HIGH CONVICTION — {subnet_name}\n"
                f"{len(signal_types)} signals converged in {config.CONVERGENCE_SIGNAL_WINDOW_HOURS}h:\n"
                f"{type_lines}"
            ),
            current_value=float(len(signal_types)),
            threshold=float(config.CONVERGENCE_MIN_SIGNALS),
        )
        await insert_alert(db, alert)
        fired.append(alert)
        logger.info("[ALERT] convergence netuid=%d signals=%s", netuid, sorted(signal_types))

    return fired
```

- [ ] **Step 4: Run all alert-related tests**

```bash
pytest tests/test_convergence.py -v
```

Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/alerts.py tests/test_convergence.py
git commit -m "feat: add analyst/milestone alert fires and convergence detector"
```

---

## Task 9: Scheduler Integration (main.py)

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add imports to main.py**

At the top of `main.py`, after the existing collector imports, add:

```python
from collectors.analyst import AnalystCollector
from collectors.milestone import MilestoneCollector
from engine.alerts import fire_analyst_alerts, fire_milestone_alerts, evaluate_convergence
from db.database import update_registry_category
```

- [ ] **Step 2: Add analyst_collect() function to main.py**

Add after the existing `github_collect()` function:

```python
async def analyst_collect() -> None:
    """60-min analyst X handle scrape. Inserts new mentions and fires alerts."""
    registry = await get_registry(_db)
    await AnalystCollector.collect(_db, registry)
    new_alerts = await fire_analyst_alerts(_db, registry)
    if _telegram and new_alerts:
        unsent = await get_unsent_alerts(_db)
        analyst_unsent = [r for r in unsent if r["alert_type"] == "analyst_mention"]
        if analyst_unsent:
            ids = [r["id"] for r in analyst_unsent]
            from models import AlertRecord as AR
            objs = [AR(
                fired_at=datetime.fromisoformat(r["fired_at"]),
                netuid=r["netuid"], subnet_name=r["subnet_name"],
                alert_type=r["alert_type"], description=r["description"],
                current_value=r["current_value"], threshold=r["threshold"],
            ) for r in analyst_unsent]
            sent_ids = await _telegram.send_alerts(objs, ids)
            await mark_alerts_sent(_db, sent_ids)
    logger.info("[COLLECTOR] analyst_collect done new_alerts=%d", len(new_alerts))


async def milestone_collect() -> None:
    """6-hour milestone poll (arXiv + HuggingFace). Inserts new milestones and fires alerts."""
    registry = await get_registry(_db)
    await MilestoneCollector.collect(_db, registry)
    new_alerts = await fire_milestone_alerts(_db, registry)
    if _telegram and new_alerts:
        unsent = await get_unsent_alerts(_db)
        milestone_unsent = [r for r in unsent if r["alert_type"] == "milestone"]
        if milestone_unsent:
            ids = [r["id"] for r in milestone_unsent]
            from models import AlertRecord as AR
            objs = [AR(
                fired_at=datetime.fromisoformat(r["fired_at"]),
                netuid=r["netuid"], subnet_name=r["subnet_name"],
                alert_type=r["alert_type"], description=r["description"],
                current_value=r["current_value"], threshold=r["threshold"],
            ) for r in milestone_unsent]
            sent_ids = await _telegram.send_alerts(objs, ids)
            await mark_alerts_sent(_db, sent_ids)
    logger.info("[COLLECTOR] milestone_collect done new_alerts=%d", len(new_alerts))
```

- [ ] **Step 3: Update github_collect() to write categories to registry**

In the existing `github_collect()` function in `main.py`, add category writing after the existing snapshot UPDATE block:

```python
async def github_collect() -> None:
    """60-min GitHub data refresh. Updates snapshots and registry categories in DB."""
    registry = await get_registry(_db)
    gh_data = await GitHubCollector.collect(registry)
    for netuid, data in gh_data.items():
        gh_push = data["gh_last_push"].isoformat() if data["gh_last_push"] else None
        await _db.execute("""
            UPDATE snapshots SET gh_last_push=?, gh_stars=?, gh_forks=?, gh_open_issues=?
            WHERE id = (SELECT id FROM snapshots WHERE netuid=? ORDER BY polled_at DESC LIMIT 1)
        """, (gh_push, data["gh_stars"], data["gh_forks"], data["gh_open_issues"], netuid))
        # Write auto-suggested category (skips if user has already confirmed one)
        if "category" in data:
            await update_registry_category(_db, netuid, data["category"], confirmed=False)
    await _db.commit()
    logger.info("[COLLECTOR] github_refresh complete subnets=%d", len(gh_data))
```

- [ ] **Step 4: Add convergence check to poll_cycle() in main.py**

In `poll_cycle()`, after the existing `await evaluate_alerts(...)` call (step 6), add:

```python
    # 6b. Convergence check (requires analyst + milestone alerts to be in DB first)
    await evaluate_convergence(_db, registry)
```

- [ ] **Step 5: Register new scheduler jobs in main()**

In the `main()` function, after the existing `scheduler.add_job` calls, add:

```python
    scheduler.add_job(
        analyst_collect, "interval", minutes=60,
        max_instances=1, id="analyst"
    )
    scheduler.add_job(
        milestone_collect, "interval", hours=6,
        max_instances=1, id="milestone"
    )
```

- [ ] **Step 6: Verify main.py imports cleanly**

```bash
python -c "import main" 2>&1 | head -5
```

Expected: no errors (module loads cleanly — it won't run since `asyncio.run(main())` isn't called).

- [ ] **Step 7: Commit**

```bash
git add main.py
git commit -m "feat: schedule analyst and milestone collectors, wire convergence into poll_cycle"
```

---

## Task 10: Dashboard — Analysts Management Page

**Files:**
- Modify: `web/routes.py`
- Create: `web/templates/analysts.html`

- [ ] **Step 1: Add /analysts routes to web/routes.py**

In `web/routes.py`, add the following imports at the top:

```python
from fastapi import Form
from fastapi.responses import RedirectResponse
from db.database import (
    get_analyst_watchlist, add_analyst_handle, remove_analyst_handle,
)
```

Then add these routes inside the `create_app()` function, after the existing routes:

```python
    @app.get("/analysts", response_class=HTMLResponse)
    async def analysts_page(request: Request):
        db_rows = await get_analyst_watchlist(db)
        db_handles = [row["handle"] for row in db_rows]
        config_handles = config.ANALYST_HANDLES
        return templates.TemplateResponse(request, "analysts.html", {
            "db_handles": db_handles,
            "config_handles": config_handles,
        })

    @app.post("/analysts/add")
    async def analysts_add(handle: str = Form(...)):
        clean = handle.lstrip("@").strip()
        if clean:
            await add_analyst_handle(db, clean, source="dashboard")
        return RedirectResponse("/analysts", status_code=303)

    @app.post("/analysts/remove/{handle}")
    async def analysts_remove(handle: str):
        await remove_analyst_handle(db, handle)
        return RedirectResponse("/analysts", status_code=303)
```

- [ ] **Step 2: Create web/templates/analysts.html**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Analyst Watchlist — TAO Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: monospace; background: #0f0f0f; color: #e0e0e0; font-size: 13px; }
    .header { background: #1a1a2e; padding: 10px 16px; display: flex; gap: 16px; align-items: center; border-bottom: 1px solid #333; }
    .header a { color: #555; text-decoration: none; font-size: 0.8rem; }
    .header a:hover { color: #00d4aa; }
    .header h1 { font-size: 1rem; color: #00d4aa; }
    .page { max-width: 700px; margin: 0 auto; padding: 20px 16px; }
    h2 { font-size: 0.8rem; color: #555; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; margin-top: 24px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.82rem; margin-bottom: 24px; }
    th { text-align: left; padding: 6px 8px; color: #555; border-bottom: 1px solid #222; }
    td { padding: 6px 8px; border-bottom: 1px solid #1a1a1a; color: #ccc; }
    .source-badge { font-size: 0.7rem; padding: 2px 5px; border-radius: 3px; background: #1a1a2e; color: #555; }
    .source-badge.config { color: #444; }
    .source-badge.dashboard { color: #00d4aa; }
    form.remove { display: inline; }
    button.remove-btn { background: none; border: none; color: #ff5252; cursor: pointer; font-size: 0.75rem; font-family: monospace; padding: 0; }
    button.remove-btn:hover { text-decoration: underline; }
    .add-form { display: flex; gap: 8px; margin-bottom: 8px; }
    .add-form input { background: #111; border: 1px solid #333; color: #e0e0e0; padding: 6px 10px; font-family: monospace; font-size: 0.82rem; flex: 1; border-radius: 3px; }
    .add-form button { background: #1a1a2e; border: 1px solid #00d4aa; color: #00d4aa; padding: 6px 14px; font-family: monospace; font-size: 0.82rem; cursor: pointer; border-radius: 3px; }
    .add-form button:hover { background: #00d4aa; color: #0f0f0f; }
    .note { color: #444; font-size: 0.75rem; margin-top: 6px; }
    .empty { color: #333; font-style: italic; padding: 8px 0; }
  </style>
</head>
<body>
<div class="header">
  <a href="/">← Dashboard</a>
  <h1>📡 Analyst Watchlist</h1>
</div>
<div class="page">
  <h2>Watched Accounts</h2>
  <table>
    <tr><th>Handle</th><th>Source</th><th></th></tr>
    {% for h in config_handles %}
    <tr>
      <td>@{{ h }}</td>
      <td><span class="source-badge config">config</span></td>
      <td><span style="color:#333;font-size:0.7rem">remove from .env</span></td>
    </tr>
    {% endfor %}
    {% for h in db_handles %}
    <tr>
      <td>@{{ h }}</td>
      <td><span class="source-badge dashboard">dashboard</span></td>
      <td>
        <form class="remove" method="post" action="/analysts/remove/{{ h }}">
          <button class="remove-btn" type="submit">remove</button>
        </form>
      </td>
    </tr>
    {% endfor %}
    {% if not config_handles and not db_handles %}
    <tr><td colspan="3" class="empty">No handles configured yet.</td></tr>
    {% endif %}
  </table>

  <h2>Add Handle</h2>
  <form class="add-form" method="post" action="/analysts/add">
    <input type="text" name="handle" placeholder="@handle or handle" autocomplete="off">
    <button type="submit">Add</button>
  </form>
  <p class="note">Dashboard-added handles are stored in the DB. Config handles (ANALYST_HANDLES env var) are shown but can only be removed by editing .env.</p>
</div>
</body>
</html>
```

- [ ] **Step 3: Verify the page loads**

Start the app and navigate to `/analysts` in a browser, or:

```bash
python -c "
import asyncio
from db.database import init_db
async def t():
    db = await init_db('./data/test_analysts.db')
    await db.close()
asyncio.run(t())
print('DB OK')
"
```

Expected: `DB OK` — DB initializes with new tables.

- [ ] **Step 4: Commit**

```bash
git add web/routes.py web/templates/analysts.html
git commit -m "feat: add /analysts management page for analyst watchlist"
```

---

## Task 11: Dashboard — Leaderboard Category Filter and Coverage Badge

**Files:**
- Modify: `web/routes.py` (enrich leaderboard data)
- Modify: `web/templates/index.html`

- [ ] **Step 1: Enrich dashboard route with coverage and category data**

In `web/routes.py`, add imports:

```python
from db.database import has_active_analyst_coverage
```

In the `dashboard()` route, after the `staked_netuids` line, add:

```python
        # Build coverage set: subnets with active analyst mentions in decay window
        coverage_netuids: set[int] = set()
        for s in snapshots:
            if await has_active_analyst_coverage(db, s["netuid"], config.ANALYST_COVERAGE_DECAY_HOURS):
                coverage_netuids.add(s["netuid"])
```

Update the `enriched` list comprehension to include `covered` and `category`:

```python
        enriched = [
            {**dict(s),
             "mcap_rank": mcap_rank_map.get(s["netuid"]),
             "trend": trend_arrow(s["netuid"], s["emission_rank"]),
             "staked": s["netuid"] in staked_netuids,
             "covered": s["netuid"] in coverage_netuids,
             "category": s["category"] if "category" in s.keys() else None}
            for s in snapshots
        ]
```

Update the `TemplateResponse` call to pass `categories`:

```python
        all_categories = sorted({
            s["category"] for s in enriched
            if s.get("category") and s["category"] != "Other"
        })

        return templates.TemplateResponse(request, "index.html", {
            "snapshots": enriched,
            "alerts": alerts,
            "last_poll": last_poll,
            "subnet_count": len(snapshots),
            "all_categories": all_categories,
        })
```

Also update the import at the top of `web/routes.py`:

```python
import config
```

(This is already imported — just confirming it's present.)

- [ ] **Step 2: Add category filter sidebar and coverage badge to web/templates/index.html**

In `index.html`, find the `<style>` block and add these CSS rules after `.trend-down`:

```css
    .category-badge { font-size: 0.68rem; padding: 1px 5px; border-radius: 3px;
                      background: #1a1a2e; color: #555; margin-left: 4px; }
    .coverage-badge { color: #00bcd4; margin-left: 4px; font-size: 0.8em; cursor: help; }
    .sidebar { width: 160px; flex-shrink: 0; padding: 16px 12px;
               border-right: 1px solid #222; overflow-y: auto; }
    .sidebar h3 { font-size: 0.7rem; color: #555; text-transform: uppercase;
                  letter-spacing: 1px; margin-bottom: 10px; }
    .cat-chip { display: block; width: 100%; text-align: left; background: none;
                border: 1px solid #222; color: #555; font-family: monospace;
                font-size: 0.72rem; padding: 4px 7px; margin-bottom: 4px;
                border-radius: 3px; cursor: pointer; }
    .cat-chip:hover, .cat-chip.active { border-color: #00d4aa; color: #00d4aa; }
    .cat-chip.all { color: #888; }
```

Find the `<div class="layout">` element and add the sidebar before `.leaderboard`:

```html
  <div class="layout">
    <div class="sidebar">
      <h3>Category</h3>
      <button class="cat-chip all active" onclick="filterCategory('')" id="cat-all">All</button>
      {% for cat in all_categories %}
      <button class="cat-chip" onclick="filterCategory('{{ cat }}')" id="cat-{{ loop.index }}">{{ cat }}</button>
      {% endfor %}
    </div>
    <div class="leaderboard">
```

Close the extra `</div>` at the end of `.leaderboard` (after the `</table>` closing tag, ensure there's still one `</div>` for leaderboard and one for layout).

In the leaderboard table header row, add a column for the coverage badge (it shares the name column — no extra `<th>` needed; badge is inline).

In the leaderboard table body rows, find the `.sn-name` cell and add coverage badge and category:

```html
<td>
  <span class="sn-name">{{ s.name or ("SN" ~ s.netuid) }}</span>
  <span class="sn-id">#{{ s.netuid }}</span>
  {% if s.covered %}<span class="coverage-badge" title="Active analyst coverage">📡</span>{% endif %}
  {% if s.staked %}<span class="staked-badge" title="In your portfolio">●</span>{% endif %}
  {% if s.category and s.category != "Other" %}<span class="category-badge">{{ s.category }}</span>{% endif %}
  <span class="sn-links">...</span>  {# keep existing links #}
</td>
```

Add this JavaScript at the bottom of `index.html` before `</body>`:

```html
<script>
function filterCategory(cat) {
  document.querySelectorAll('.cat-chip').forEach(b => b.classList.remove('active'));
  const btn = cat ? document.getElementById('cat-' + [...document.querySelectorAll('.cat-chip:not(.all)')].findIndex(b => b.textContent === cat) + 1) : document.getElementById('cat-all');
  if (btn) btn.classList.add('active');

  document.querySelectorAll('table.leaderboard-table tbody tr').forEach(row => {
    if (!cat) { row.style.display = ''; return; }
    row.style.display = row.dataset.category === cat ? '' : 'none';
  });
}
</script>
```

Add `data-category="{{ s.category or '' }}"` to each `<tr class="clickable">` row in the leaderboard table.

- [ ] **Step 3: Commit**

```bash
git add web/routes.py web/templates/index.html
git commit -m "feat: add category filter sidebar and analyst coverage badge to leaderboard"
```

---

## Task 12: Dashboard — Subnet Detail Updates

**Files:**
- Modify: `web/routes.py` (enrich detail route)
- Modify: `web/templates/subnet.html`

- [ ] **Step 1: Enrich subnet detail route**

In `web/routes.py`, add imports:

```python
from db.database import (
    get_analyst_mentions_for_netuid, get_milestones_for_netuid,
    update_registry_category,
)
```

In the `subnet_detail()` route, after the existing `alerts` query, add:

```python
        analyst_mentions = await get_analyst_mentions_for_netuid(db, netuid, limit=10)
        milestones = await get_milestones_for_netuid(db, netuid, limit=10)
```

Add these to the `TemplateResponse` call:

```python
        return templates.TemplateResponse(request, "subnet.html", {
            ...,  # keep all existing keys
            "analyst_mentions": analyst_mentions,
            "milestones": milestones,
        })
```

Add a POST route for category confirmation:

```python
    @app.post("/subnet/{netuid}/category")
    async def subnet_set_category(netuid: int, category: str = Form(...)):
        await update_registry_category(db, netuid, category, confirmed=True)
        return RedirectResponse(f"/subnet/{netuid}", status_code=303)
```

- [ ] **Step 2: Add milestone timeline and analyst mentions to web/templates/subnet.html**

In `subnet.html`, find the end of the existing `.page` content (after the alerts card) and add:

```html
{% if milestones %}
<div class="card full-width" style="margin-top:16px">
  <h3>Milestones</h3>
  {% for m in milestones %}
  <div class="alert-item">
    <div class="alert-type">
      {{ "🔬 arXiv" if m.milestone_type == "arxiv" else "🤗 HuggingFace" }}
    </div>
    <div class="alert-desc"><a href="{{ m.url }}" target="_blank" style="color:#00d4aa">{{ m.title }}</a></div>
    {% if m.ai_summary %}<div class="alert-desc" style="color:#888;margin-top:4px">{{ m.ai_summary }}</div>{% endif %}
    {% if m.ai_take %}<div class="alert-desc" style="color:#00bcd4;margin-top:2px"><em>Take: {{ m.ai_take }}</em></div>{% endif %}
    <div class="alert-time">{{ m.published_at[:10] }}</div>
  </div>
  {% endfor %}
</div>
{% endif %}

{% if analyst_mentions %}
<div class="card full-width" style="margin-top:16px">
  <h3>Analyst Mentions</h3>
  {% for m in analyst_mentions %}
  <div class="alert-item">
    <div class="alert-type">📡 @{{ m.analyst_handle }}</div>
    <div class="alert-desc">{{ (m.tweet_text or "")[:200] }}{% if (m.tweet_text or "")|length > 200 %}…{% endif %}</div>
    <div class="alert-time"><a href="{{ m.tweet_url }}" target="_blank" style="color:#555">{{ m.mentioned_at[:16] }} UTC →</a></div>
  </div>
  {% endfor %}
</div>
{% endif %}

<div class="card full-width" style="margin-top:16px">
  <h3>Category</h3>
  <form method="post" action="/subnet/{{ snap.netuid }}/category" style="display:flex;gap:8px;align-items:center">
    <select name="category" style="background:#111;border:1px solid #333;color:#e0e0e0;padding:5px 8px;font-family:monospace;font-size:0.82rem;border-radius:3px">
      {% for cat in ["AI Training","Post-Training/RLHF","Data / Retrieval","Quant / Finance","Privacy / Compute","Biomedical","Infrastructure","Other"] %}
      <option value="{{ cat }}" {% if snap.category == cat %}selected{% endif %}>{{ cat }}</option>
      {% endfor %}
    </select>
    <button type="submit" style="background:#1a1a2e;border:1px solid #00d4aa;color:#00d4aa;padding:5px 12px;font-family:monospace;font-size:0.82rem;cursor:pointer;border-radius:3px">
      {% if snap.category_confirmed %}Update{% else %}Confirm{% endif %}
    </button>
    {% if snap.category_confirmed %}<span style="color:#444;font-size:0.72rem">✓ confirmed</span>{% else %}<span style="color:#333;font-size:0.72rem">auto-suggested</span>{% endif %}
  </form>
</div>
```

- [ ] **Step 3: Run all tests to confirm nothing is broken**

```bash
pytest -v
```

Expected: all existing tests + new tests pass. No import errors.

- [ ] **Step 4: Commit**

```bash
git add web/routes.py web/templates/subnet.html
git commit -m "feat: add milestone timeline, analyst mentions feed, and category editor to subnet detail"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Analyst Collector: `collectors/analyst.py` — Tasks 4 + 5
- ✅ Analyst alert + dashboard badge: `engine/alerts.py` `fire_analyst_alerts()` — Task 8; badge in `index.html` — Task 11
- ✅ ANALYST_HANDLES config + DB watchlist: `config.py` Task 2, DB Task 3, `/analysts` route Task 10
- ✅ Milestone Collector (arXiv + HF): `collectors/milestone.py` — Task 7
- ✅ AI Signal Interpreter: `interpret_milestone()` inside Task 7
- ✅ Milestone alert with AI take: `fire_milestone_alerts()` — Task 8
- ✅ Category auto-suggest: `suggest_category()` + `fetch_readme()` — Task 6
- ✅ Category confirmed/override in dashboard: `/subnet/{netuid}/category` POST — Task 12
- ✅ Category filter sidebar: `index.html` — Task 11
- ✅ Signal convergence detector: `evaluate_convergence()` — Task 8; wired in `poll_cycle()` — Task 9
- ✅ analyst_watchlist table: DB Task 3; managed via /analysts — Task 10
- ✅ collector_state table for milestone last-check: DB Task 3; used in `MilestoneCollector.collect()` — Task 7
- ✅ CONVERGENCE_COOLDOWN_HOURS (48h, separate from standard 6h): `config.py` Task 2; used in `evaluate_convergence()` — Task 8
- ✅ coverage badge decay (72h ANALYST_COVERAGE_DECAY_HOURS): Task 11 route passes to template
- ✅ Milestone timeline on subnet detail: Task 12
- ✅ Analyst mentions feed on subnet detail: Task 12
- ✅ Link from header to /analysts: Task 10 template includes `← Dashboard`; add `/analysts` link in `index.html` header (add to Task 11 inline — the template already has a header nav pattern)

**Placeholder scan:** No TBDs. All code blocks are complete.

**Type consistency:** 
- `insert_analyst_mention(db, handle, netuid, tweet_url, tweet_text, mentioned_at: datetime)` — used identically in Task 5 (AnalystCollector) and Task 3 (DB function signature).
- `insert_milestone(db, netuid, milestone_type, title, url, published_at, ai_summary, ai_take)` — used identically in Task 7 (MilestoneCollector) and Task 3 (DB function).
- `_registry_name(registry, netuid)` — already in `alerts.py`, used correctly in Task 8.
- `update_registry_category(db, netuid, category, confirmed)` — defined in Task 3, used in Task 9 (github_collect) and Task 12 (POST route). Signatures match.
