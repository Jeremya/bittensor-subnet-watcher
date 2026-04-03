# Dashboard Investor View — Design Spec

**Goal:** Transform the monitoring dashboard into a discovery tool for investors: a scannable leaderboard with the right columns, and a per-subnet scorecard reachable by clicking any row.

**Primary use case:** Discovery — "what should I be buying right now?" Scan the leaderboard by composite score, spot the emission/mcap divergence opportunity, click through to understand the score and verify the team is active.

**Approach:** Pure server-side. Jinja2 templates, FastAPI routes, zero new JS dependencies. No sortable columns — the score ranking is the discovery interface.

---

## What Changes

### 1. Leaderboard (`index.html` + `routes.py` + `db/database.py`)

**New columns replacing the current table:**

| Column | Source | Notes |
|--------|--------|-------|
| # | loop.index | unchanged |
| Subnet | `subnet_registry.name` + `snapshots.netuid` | Real name in teal, `SN{n}` in grey. Falls back to `SN{n}` if no registry entry |
| Score | `composite_score` | Colour-coded: ≥70 green, ≥40 yellow, <40 red |
| Em. Rank | `emission_rank` | `#N` |
| MC. Rank | computed at query time | rank of `alpha_mcap_tao` desc across all latest snapshots |
| MCap | `alpha_mcap_usd` | `$2.1M` / `$890K` format. Falls back to `{alpha_mcap_tao:.0f} τ` if no USD price |
| Emit/day | `daily_emission_tao` | `142 τ` |
| Trend | computed from 24h-ago snapshot | `▲` green / `→` grey / `▼` red vs emission rank 24h prior. `—` if no prior data |

**Row behaviour:** Each `<tr>` is a link (`<a>` wrapping the row or `onclick`) to `/subnet/{netuid}`.

**Columns removed:** Yield, Quality, Momentum sub-scores (moved to detail page only — too noisy in the leaderboard).

---

### 2. Subnet Detail Page (new route + `subnet.html`)

**Route:** `GET /subnet/{netuid}` → `web/templates/subnet.html`

**Header bar:**
- `← Leaderboard` back link
- Subnet name + `SN{netuid}` + `#{score_rank} by score`
- External links (right-aligned): `GitHub ↗` (if `github_url`), `Website ↗` (if `website`), `@handle ↗` (if `x_handle`)

**Grid layout — 4 cards in a 2×2 grid, alert history spans full width below:**

#### Card 1 — Score Breakdown
Three score rows, each with:
- Label (`Yield` / `Quality` / `Momentum`)
- Visual bar (filled % of 100, colour-coded)
- Numeric value
- Plain-English reasoning (see below)

Reasoning strings:
- **Yield:** `em #{emission_rank} vs mc #{mcap_rank} → {ratio:.1f}× gap` (ratio = mcap_rank / emission_rank)
- **Quality:** `pushed {N}d ago · {gh_stars} ⭐` — or `no GitHub data` if `gh_last_push` is None
- **Momentum:** `+{N} em ranks in 7 polls` / `−{N} em ranks in 7 polls` / `stable` — comparing current `emission_rank` to the snapshot from 7 polls prior for this netuid

Composite score displayed below a divider, larger font.

#### Card 2 — Chain Stats
Two-column stat grid:

| Stat | Source |
|------|--------|
| MCap | `alpha_mcap_usd` (formatted) + `alpha_mcap_tao τ` secondary |
| Daily Emission | `daily_emission_tao τ / day` + `Rank #{emission_rank} of {total}` |
| Alpha Price | `alpha_price_tao τ` |
| 24h Volume | `volume_24h_alpha α` |
| Neurons | `n_neurons active` |
| Reg Cost | `reg_cost_tao τ` |
| Owner | Full `owner_coldkey` in monospace, dimmed. Omitted if None. |

#### Card 3 — GitHub
Shown only if any GitHub data exists (`gh_last_push` or `gh_stars` is not None).

Stats: Stars, Forks, Open Issues, Last Push (colour-coded: ≤7d green, ≤30d yellow, >30d red).

Hidden entirely (card not rendered) if no GitHub data.

#### Card 4 — Social (X)
Shown only if `x_followers` or `x_last_tweet` is not None.

Stats: Followers, Last Tweet (colour-coded: ≤3d green, ≤14d yellow, >14d red).

Hidden entirely if no social data.

#### Full-width — Alert History
Last 10 alerts for this subnet, most recent first. Same card style as the main alert feed. If no alerts: `No alerts fired for this subnet`.

---

## DB Changes (`db/database.py`)

### New function: `get_latest_snapshots_with_registry(db)`
Returns latest snapshot per netuid LEFT JOINed with `subnet_registry`. Adds `name`, `github_url`, `x_handle`, `website` columns to each row. Ordered by `composite_score DESC NULLS LAST`.

Replaces the call to `get_latest_snapshots` in the dashboard route. The existing `get_latest_snapshots` function stays (used by the `/api/snapshots` endpoint and by `main.py`).

```sql
SELECT s.*, r.name, r.github_url, r.x_handle, r.website
FROM snapshots s
INNER JOIN (
    SELECT netuid, MAX(polled_at) AS max_ts FROM snapshots GROUP BY netuid
) latest ON s.netuid = latest.netuid AND s.polled_at = latest.max_ts
LEFT JOIN subnet_registry r ON s.netuid = r.netuid
ORDER BY s.composite_score DESC NULLS LAST
```

### New function: `get_emission_rank_24h_ago(db)`
Returns `{netuid: emission_rank}` mapping from the most recent snapshot that is ≥24h old for each netuid. Used by the dashboard route to compute trend arrows.

```sql
SELECT netuid, emission_rank
FROM snapshots s1
WHERE polled_at = (
    SELECT MAX(polled_at) FROM snapshots s2
    WHERE s2.netuid = s1.netuid
    AND s2.polled_at <= datetime('now', '-24 hours')
)
```

Returns a dict `{netuid: emission_rank}`. Trend arrow logic (in route, not template):
- `prev_rank is None` → `"—"`
- `current_rank < prev_rank` → `"▲"` (improved, green)
- `current_rank > prev_rank` → `"▼"` (worsened, red)
- `current_rank == prev_rank` → `"→"` (grey)

### New function: `get_subnet_detail(db, netuid)`
Returns the latest snapshot for a single netuid LEFT JOINed with `subnet_registry`.

```sql
SELECT s.*, r.name, r.github_url, r.x_handle, r.website
FROM snapshots s
LEFT JOIN subnet_registry r ON s.netuid = r.netuid
WHERE s.netuid = ?
ORDER BY s.polled_at DESC LIMIT 1
```

### New function: `get_alerts_for_netuid(db, netuid, limit=10)`
Returns most recent N alerts for a specific netuid.

```sql
SELECT * FROM alerts WHERE netuid = ? ORDER BY fired_at DESC LIMIT ?
```

---

## Route Changes (`web/routes.py`)

### Updated `dashboard` route
```python
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    snapshots = await get_latest_snapshots_with_registry(db)
    alerts = await get_last_50_alerts(db)
    trend_raw = await get_emission_rank_24h_ago(db)

    # Compute mcap_rank and trend arrows
    sorted_by_mcap = sorted(
        [s for s in snapshots if s["alpha_mcap_tao"] is not None],
        key=lambda s: s["alpha_mcap_tao"], reverse=True
    )
    mcap_rank = {s["netuid"]: i + 1 for i, s in enumerate(sorted_by_mcap)}

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
         "mcap_rank": mcap_rank.get(s["netuid"]),
         "trend": trend_arrow(s["netuid"], s["emission_rank"])}
        for s in snapshots
    ]

    return templates.TemplateResponse(request, "index.html", {
        "snapshots": enriched,
        "alerts": alerts,
        "last_poll": snapshots[0]["polled_at"] if snapshots else None,
        "subnet_count": len(snapshots),
    })
```

### New `subnet_detail` route
```python
@app.get("/subnet/{netuid}", response_class=HTMLResponse)
async def subnet_detail(request: Request, netuid: int):
    snap = await get_subnet_detail(db, netuid)
    if snap is None:
        return HTMLResponse("Subnet not found", status_code=404)

    alerts = await get_alerts_for_netuid(db, netuid, limit=10)

    # Compute mcap_rank and score_rank from latest snapshots
    all_snaps = await get_latest_snapshots_with_registry(db)
    total = len(all_snaps)
    sorted_by_mcap = sorted(
        [s for s in all_snaps if s["alpha_mcap_tao"] is not None],
        key=lambda s: s["alpha_mcap_tao"], reverse=True
    )
    mcap_rank = next(
        (i + 1 for i, s in enumerate(sorted_by_mcap) if s["netuid"] == netuid), None
    )
    score_rank = next(
        (i + 1 for i, s in enumerate(all_snaps) if s["netuid"] == netuid), None
    )

    # Yield reasoning
    em_rank = snap["emission_rank"]
    yield_why = "—"
    if em_rank and mcap_rank:
        ratio = mcap_rank / em_rank
        yield_why = f"em #{em_rank} vs mc #{mcap_rank} → {ratio:.1f}× gap"

    # Quality reasoning
    quality_why = "no GitHub data"
    if snap["gh_last_push"]:
        from datetime import datetime, timezone
        age = (datetime.now(timezone.utc) -
               datetime.fromisoformat(snap["gh_last_push"])).days
        stars = snap["gh_stars"] or 0
        quality_why = f"pushed {age}d ago · {stars:,} ⭐"

    # Momentum reasoning
    momentum_why = "no history"
    history = await get_snapshots_for_netuid(db, netuid, limit=8)
    if len(history) >= 2:
        oldest = history[-1]
        if oldest["emission_rank"] and em_rank:
            delta = oldest["emission_rank"] - em_rank
            if delta > 0:
                momentum_why = f"+{delta} em ranks in {len(history)-1} polls"
            elif delta < 0:
                momentum_why = f"{delta} em ranks in {len(history)-1} polls"
            else:
                momentum_why = "stable"

    return templates.TemplateResponse(request, "subnet.html", {
        "snap": dict(snap),
        "alerts": alerts,
        "mcap_rank": mcap_rank,
        "score_rank": score_rank,
        "total_subnets": total,
        "yield_why": yield_why,
        "quality_why": quality_why,
        "momentum_why": momentum_why,
    })
```

---

## Template Changes

### `web/templates/index.html`
- Replace table columns as per new leaderboard spec
- Each `<tr>` wrapped in or linked to `/subnet/{s.netuid}`
- Trend arrow coloured via Jinja: `▲` → `score-high`, `▼` → `score-low`, `→` / `—` → `score-null`
- MCap formatted with `$` prefix and K/M suffix (Jinja filter or inline logic)
- Remove Yield/Quality/Momentum sub-score columns

### `web/templates/subnet.html` (new)
- Standalone page with same dark theme as index.html (shared CSS inline or `<style>` block)
- Header bar with back link and external links
- 2×2 card grid + full-width alert history
- Score bars rendered as inline `<div>` width % via Jinja `{{ (score or 0)|int }}%`
- GitHub and Social cards wrapped in `{% if snap.gh_last_push or snap.gh_stars %}` / `{% if snap.x_followers or snap.x_last_tweet %}`
- MCap USD formatted using a Jinja macro `fmt_usd(v)` defined at the top of each template:
  ```jinja
  {% macro fmt_usd(v) %}
  {%- if v is none %}—
  {%- elif v >= 1_000_000 %}${{ "%.1f"|format(v / 1_000_000) }}M
  {%- elif v >= 1_000 %}${{ "%.0f"|format(v / 1_000) }}K
  {%- else %}${{ "%.0f"|format(v) }}
  {%- endif %}
  {% endmacro %}
  ```
  Fallback when `alpha_mcap_usd` is None: display `{{ "{:,.0f}".format(snap.alpha_mcap_tao) }} τ` instead.

---

## What's Not Changing

- `/api/snapshots` and `/api/alerts` endpoints — unchanged
- `get_latest_snapshots()` — kept as-is (used by main.py and API)
- `get_last_50_alerts()` — kept as-is
- Alert system, scoring engine, collectors — untouched
- Auto-refresh (60s `<meta>` tag) — kept on index, not added to detail page (detail is point-in-time)

---

## Files Touched

| File | Change |
|------|--------|
| `db/database.py` | +4 new functions (`get_latest_snapshots_with_registry`, `get_emission_rank_24h_ago`, `get_subnet_detail`, `get_alerts_for_netuid`). Existing `get_snapshots_for_netuid` used in detail route — no change needed. |
| `web/routes.py` | Update dashboard route, add subnet_detail route |
| `web/templates/index.html` | New leaderboard columns, row links |
| `web/templates/subnet.html` | New file — scorecard detail page |

Total: 4 files, ~200 lines net new.
