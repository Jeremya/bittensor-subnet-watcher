# Dashboard Investor View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Transform the TAO Monitor dashboard into a discovery tool: a leaderboard with real subnet names, USD mcap, emission vs. mcap ranks, and trend arrows — plus a per-subnet scorecard page reachable by clicking any row.

**Architecture:** Pure server-side Jinja2/FastAPI. Four new DB query functions feed an updated dashboard route and a new `/subnet/{netuid}` route. Two templates are updated/created. Zero new dependencies.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, aiosqlite, pytest with `asyncio_mode = auto`, httpx AsyncClient for route tests.

---

## File Map

| File | Change |
|------|--------|
| `db/database.py` | +4 new query functions |
| `web/routes.py` | Update dashboard route; add subnet_detail route; add datetime import |
| `web/templates/index.html` | New leaderboard columns, row links, fmt_usd macro |
| `web/templates/subnet.html` | New file — scorecard detail page |
| `tests/test_database.py` | +5 tests for new DB functions |
| `tests/web/test_routes.py` | +6 tests for updated and new routes |

---

### Task 1: New DB query functions

**Files:**
- Modify: `db/database.py`
- Modify: `tests/test_database.py`

**Background:** `db/database.py` currently has `get_latest_snapshots()` which returns snapshots ordered by composite score but with no registry data. We need four new functions. The existing functions are NOT changed.

- [ ] **Step 1: Write the five failing tests**

Open `tests/test_database.py`. Add `upsert_registry_entry` to the existing import line, and add the four new functions to the new imports at the bottom of the import block:

```python
# existing import line — extend it:
from db.database import SCHEMA_SQL, insert_snapshot, get_latest_snapshots, \
    insert_alert, get_unsent_alerts, mark_alerts_sent, is_alert_in_cooldown, \
    prune_old_snapshots, upsert_registry_entry, \
    get_latest_snapshots_with_registry, get_emission_rank_24h_ago, \
    get_subnet_detail, get_alerts_for_netuid
```

Then append these five tests at the end of the file:

```python
# ── New DB functions ───────────────────────────────────────────────────────────

async def test_get_latest_snapshots_with_registry_includes_name(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              composite_score=80.0))
    await upsert_registry_entry(db, 1, "Apex", "https://github.com/apex/sn",
                                 "apex_subnet", "https://apex.ai")
    rows = await get_latest_snapshots_with_registry(db)
    assert len(rows) == 1
    assert rows[0]["name"] == "Apex"
    assert rows[0]["x_handle"] == "apex_subnet"


async def test_get_latest_snapshots_with_registry_name_none_without_registry(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=42, polled_at=now,
                                              composite_score=50.0))
    rows = await get_latest_snapshots_with_registry(db)
    assert rows[0]["name"] is None  # no registry entry → LEFT JOIN produces NULL


async def test_get_emission_rank_24h_ago_returns_old_rank(db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=25)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=old,
                                              emission_rank=5))
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              emission_rank=3))
    result = await get_emission_rank_24h_ago(db)
    assert result[1] == 5  # returns the OLD rank, not the current one


async def test_get_emission_rank_24h_ago_ignores_recent_only(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              emission_rank=3))
    result = await get_emission_rank_24h_ago(db)
    assert 1 not in result  # no snapshot older than 24h → nothing returned


async def test_get_subnet_detail_includes_registry_data(db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=5, polled_at=now,
                                              composite_score=60.0,
                                              alpha_mcap_usd=1_200_000.0))
    await upsert_registry_entry(db, 5, "Vision", None, None, "https://vision.ai")
    row = await get_subnet_detail(db, 5)
    assert row is not None
    assert row["name"] == "Vision"
    assert row["composite_score"] == pytest.approx(60.0)


async def test_get_alerts_for_netuid_filters_correctly(db):
    now = datetime.now(timezone.utc)
    for netuid in [1, 2, 1]:
        await insert_alert(db, AlertRecord(
            fired_at=now, netuid=netuid, subnet_name=f"SN{netuid}",
            alert_type="new_entry", description="x"
        ))
    rows = await get_alerts_for_netuid(db, 1, limit=10)
    assert len(rows) == 2
    assert all(r["netuid"] == 1 for r in rows)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/jeremy/dev/tao-subnet-investigation
pytest tests/test_database.py -v -k "registry or emission_rank_24h or subnet_detail or alerts_for_netuid" 2>&1 | tail -20
```

Expected: `ImportError: cannot import name 'get_latest_snapshots_with_registry'`

- [ ] **Step 3: Implement the four new functions in `db/database.py`**

Add these four functions after `get_snapshots_for_netuid` and before `insert_alert`:

```python
async def get_latest_snapshots_with_registry(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Latest snapshot per netuid LEFT JOINed with subnet_registry. Ordered by composite_score DESC."""
    cursor = await db.execute("""
        SELECT s.*, r.name, r.github_url, r.x_handle, r.website
        FROM snapshots s
        INNER JOIN (
            SELECT netuid, MAX(polled_at) AS max_ts
            FROM snapshots GROUP BY netuid
        ) latest ON s.netuid = latest.netuid AND s.polled_at = latest.max_ts
        LEFT JOIN subnet_registry r ON s.netuid = r.netuid
        ORDER BY s.composite_score DESC NULLS LAST
    """)
    return await cursor.fetchall()


async def get_emission_rank_24h_ago(db: aiosqlite.Connection) -> dict[int, Optional[int]]:
    """Return {netuid: emission_rank} from the most recent snapshot ≥24h old per netuid."""
    cursor = await db.execute("""
        SELECT netuid, emission_rank
        FROM snapshots s1
        WHERE polled_at = (
            SELECT MAX(polled_at) FROM snapshots s2
            WHERE s2.netuid = s1.netuid
            AND s2.polled_at <= datetime('now', '-24 hours')
        )
    """)
    rows = await cursor.fetchall()
    return {row["netuid"]: row["emission_rank"] for row in rows}


async def get_subnet_detail(db: aiosqlite.Connection,
                             netuid: int) -> Optional[aiosqlite.Row]:
    """Latest snapshot for one netuid LEFT JOINed with subnet_registry."""
    cursor = await db.execute("""
        SELECT s.*, r.name, r.github_url, r.x_handle, r.website
        FROM snapshots s
        LEFT JOIN subnet_registry r ON s.netuid = r.netuid
        WHERE s.netuid = ?
        ORDER BY s.polled_at DESC LIMIT 1
    """, (netuid,))
    return await cursor.fetchone()


async def get_alerts_for_netuid(db: aiosqlite.Connection,
                                 netuid: int,
                                 limit: int = 10) -> list[aiosqlite.Row]:
    """Most recent alerts for a specific subnet."""
    cursor = await db.execute(
        "SELECT * FROM alerts WHERE netuid = ? ORDER BY fired_at DESC LIMIT ?",
        (netuid, limit)
    )
    return await cursor.fetchall()
```

- [ ] **Step 4: Run the new tests to verify they pass**

```bash
pytest tests/test_database.py -v -k "registry or emission_rank_24h or subnet_detail or alerts_for_netuid"
```

Expected: 6 tests PASSED (5 new + implicitly the existing ones still pass)

- [ ] **Step 5: Run the full test suite to confirm no regressions**

```bash
pytest tests/test_database.py -v
```

Expected: all tests PASS (was 6 tests, now 12)

- [ ] **Step 6: Commit**

```bash
git add db/database.py tests/test_database.py
git commit -m "feat: add registry-joined and trend DB query functions"
```

---

### Task 2: Update dashboard route and leaderboard template

**Files:**
- Modify: `web/routes.py`
- Modify: `web/templates/index.html`
- Modify: `tests/web/test_routes.py`

**Background:** The dashboard route currently calls `get_latest_snapshots()`. We replace it with `get_latest_snapshots_with_registry()`, compute mcap_rank and trend arrows in Python, and pass enriched dicts to the template. The template gets new columns and clickable rows.

- [ ] **Step 1: Write three failing route tests**

Open `tests/web/test_routes.py`. Add `upsert_registry_entry` to the imports:

```python
from db.database import SCHEMA_SQL, insert_snapshot, insert_alert, upsert_registry_entry
```

Append these tests at the end of the file:

```python
async def test_dashboard_shows_registry_name(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              composite_score=80.0,
                                              alpha_mcap_tao=5000.0))
    await upsert_registry_entry(db, 1, "Apex", None, None, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "Apex" in resp.text


async def test_dashboard_row_links_to_subnet_page(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=7, polled_at=now,
                                              composite_score=60.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert "/subnet/7" in resp.text


async def test_dashboard_shows_mcap_usd(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              composite_score=75.0,
                                              alpha_mcap_usd=2_100_000.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert "$2.1M" in resp.text
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/web/test_routes.py -v -k "registry_name or links_to or mcap_usd"
```

Expected: FAILED — `AssertionError` (current template doesn't have Apex, /subnet/7, or $2.1M)

- [ ] **Step 3: Update `web/routes.py`**

Replace the entire file content with:

```python
# web/routes.py
import aiosqlite
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from db.database import (
    get_latest_snapshots, get_last_50_alerts,
    get_latest_snapshots_with_registry, get_emission_rank_24h_ago,
    get_subnet_detail, get_alerts_for_netuid, get_snapshots_for_netuid,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(db: aiosqlite.Connection) -> FastAPI:
    app = FastAPI(title="TAO Monitor")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        snapshots = await get_latest_snapshots_with_registry(db)
        alerts = await get_last_50_alerts(db)
        trend_raw = await get_emission_rank_24h_ago(db)
        last_poll = snapshots[0]["polled_at"] if snapshots else None

        sorted_by_mcap = sorted(
            [s for s in snapshots if s["alpha_mcap_tao"] is not None],
            key=lambda s: s["alpha_mcap_tao"], reverse=True,
        )
        mcap_rank_map = {s["netuid"]: i + 1 for i, s in enumerate(sorted_by_mcap)}

        def trend_arrow(netuid, current_rank):
            prev = trend_raw.get(netuid)
            if prev is None or current_rank is None:
                return "—"
            if current_rank < prev:
                return "▲"
            if current_rank > prev:
                return "▼"
            return "→"

        enriched = [
            {**dict(s),
             "mcap_rank": mcap_rank_map.get(s["netuid"]),
             "trend": trend_arrow(s["netuid"], s["emission_rank"])}
            for s in snapshots
        ]

        return templates.TemplateResponse(request, "index.html", {
            "snapshots": enriched,
            "alerts": alerts,
            "last_poll": last_poll,
            "subnet_count": len(snapshots),
        })

    @app.get("/subnet/{netuid}", response_class=HTMLResponse)
    async def subnet_detail(request: Request, netuid: int):
        snap = await get_subnet_detail(db, netuid)
        if snap is None:
            return HTMLResponse("Subnet not found", status_code=404)

        alerts = await get_alerts_for_netuid(db, netuid, limit=10)
        all_snaps = await get_latest_snapshots_with_registry(db)
        total = len(all_snaps)

        sorted_by_mcap = sorted(
            [s for s in all_snaps if s["alpha_mcap_tao"] is not None],
            key=lambda s: s["alpha_mcap_tao"], reverse=True,
        )
        mcap_rank = next(
            (i + 1 for i, s in enumerate(sorted_by_mcap) if s["netuid"] == netuid),
            None,
        )
        score_rank = next(
            (i + 1 for i, s in enumerate(all_snaps) if s["netuid"] == netuid),
            None,
        )

        em_rank = snap["emission_rank"]
        yield_why = "no emission data"
        if em_rank and mcap_rank:
            ratio = mcap_rank / em_rank
            yield_why = f"em #{em_rank} vs mc #{mcap_rank} → {ratio:.1f}× gap"

        quality_why = "no GitHub data"
        if snap["gh_last_push"]:
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(snap["gh_last_push"])).days
            stars = snap["gh_stars"] or 0
            quality_why = f"pushed {age}d ago · {stars:,} ⭐"

        momentum_why = "no history"
        history = await get_snapshots_for_netuid(db, netuid, limit=8)
        if len(history) >= 2:
            oldest_rank = history[-1]["emission_rank"]
            if oldest_rank and em_rank:
                delta = oldest_rank - em_rank
                polls = len(history) - 1
                if delta > 0:
                    momentum_why = f"+{delta} em ranks in {polls} polls"
                elif delta < 0:
                    momentum_why = f"{delta} em ranks in {polls} polls"
                else:
                    momentum_why = "stable"

        now_utc = datetime.now(timezone.utc)
        gh_push_age_days = None
        if snap["gh_last_push"]:
            gh_push_age_days = (now_utc -
                                datetime.fromisoformat(snap["gh_last_push"])).days
        x_tweet_age_days = None
        if snap["x_last_tweet"]:
            x_tweet_age_days = (now_utc -
                                datetime.fromisoformat(snap["x_last_tweet"])).days

        return templates.TemplateResponse(request, "subnet.html", {
            "snap": dict(snap),
            "alerts": alerts,
            "mcap_rank": mcap_rank,
            "score_rank": score_rank,
            "total_subnets": total,
            "yield_why": yield_why,
            "quality_why": quality_why,
            "momentum_why": momentum_why,
            "gh_push_age_days": gh_push_age_days,
            "x_tweet_age_days": x_tweet_age_days,
        })

    @app.get("/api/snapshots")
    async def api_snapshots():
        rows = await get_latest_snapshots(db)
        return [dict(row) for row in rows]

    @app.get("/api/alerts")
    async def api_alerts():
        rows = await get_last_50_alerts(db)
        return [dict(row) for row in rows]

    return app
```

- [ ] **Step 4: Replace `web/templates/index.html` with the updated leaderboard**

Replace the entire file with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="60">
  <title>TAO Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: monospace; background: #0f0f0f; color: #e0e0e0; }
    header { background: #1a1a2e; padding: 12px 20px; display: flex;
             gap: 24px; align-items: center; border-bottom: 1px solid #333; }
    header h1 { font-size: 1.2rem; color: #00d4aa; }
    header span { font-size: 0.85rem; color: #999; }
    .layout { display: flex; gap: 0; height: calc(100vh - 50px); }
    .leaderboard { flex: 1.5; overflow-y: auto; padding: 16px; }
    .alert-feed { flex: 1; overflow-y: auto; padding: 16px;
                  border-left: 1px solid #333; }
    h2 { font-size: 0.9rem; color: #888; text-transform: uppercase;
         letter-spacing: 1px; margin-bottom: 12px; }
    table { width: 100%; border-collapse: collapse; font-size: 0.8rem; }
    th { text-align: left; padding: 6px 8px; color: #666;
         border-bottom: 1px solid #333; }
    td { padding: 6px 8px; border-bottom: 1px solid #1a1a1a; }
    tr.clickable { cursor: pointer; }
    tr.clickable:hover td { background: #1a1a1a; }
    tr.clickable:hover .sn-name { color: #00ffcc; }
    .sn-name { color: #00d4aa; }
    .sn-id { color: #444; margin-left: 4px; font-size: 0.9em; }
    .score-high { color: #00c853; }
    .score-med  { color: #ffd600; }
    .score-low  { color: #ff5252; }
    .score-null { color: #555; }
    .trend-up   { color: #00c853; }
    .trend-flat { color: #555; }
    .trend-down { color: #ff5252; }
    .alert-item { padding: 10px; border: 1px solid #333; margin-bottom: 8px;
                  border-radius: 4px; font-size: 0.8rem; }
    .alert-item .type { font-weight: bold; color: #00d4aa; }
    .alert-item .time { color: #666; font-size: 0.75rem; }
    .empty { color: #555; font-style: italic; padding: 20px 0; }
  </style>
</head>
<body>

{% macro fmt_usd(v) -%}
{%- if v is none %}—
{%- elif v >= 1000000 %}${{ "%.1f"|format(v / 1000000) }}M
{%- elif v >= 1000 %}${{ "%.0f"|format(v / 1000) }}K
{%- else %}${{ "%.0f"|format(v) }}
{%- endif %}
{%- endmacro %}

<header>
  <h1>TAO Monitor</h1>
  <span>Subnets: {{ subnet_count }}</span>
  <span>Last poll: {{ last_poll or "never" }}</span>
</header>
<div class="layout">

  <div class="leaderboard">
    <h2>Leaderboard</h2>
    {% if snapshots %}
    <table>
      <thead>
        <tr>
          <th>#</th><th>Subnet</th><th>Score</th>
          <th>Em. Rank</th><th>MC. Rank</th>
          <th>MCap</th><th>Emit/day</th><th>Trend</th>
        </tr>
      </thead>
      <tbody>
        {% for row in snapshots %}
        {% set score = row.composite_score %}
        {% set cls = "score-high" if score and score > 70
                     else ("score-med" if score and score > 40
                     else ("score-low" if score else "score-null")) %}
        {% set trend_cls = "trend-up" if row.trend == "▲"
                           else ("trend-down" if row.trend == "▼" else "trend-flat") %}
        <tr class="clickable" onclick="location.href='/subnet/{{ row.netuid }}'">
          <td class="score-null">{{ loop.index }}</td>
          <td>
            <span class="sn-name">{{ row.name or ("SN" ~ row.netuid) }}</span>
            <span class="sn-id">SN{{ row.netuid }}</span>
          </td>
          <td class="{{ cls }}">{{ "%.1f"|format(score) if score else "—" }}</td>
          <td>{{ ("#" ~ row.emission_rank) if row.emission_rank else "—" }}</td>
          <td>{{ ("#" ~ row.mcap_rank) if row.mcap_rank else "—" }}</td>
          <td>{{ fmt_usd(row.alpha_mcap_usd) }}</td>
          <td>{{ ("%.0f"|format(row.daily_emission_tao) ~ " τ") if row.daily_emission_tao else "—" }}</td>
          <td class="{{ trend_cls }}">{{ row.trend }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% else %}
    <p class="empty">Waiting for first poll…</p>
    {% endif %}
  </div>

  <div class="alert-feed">
    <h2>Alerts</h2>
    {% if alerts %}
    {% for alert in alerts %}
    <div class="alert-item">
      <div class="type">{{ alert.alert_type.replace("_", " ").title() }}
        — SN{{ alert.netuid }} {{ alert.subnet_name }}</div>
      <div>{{ alert.description }}</div>
      <div class="time">{{ alert.fired_at }}</div>
    </div>
    {% endfor %}
    {% else %}
    <p class="empty">No alerts yet</p>
    {% endif %}
  </div>

</div>
</body>
</html>
```

- [ ] **Step 5: Create a stub `web/templates/subnet.html`** so the new route doesn't crash before Task 3 fills it in

```html
<!DOCTYPE html>
<html><body><p>Subnet {{ snap.netuid }} — coming in Task 3</p></body></html>
```

- [ ] **Step 6: Run the new route tests**

```bash
pytest tests/web/test_routes.py -v -k "registry_name or links_to or mcap_usd"
```

Expected: 3 tests PASSED

- [ ] **Step 7: Run the full route test suite**

```bash
pytest tests/web/test_routes.py -v
```

Expected: all 7 tests PASS (4 existing + 3 new)

- [ ] **Step 8: Commit**

```bash
git add web/routes.py web/templates/index.html web/templates/subnet.html \
        tests/web/test_routes.py
git commit -m "feat: update dashboard route and leaderboard with registry names, mcap rank, trend arrows"
```

---

### Task 3: Subnet detail route — full template

**Files:**
- Modify: `web/templates/subnet.html` (replace stub with full page)
- Modify: `tests/web/test_routes.py` (add 3 detail page tests)

**Background:** The route code was added in Task 2. This task replaces the stub template with the full scorecard page and adds tests that verify the HTML content.

- [ ] **Step 1: Write three failing detail page tests**

Append to `tests/web/test_routes.py`:

```python
async def test_subnet_detail_returns_200(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=5, polled_at=now,
                                              composite_score=72.0,
                                              yield_score=80.0,
                                              quality_score=60.0,
                                              momentum_score=76.0,
                                              emission_rank=4,
                                              alpha_mcap_tao=3000.0,
                                              alpha_mcap_usd=1_374_000.0))
    await upsert_registry_entry(db, 5, "Vision", "https://github.com/v/sn",
                                 "vision_ai", "https://vision.ai")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/5")
    assert resp.status_code == 200
    assert "Vision" in resp.text
    assert "Score Breakdown" in resp.text


async def test_subnet_detail_shows_chain_stats(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=5, polled_at=now,
                                              composite_score=72.0,
                                              alpha_mcap_usd=1_374_000.0,
                                              daily_emission_tao=88.0,
                                              emission_rank=4))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/5")
    assert "$1.4M" in resp.text
    assert "88" in resp.text   # daily emission


async def test_subnet_detail_404_for_unknown(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/9999")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/web/test_routes.py -v -k "subnet_detail"
```

Expected: `test_subnet_detail_returns_200` FAILS (stub template doesn't have "Score Breakdown"), `test_subnet_detail_404_for_unknown` PASSES already (route logic correct), `test_subnet_detail_shows_chain_stats` FAILS.

- [ ] **Step 3: Replace `web/templates/subnet.html` with the full scorecard**

Replace the entire file with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{{ snap.name or ("SN" ~ snap.netuid) }} — TAO Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: monospace; background: #0f0f0f; color: #e0e0e0; font-size: 13px; }
    header { background: #1a1a2e; padding: 10px 16px; display: flex; gap: 16px;
             align-items: center; border-bottom: 1px solid #333; }
    header a.back { color: #555; text-decoration: none; font-size: 0.85rem; }
    header a.back:hover { color: #00d4aa; }
    header h1 { font-size: 1rem; color: #00d4aa; }
    header .sub { color: #555; font-size: 0.8rem; }
    header .ext-links { margin-left: auto; display: flex; gap: 12px; }
    header .ext-links a { color: #555; text-decoration: none; font-size: 0.78rem; }
    header .ext-links a:hover { color: #00d4aa; }
    .page { max-width: 900px; margin: 0 auto; padding: 20px 16px; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
    .card { background: #111; border: 1px solid #1e1e1e; border-radius: 4px; padding: 14px; }
    .card.full { grid-column: 1 / -1; }
    .card h3 { font-size: 0.68rem; color: #555; text-transform: uppercase;
               letter-spacing: 1px; margin-bottom: 12px; }
    .score-row { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
    .score-lbl { width: 76px; color: #666; font-size: 0.82em; flex-shrink: 0; }
    .bar-wrap { flex: 1; height: 4px; background: #1e1e1e; border-radius: 2px; }
    .bar { height: 100%; border-radius: 2px; background: #333; }
    .bar.score-high { background: #00c853; }
    .bar.score-med  { background: #ffd600; }
    .bar.score-low  { background: #ff5252; }
    .bar.score-null { background: #333; }
    .score-num { min-width: 34px; text-align: right; font-size: 0.9em; }
    .score-why { color: #555; font-size: 0.76em; white-space: nowrap;
                 overflow: hidden; text-overflow: ellipsis; max-width: 180px; }
    .composite { border-top: 1px solid #222; padding-top: 10px; margin-top: 4px;
                 display: flex; align-items: baseline; gap: 8px; }
    .composite-lbl { color: #888; font-size: 0.82em; width: 76px; }
    .composite-val { font-size: 1.4em; }
    .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4px 12px; }
    .stat { padding: 5px 0; border-bottom: 1px solid #161616; }
    .stat.full { grid-column: 1 / -1; }
    .stat-lbl { font-size: 0.67rem; color: #444; text-transform: uppercase; letter-spacing: 0.5px; }
    .stat-val { font-size: 0.85rem; color: #ccc; margin-top: 2px; }
    .stat-val.accent { color: #00d4aa; }
    .stat-val.dim { color: #555; font-size: 0.78rem; }
    .stat-val.coldkey { color: #444; word-break: break-all; }
    .gh-row { display: flex; gap: 20px; flex-wrap: wrap; }
    .gh-stat .lbl { font-size: 0.67rem; color: #444; text-transform: uppercase; }
    .gh-stat .val { font-size: 0.85rem; color: #ccc; margin-top: 2px; }
    .age-fresh { color: #00c853; }
    .age-med   { color: #ffd600; }
    .age-old   { color: #ff5252; }
    .alert-item { padding: 8px 10px; border: 1px solid #1e1e1e; margin-bottom: 6px;
                  border-radius: 3px; font-size: 0.78rem; }
    .alert-type { color: #00d4aa; font-weight: bold; }
    .alert-desc { color: #777; margin-top: 2px; }
    .alert-time { color: #444; font-size: 0.72rem; margin-top: 2px; }
    .empty { color: #444; font-style: italic; font-size: 0.82rem; }
    .score-high { color: #00c853; }
    .score-med  { color: #ffd600; }
    .score-low  { color: #ff5252; }
    .score-null { color: #555; }
  </style>
</head>
<body>

{% macro fmt_usd(v) -%}
{%- if v is none %}—
{%- elif v >= 1000000 %}${{ "%.1f"|format(v / 1000000) }}M
{%- elif v >= 1000 %}${{ "%.0f"|format(v / 1000) }}K
{%- else %}${{ "%.0f"|format(v) }}
{%- endif %}
{%- endmacro %}

{% macro score_cls(v) -%}
{%- if v and v > 70 %}score-high
{%- elif v and v > 40 %}score-med
{%- elif v %}score-low
{%- else %}score-null{%- endif %}
{%- endmacro %}

{% macro age_cls(days) -%}
{%- if days is none %}score-null
{%- elif days <= 7 %}age-fresh
{%- elif days <= 30 %}age-med
{%- else %}age-old{%- endif %}
{%- endmacro %}

<header>
  <a class="back" href="/">← Leaderboard</a>
  <h1>{{ snap.name or ("SN" ~ snap.netuid) }}</h1>
  <span class="sub">SN{{ snap.netuid }}{% if score_rank %} · #{{ score_rank }} by score{% endif %}</span>
  <div class="ext-links">
    {% if snap.github_url %}<a href="{{ snap.github_url }}" target="_blank">GitHub ↗</a>{% endif %}
    {% if snap.website %}<a href="{{ snap.website }}" target="_blank">Website ↗</a>{% endif %}
    {% if snap.x_handle %}<a href="https://x.com/{{ snap.x_handle }}" target="_blank">@{{ snap.x_handle }} ↗</a>{% endif %}
  </div>
</header>

<div class="page">
  <div class="grid">

    <!-- Score Breakdown -->
    <div class="card">
      <h3>Score Breakdown</h3>

      <div class="score-row">
        <span class="score-lbl">Yield</span>
        <div class="bar-wrap">
          <div class="bar {{ score_cls(snap.yield_score) }}"
               style="width: {{ [(snap.yield_score or 0)|int, 100]|min }}%"></div>
        </div>
        <span class="score-num {{ score_cls(snap.yield_score) }}">
          {{ "%.1f"|format(snap.yield_score) if snap.yield_score else "—" }}
        </span>
        <span class="score-why">{{ yield_why }}</span>
      </div>

      <div class="score-row">
        <span class="score-lbl">Quality</span>
        <div class="bar-wrap">
          <div class="bar {{ score_cls(snap.quality_score) }}"
               style="width: {{ [(snap.quality_score or 0)|int, 100]|min }}%"></div>
        </div>
        <span class="score-num {{ score_cls(snap.quality_score) }}">
          {{ "%.1f"|format(snap.quality_score) if snap.quality_score else "—" }}
        </span>
        <span class="score-why">{{ quality_why }}</span>
      </div>

      <div class="score-row">
        <span class="score-lbl">Momentum</span>
        <div class="bar-wrap">
          <div class="bar {{ score_cls(snap.momentum_score) }}"
               style="width: {{ [(snap.momentum_score or 0)|int, 100]|min }}%"></div>
        </div>
        <span class="score-num {{ score_cls(snap.momentum_score) }}">
          {{ "%.1f"|format(snap.momentum_score) if snap.momentum_score else "—" }}
        </span>
        <span class="score-why">{{ momentum_why }}</span>
      </div>

      <div class="composite">
        <span class="composite-lbl">Composite</span>
        <span class="composite-val {{ score_cls(snap.composite_score) }}">
          {{ "%.1f"|format(snap.composite_score) if snap.composite_score else "—" }}
        </span>
      </div>
    </div>

    <!-- Chain Stats -->
    <div class="card">
      <h3>Chain Stats</h3>
      <div class="stat-grid">

        <div class="stat">
          <div class="stat-lbl">MCap</div>
          <div class="stat-val accent">{{ fmt_usd(snap.alpha_mcap_usd) }}</div>
          {% if snap.alpha_mcap_tao %}
          <div class="stat-val dim">{{ "{:,.0f}".format(snap.alpha_mcap_tao) }} τ</div>
          {% endif %}
        </div>

        <div class="stat">
          <div class="stat-lbl">Daily Emission</div>
          <div class="stat-val">
            {{ ("%.0f"|format(snap.daily_emission_tao) ~ " τ/day") if snap.daily_emission_tao else "—" }}
          </div>
          {% if snap.emission_rank %}
          <div class="stat-val dim">Rank #{{ snap.emission_rank }} of {{ total_subnets }}</div>
          {% endif %}
        </div>

        <div class="stat">
          <div class="stat-lbl">Alpha Price</div>
          <div class="stat-val">
            {{ ("%.4f"|format(snap.alpha_price_tao) ~ " τ") if snap.alpha_price_tao else "—" }}
          </div>
        </div>

        <div class="stat">
          <div class="stat-lbl">24h Volume</div>
          <div class="stat-val">
            {{ ("{:,.0f}".format(snap.volume_24h_alpha) ~ " α") if snap.volume_24h_alpha else "—" }}
          </div>
        </div>

        <div class="stat">
          <div class="stat-lbl">Neurons</div>
          <div class="stat-val">{{ (snap.n_neurons ~ " active") if snap.n_neurons else "—" }}</div>
        </div>

        <div class="stat">
          <div class="stat-lbl">Reg Cost</div>
          <div class="stat-val">
            {{ ("%.4f"|format(snap.reg_cost_tao) ~ " τ") if snap.reg_cost_tao else "—" }}
          </div>
        </div>

        {% if snap.owner_coldkey %}
        <div class="stat full">
          <div class="stat-lbl">Owner</div>
          <div class="stat-val coldkey">{{ snap.owner_coldkey }}</div>
        </div>
        {% endif %}

      </div>
    </div>

    <!-- GitHub (shown only when data exists) -->
    {% if snap.gh_last_push or snap.gh_stars is not none %}
    <div class="card">
      <h3>GitHub</h3>
      <div class="gh-row">
        {% if snap.gh_stars is not none %}
        <div class="gh-stat">
          <div class="lbl">Stars</div>
          <div class="val">{{ "{:,}".format(snap.gh_stars) }}</div>
        </div>
        {% endif %}
        {% if snap.gh_forks is not none %}
        <div class="gh-stat">
          <div class="lbl">Forks</div>
          <div class="val">{{ "{:,}".format(snap.gh_forks) }}</div>
        </div>
        {% endif %}
        {% if snap.gh_open_issues is not none %}
        <div class="gh-stat">
          <div class="lbl">Open Issues</div>
          <div class="val">{{ snap.gh_open_issues }}</div>
        </div>
        {% endif %}
        {% if gh_push_age_days is not none %}
        <div class="gh-stat">
          <div class="lbl">Last Push</div>
          <div class="val {{ age_cls(gh_push_age_days) }}">{{ gh_push_age_days }}d ago</div>
        </div>
        {% endif %}
      </div>
    </div>
    {% endif %}

    <!-- Social (shown only when data exists) -->
    {% if snap.x_followers is not none or snap.x_last_tweet is not none %}
    <div class="card">
      <h3>Social (X)</h3>
      <div class="gh-row">
        {% if snap.x_followers is not none %}
        <div class="gh-stat">
          <div class="lbl">Followers</div>
          <div class="val">{{ "{:,}".format(snap.x_followers) }}</div>
        </div>
        {% endif %}
        {% if x_tweet_age_days is not none %}
        <div class="gh-stat">
          <div class="lbl">Last Tweet</div>
          <div class="val {{ age_cls(x_tweet_age_days) }}">{{ x_tweet_age_days }}d ago</div>
        </div>
        {% endif %}
      </div>
    </div>
    {% endif %}

    <!-- Alert History -->
    <div class="card full">
      <h3>Alert History · this subnet</h3>
      {% if alerts %}
      {% for alert in alerts %}
      <div class="alert-item">
        <div class="alert-type">{{ alert.alert_type.replace("_", " ").title() }}</div>
        <div class="alert-desc">{{ alert.description }}</div>
        <div class="alert-time">{{ alert.fired_at }}</div>
      </div>
      {% endfor %}
      {% else %}
      <p class="empty">No alerts fired for this subnet</p>
      {% endif %}
    </div>

  </div>
</div>
</body>
</html>
```

- [ ] **Step 4: Run the detail page tests**

```bash
pytest tests/web/test_routes.py -v -k "subnet_detail"
```

Expected: all 3 tests PASSED

- [ ] **Step 5: Run the full test suite**

```bash
pytest -v --ignore=tests/test_main.py --ignore=tests/collectors/test_x_scraper.py
```

Expected: all tests PASS. (test_main.py and test_x_scraper.py have pre-existing env/dependency issues unrelated to this feature.)

- [ ] **Step 6: Commit**

```bash
git add web/templates/subnet.html tests/web/test_routes.py
git commit -m "feat: add subnet detail scorecard page"
```

---

## Self-Review

**Spec coverage:**
- [x] Leaderboard: real subnet name, Score, Em.Rank, MC.Rank, MCap USD, Emit/day, Trend — Task 2
- [x] Clickable rows → `/subnet/{netuid}` — Task 2
- [x] `fmt_usd` macro with K/M suffix — Task 2
- [x] Trend arrows with 24h-ago comparison — Task 1 (`get_emission_rank_24h_ago`) + Task 2 (route logic)
- [x] Score breakdown with visual bars and plain-English reasoning — Task 3
- [x] Chain stats grid (MCap, emission, price, volume, neurons, reg cost, owner) — Task 3
- [x] GitHub card (conditional) — Task 3
- [x] Social card (conditional) — Task 3
- [x] Alert history for subnet (last 10) — Task 1 (`get_alerts_for_netuid`) + Task 3
- [x] Back link + external links in header — Task 3
- [x] `get_latest_snapshots` and API endpoints unchanged — routes.py preserves them

**Placeholder scan:** None found. All steps have complete code.

**Type consistency:**
- `get_latest_snapshots_with_registry` → `list[aiosqlite.Row]` — used in both dashboard and subnet_detail routes ✓
- `get_emission_rank_24h_ago` → `dict[int, Optional[int]]` — accessed as `trend_raw.get(netuid)` ✓
- `enriched` list contains plain `dict` objects — Jinja accesses `row.name`, `row.netuid` etc. via dict key fallback ✓
- `snap` in subnet_detail is `dict(aiosqlite.Row)` — Jinja accesses `snap.gh_last_push` etc. ✓
