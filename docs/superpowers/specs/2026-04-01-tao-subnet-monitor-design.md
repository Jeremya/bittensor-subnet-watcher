# TAO Subnet Monitor — Design Spec
**Date:** 2026-04-01  
**Status:** Approved — ready for implementation planning  
**Review:** CEO plan review complete, all gaps resolved

---

## Overview

A personal monitoring app for Bittensor subnets. Watches all active subnets (~100+), scores them using a three-factor model, fires Telegram alerts when thresholds are crossed, and serves a simple dashboard showing the leaderboard + alert feed.

---

## Decisions Made

| Question | Answer |
|---|---|
| Stack | Python backend + simple web UI (FastAPI + Jinja2) |
| Alerts | Telegram bot (send-only) |
| X/Twitter data | Scraped via headless Chromium (no API, best-effort) |
| Subnet scope | All active subnets (full market scan) |
| Dashboard primary view | Leaderboard (left) + Alert feed (right) |
| Poll frequency | Every 15 minutes (GitHub: 60 min, Registry: daily) |
| Architecture pattern | Monolithic Python daemon (Option A) |

---

## Section 1: Architecture

Single process. Entry point: `python main.py` — starts `AsyncIOScheduler` and FastAPI/uvicorn together in the same event loop via `asyncio.gather()`.

```python
# main.py pattern
async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(poll_cycle, 'interval', minutes=15,
                      max_instances=1, misfire_grace_time=60)
    scheduler.add_job(github_collect, 'interval', minutes=60,
                      max_instances=1)
    scheduler.add_job(registry_refresh, 'interval', hours=24)
    scheduler.add_job(prune_snapshots, 'interval', hours=24)
    scheduler.start()
    config = uvicorn.Config(app, host="0.0.0.0", port=settings.DASHBOARD_PORT)
    server = uvicorn.Server(config)
    await server.serve()

asyncio.run(main())
```

```
┌─────────────────────────────────────────────────────┐
│                   tao-monitor process                │
│                                                      │
│  ┌─────────────┐   ┌──────────────┐   ┌──────────┐  │
│  │  Scheduler  │──▶│  Collectors  │──▶│  SQLite  │  │
│  │ (AsyncIO-   │   │  (15-min     │   │  (WAL)   │  │
│  │  Scheduler) │   │   pipeline)  │   └────┬─────┘  │
│  └─────────────┘   └──────┬───────┘        │        │
│                           │                │        │
│                    ┌──────▼───────┐        │        │
│                    │  Alert Engine│◀───────┘        │
│                    │  (scoring +  │                  │
│                    │   thresholds)│                  │
│                    └──────┬───────┘                  │
│                           │                          │
│              ┌────────────┴────────────┐             │
│              │                         │             │
│       ┌──────▼──────┐          ┌───────▼──────┐      │
│       │  Telegram   │          │   FastAPI    │      │
│       │    Bot      │          │  + Jinja2    │      │
│       └─────────────┘          └──────────────┘      │
└─────────────────────────────────────────────────────┘
```

**Directory layout:**
```
tao-monitor/
├── main.py
├── config.py           # scoring constants (40/30/30 weights) + thresholds
├── models.py           # SubnetSnapshot dataclass + shared types
├── utils.py            # async_retry() utility
├── collectors/
│   ├── price.py        # PriceCollector — runs every 15 min
│   ├── chain.py        # ChainCollector — runs every 15 min, bt.AsyncSubtensor singleton
│   ├── github.py       # GitHubCollector — runs every 60 min
│   ├── x_scraper.py    # XCollector — runs every 15 min, sequential, max 30/cycle
│   └── registry.py     # SubnetRegistry — runs daily
├── engine/
│   ├── scorer.py       # compute_yield_score(), compute_quality_score(), compute_momentum_score()
│   └── alerts.py       # check_emission_divergence(), check_dead_github(), ... (one fn per alert)
├── db/
│   └── database.py     # all SQL, schema init, WAL mode
├── bot/
│   └── telegram.py     # send-only, retry on RetryAfter, fail-fast on Unauthorized
├── web/
│   ├── routes.py
│   └── templates/
│       └── index.html  # two-column layout, empty states, 60s meta-refresh
├── data/               # gitignored
├── logs/               # gitignored
├── .env                # gitignored
├── .env.example
├── .gitignore
├── com.taomonitor.plist.example   # launchd template for always-on macOS
└── requirements.txt
```

---

## Section 2: Data Collection

**Poll schedule:**
- Every 15 min: `PriceCollector`, `ChainCollector`, `XCollector`
- Every 60 min: `GitHubCollector` (rate limit: 60 req/hr unauthenticated, ~50 subnets have repos)
- Daily: `SubnetRegistry`, snapshot pruning

Each collector returns a `SubnetSnapshot` (defined in `models.py`). Failed collectors write `None` for their fields — the rest still land. All 15-min collectors run in parallel via `asyncio.gather()`.

| Collector | Source | Data fetched | Schedule |
|---|---|---|---|
| `PriceCollector` | tao.app API | alpha price, mcap, 24h volume, emission rank | 15 min |
| `ChainCollector` | Bittensor SDK (`bt.AsyncSubtensor`) — **singleton**, reconnect on fail | daily emissions, metagraph size (n_neurons), registration cost | 15 min |
| `GitHubCollector` | GitHub API (`GITHUB_TOKEN` recommended, not required) | last push date, open issues, stars, forks | **60 min** |
| `XCollector` | Headless Chromium — **sequential**, 2s delay, max 30 subnets/cycle | latest tweet date, follower count | 15 min |
| `SubnetRegistry` | taostat subnets JSON + taomarketcap.com | subnet name, team, website — keeps old data on 404 | **daily** |

**Health check:** If >50% of subnets return `None` for `daily_emission_tao` after a poll, send a Telegram warning: `⚠️ ChainCollector: >50% of subnets missing emission data. Check logs.`

---

## Section 3: Scoring & Alert Engine

### Three-Score System (each 0–100)

Weights are defined as constants in `config.py` with inline comments explaining the investment thesis.

```python
# config.py — scoring weights
# Yield is retained as a relative-value signal; Spec 421 is the primary protocol thesis.
# Quality gates out dead subnets. Momentum confirms entry timing.
YIELD_WEIGHT = 0.40
QUALITY_WEIGHT = 0.30
MOMENTUM_WEIGHT = 0.30
```

| Score | Formula | Weight |
|---|---|---|
| **Yield Score** | `(daily_emission_TAO × TAO_price × 365) / alpha_mcap_usd × 100` — normalized 0–100 across all subnets. Guard: skip if `alpha_mcap <= 0`. If all yields identical (stddev=0), default all to 50. | 40% |
| **Quality Score** | GitHub recency: last push <30d=40pts, <90d=20pts, else 0. + n_neurons normalized + reg_cost normalized. Skip sub-component if input is None. | 30% |
| **Momentum Score** | 7-day alpha price change + 7-day volume change + emission rank delta. Returns `None` if fewer than 2 snapshots exist for the subnet. | 30% |

**Composite score** = weighted sum of non-None sub-scores. Leaderboard sorts by composite descending.

### Eight Alert Types

Each is a standalone function in `engine/alerts.py`. `evaluate()` calls all eight and collects results.

Dedup: before firing, query `SELECT COUNT(*) FROM alerts WHERE netuid=? AND alert_type=? AND fired_at > datetime('now', '-6 hours')`. If count > 0, skip.

| # | Function | Trigger |
|---|---|---|
| 1 | `check_emission_divergence` | emission_rank ÷ mcap_rank > 1.5 |
| 2 | `check_dead_github` | no commit in 60+ days AND mcap > $500K |
| 3 | `check_ownership_transfer` | new coldkey registered as subnet owner |
| 4 | `check_whale_inflow` | single wallet stakes >5% of alpha supply in one poll |
| 5 | `check_emission_drop` | subnet loses >2 emission rank positions in 24h |
| 6 | `check_github_spike` | stars or forks double in 24h |
| 7 | `check_social_silence` | no tweet in 14+ days (X scrape only, best-effort) |
| 8 | `check_new_entry` | subnet appears in registry for first time |

**Telegram message format:**
```
🔔 [SN42 — Bittensor] Emission Divergence
Emission rank #3 / MCap rank #18 → ratio 6.0x
Threshold: 1.5x
```

Send with 0.1s delay between messages to avoid Telegram flood control.

---

## Section 4: Dashboard UI

Single-page layout, two columns. Jinja2 + minimal CSS. No JavaScript framework.

**Left column — Subnet Leaderboard:**
- Table sorted by composite score (descending)
- Columns: Rank, Subnet name + netuid, Yield Score, Quality Score, Momentum Score, Composite, 24h change indicator (▲/▼), last updated
- Color bands: green (composite >70), yellow (40–70), red (<40)
- Empty state: "Waiting for first poll..." if no data in DB
- Auto-refreshes via `<meta http-equiv="refresh" content="60">`

**Right column — Alert Feed:**
- Reverse-chronological list of fired alerts
- Each entry: timestamp, subnet name, alert type badge (color-coded), human-readable description
- Shows last 50 alerts
- Empty state: "No alerts yet"

**Header bar:** App name, last poll time, count of subnets monitored, TAO price.

**FastAPI routes:**
- `GET /` — renders full dashboard page
- `GET /api/snapshots` — returns latest snapshot per subnet as JSON
- `GET /api/alerts` — returns last 50 alerts as JSON

No auth — runs locally only.

---

## Section 5: SQLite Schema

```sql
-- Enable WAL mode for concurrent read+write
PRAGMA journal_mode=WAL;

CREATE TABLE snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid             INTEGER NOT NULL,
    polled_at          DATETIME NOT NULL,
    -- Price data
    alpha_price        REAL,
    alpha_mcap         REAL,
    volume_24h         REAL,
    emission_rank      INTEGER,
    -- Chain data
    daily_emission_tao REAL,
    n_neurons          INTEGER,
    reg_cost           REAL,
    -- GitHub data (updated on 60-min schedule)
    gh_last_push       DATETIME,
    gh_stars           INTEGER,
    gh_forks           INTEGER,
    gh_open_issues     INTEGER,
    -- X/social data (best-effort)
    x_last_tweet       DATETIME,
    x_followers        INTEGER,
    -- Computed scores (None if insufficient data)
    yield_score        REAL,
    quality_score      REAL,
    momentum_score     REAL,
    composite_score    REAL
);

CREATE TABLE alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at      DATETIME NOT NULL,
    netuid        INTEGER NOT NULL,
    subnet_name   TEXT NOT NULL,
    alert_type    TEXT NOT NULL,
    description   TEXT NOT NULL,
    current_value REAL,
    threshold     REAL,
    notified      INTEGER DEFAULT 0  -- 1 = Telegram sent
);

CREATE TABLE subnet_registry (
    netuid     INTEGER PRIMARY KEY,
    name       TEXT,
    team       TEXT,
    website    TEXT,
    github_url TEXT,
    x_handle   TEXT,
    updated_at DATETIME
);

CREATE INDEX idx_snapshots_netuid_time ON snapshots (netuid, polled_at);
CREATE INDEX idx_alerts_fired_at ON alerts (fired_at DESC);
CREATE INDEX idx_alerts_dedup ON alerts (netuid, alert_type, fired_at);
```

Pruning: daily job deletes rows where `polled_at < datetime('now', '-30 days')`.

---

## Section 6: Configuration & Telegram Bot

**`.env` file (validated at startup — process exits if required vars missing):**
```
TELEGRAM_BOT_TOKEN=...        # required — validated at startup
TELEGRAM_CHAT_ID=...          # required — validated at startup
GITHUB_TOKEN=                 # optional — recommended, raises limit to 5000/hr (read:public scope only)
POLL_INTERVAL_MINUTES=15      # default: 15
DASHBOARD_PORT=8000           # default: 8000
DB_PATH=./data/monitor.db     # default: ./data/monitor.db
LOG_LEVEL=INFO                # default: INFO
```

Alert thresholds live in `config.py` as constants — not in `.env`.

**Startup validation:**
```python
def validate_config():
    required = ['TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
    for key in required:
        if not os.getenv(key):
            sys.exit(f"[STARTUP ERROR] Missing required env var: {key}")
    # Test Telegram token validity — send a startup message or raise Unauthorized
```

**Telegram — send-only flow:**
1. Alert engine writes row to `alerts` with `notified=0`
2. Post-poll hook queries `WHERE notified=0`, sends each with 0.1s between messages
3. Marks `notified=1` after successful send
4. `Unauthorized` → caught at startup, `exit(1)`
5. `RetryAfter` → sleep `e.retry_after` seconds, retry once
6. `NetworkError` → log, leave `notified=0` for next poll

---

## Section 7: Error Handling & Logging

**Error handling — per collector:**
- `PriceCollector`: catch `asyncio.TimeoutError`, `ClientResponseError`, `json.JSONDecodeError`, `KeyError` → log warning, return None fields
- `ChainCollector`: `os.environ['SSL_CERT_FILE'] = certifi.where()` at module import. Catch broad `Exception` → log with context (`collector=chain netuid=? error=?`), return None fields. Singleton `bt.AsyncSubtensor` reconnects on next poll if connection dropped.
- `GitHubCollector`: catch `ClientResponseError` 404 (repo deleted), 403 (rate limit), timeout → log, return None fields
- `XCollector`: catch all Playwright errors → log, return None. Never blocks other collectors.
- All SQLite writes: catch `sqlite3.OperationalError` → log error, skip write, continue

**`utils.py`:**
```python
async def async_retry(fn, retries=1, delay=5):
    """Retry an async function once after delay seconds on any exception."""
    ...
```

**Logging** — structured plain-text, stdout + rotating file (`logs/monitor.log`, 10MB, 3 backups):

```
[STARTUP] db=ok telegram=ok scheduler=ok dashboard=http://localhost:8000
[POLL_START] cycle_id=1234
[COLLECTOR] name=price ok=100 errors=0
[COLLECTOR] name=chain ok=87 errors=13
[COLLECTOR] name=x ok=28 errors=2 (best-effort)
[POLL] netuid=42 yield=87.3 quality=61.0 momentum=44.2 composite=66.7
[ALERT] netuid=42 type=emission_divergence value=3.2 threshold=1.5
[TELEGRAM] sent=3 failed=0
[POLL_END] cycle_id=1234 duration=94s subnets=98 errors=15
```

**Deployment:**
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID
python main.py         # starts scheduler + dashboard on :8000
```

For always-on: copy `com.taomonitor.plist.example` to `~/Library/LaunchAgents/com.taomonitor.plist`, edit the path, then:
```bash
launchctl load ~/Library/LaunchAgents/com.taomonitor.plist
```
Auto-restarts on crash and on reboot.

**`.gitignore`:**
```
.env
data/
logs/
.venv/
__pycache__/
*.pyc
*.db
*.db-journal
.DS_Store
```

---

## Investment Criteria Reference

See `2026-04-01-tao-investment-criteria.md` for the full signal framework (signal stack, lifecycle stages, red flags, data sources).
