# Design: Analyst Tracking, Milestone Detection & Subnet Categories

**Date:** 2026-04-21  
**Status:** Approved  
**Motivation:** Tweet from @0xai_dev (2034255124659384621) documents a 2-day price lag after SN3's Covenant-72B announcement — a "cognitive arbitrage" window between AI researchers and crypto investors. This design adds three features to detect and surface that window.

---

## Overview

Five self-contained additions:

1. **Analyst Collector** — hourly scrape of watchlist analyst X accounts; detects subnet mentions; fires Telegram alert + dashboard coverage badge
2. **Milestone Collector** — 6-hourly poll of arXiv and HuggingFace for subnet-linked publications; fires Telegram alert; provides the leading signal before analyst coverage
3. **Subnet Categories** — sector labels auto-suggested from GitHub README, confirmed/overridden in dashboard; leaderboard gains a category filter sidebar
4. **AI Signal Interpreter** — when a milestone is detected, calls Claude API to generate a plain-English summary + investment take; stored alongside the milestone row; shown in dashboard and Telegram alert
5. **Signal Convergence detector** — fires a high-conviction "Pump Lab" alert when 2+ independent signals (milestone, analyst mention, whale inflow, emission spike) hit the same subnet within a rolling 24h window

Together: paper drops → milestone alert + AI take → analyst writes about it → analyst alert + coverage badge → convergence fires if whale inflow also present → you act before the market prices it in.

---

## Architecture & Data Flow

```
main.py (scheduler)
  ├── every 15 min  → ChainCollector + scorer + alerts (unchanged)
  │                    alerts gains: check_convergence() (NEW)
  ├── every 60 min  → GitHubCollector (gains: category auto-suggest on README fetch)
  ├── every 60 min  → AnalystCollector (NEW)
  ├── every 6 hours → MilestoneCollector (NEW)
  │                    on new milestone: calls Claude API → stores ai_summary + ai_take
  └── best-effort   → XCollector (unchanged)

alerts.py
  ├── check_analyst_mention()  (NEW)
  ├── check_milestone()        (NEW) — includes AI take in Telegram message
  └── check_convergence()      (NEW) — fires when 2+ signals hit same subnet in 24h

dashboard
  ├── leaderboard: coverage badge + category label + category filter sidebar
  ├── subnet detail: milestone timeline (with AI summaries) + analyst mentions feed
  └── GET/POST /analysts: manage watchlist handles
```

### New DB Tables

```sql
CREATE TABLE analyst_watchlist (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    handle  TEXT NOT NULL UNIQUE,   -- without @
    added_at TEXT NOT NULL,         -- ISO8601 UTC
    source  TEXT NOT NULL DEFAULT 'dashboard'  -- 'config' | 'dashboard'
);

CREATE TABLE analyst_mentions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    analyst_handle TEXT NOT NULL,
    netuid        INTEGER NOT NULL,
    tweet_url     TEXT NOT NULL UNIQUE,   -- deduplication key
    tweet_text    TEXT,
    mentioned_at  TEXT NOT NULL,          -- ISO8601 UTC
    notified      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE subnet_milestones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid         INTEGER NOT NULL,
    milestone_type TEXT NOT NULL,         -- 'arxiv' | 'huggingface'
    title          TEXT NOT NULL,
    url            TEXT NOT NULL UNIQUE,  -- deduplication key
    published_at   TEXT NOT NULL,         -- ISO8601 UTC
    notified       INTEGER NOT NULL DEFAULT 0
);
```

### Registry Table Changes

```sql
ALTER TABLE registry ADD COLUMN category TEXT;
ALTER TABLE registry ADD COLUMN category_confirmed INTEGER NOT NULL DEFAULT 0;
-- 0 = auto-suggested (re-evaluated on next GitHub refresh)
-- 1 = user-confirmed (never overwritten by auto-suggest)
```

---

## Feature 1: Analyst Collector (`collectors/analyst.py`)

### Config (`config.py` additions)

```python
ANALYST_HANDLES: list[str] = [
    h.strip() for h in os.getenv("ANALYST_HANDLES", "").split(",") if h.strip()
]
ANALYST_TWEET_LOOKBACK_HOURS: int = 25   # slightly > poll interval to avoid gaps
ANALYST_COVERAGE_DECAY_HOURS: int = 72   # coverage badge visible for 72h after mention
```

Analyst handles are seeded from `ANALYST_HANDLES` env var and supplemented by handles added via the dashboard `/analysts` page (stored in a new `analyst_watchlist` DB table).

### Subnet Matching

For each tweet text, match against all registry subnets using:
- Subnet name (case-insensitive, whole-word): e.g. `"Templar"`, `"Apex"`
- SN-pattern: `\bSN\d+\b` → resolve netuid to registry entry

All matching subnets are recorded — a single tweet can produce multiple `analyst_mentions` rows (one per matched subnet).

### Per-Run Flow

1. Load analyst handles: union of `ANALYST_HANDLES` config + `analyst_watchlist` DB table
2. For each handle, reuse `get_browser_page()` from `x_scraper.py`, scrape last ~10 tweets
3. Filter to tweets with `mentioned_at > now - ANALYST_TWEET_LOOKBACK_HOURS`
4. For each tweet, run subnet matching against registry
5. For each match, insert into `analyst_mentions` if `tweet_url` not already present (UNIQUE constraint handles dedup)
6. Newly inserted rows with `notified = 0` are picked up by `alerts.check_analyst_mention()`

### Alert Format

```
📡 @0xai_dev mentioned SN3 (Templar)
"Bittensor Is The Chosen One" → https://x.com/0xai_dev/status/...
```

### Coverage Badge

A subnet has active coverage if any `analyst_mentions` row satisfies:
`netuid = ? AND mentioned_at > now - ANALYST_COVERAGE_DECAY_HOURS`

Boolean — no float score. Leaderboard shows a `📡` badge with tooltip "Last covered Xh ago by @handle".

---

## Feature 2: Milestone Collector (`collectors/milestone.py`)

Runs every 6 hours. No API key required for either source.

### arXiv

Uses the arXiv public API (`http://export.arxiv.org/api/query`). Search query per subnet:
```
search_query=all:"bittensor" AND all:"{subnet_name}"
```
Only subnets with `gh_repo` set are queried (indicates active development). Results filtered to `published_at > last_check`. Parsed from Atom XML response.

### HuggingFace

Uses the `huggingface_hub` Python package (`list_models(search="{subnet_name} bittensor")`). No authentication required for public models. Results filtered by `lastModified > last_check`.

### Deduplication

Both sources use URL as the UNIQUE key in `subnet_milestones`. Duplicate inserts are silently ignored.

### Alert Format

```
🔬 SN3 (Templar) — new arXiv paper
"SparseLoCo: Gradient Compression for Decentralized LLM Training"
→ https://arxiv.org/abs/2603.08163
```

### Last-Check State

Stored as a Unix timestamp in a `collector_state` key-value table (add if not exists):
```sql
CREATE TABLE IF NOT EXISTS collector_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```
Keys: `milestone_last_arxiv_check`, `milestone_last_hf_check`.

---

## Feature 3: Subnet Categories

### Category Map (`config.py`)

```python
SUBNET_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "AI Training":        ["training", "finetune", "gradient", "pretraining", "llm"],
    "Post-Training/RLHF": ["rlhf", "alignment", "reinforcement", "reward model"],
    "Data / Retrieval":   ["dataset", "scraping", "retrieval", "indexing", "storage"],
    "Quant / Finance":    ["trading", "quant", "alpha", "market", "prediction"],
    "Privacy / Compute":  ["zkp", "zero-knowledge", "secure compute", "homomorphic"],
    "Biomedical":         ["biomedical", "protein", "genomics", "drug", "clinical"],
    "Infrastructure":     ["bandwidth", "networking", "storage", "compute", "validator"],
    "Other":              [],  # fallback
}
```

### Auto-Suggest Logic (in `GitHubCollector`)

After fetching README text, run keyword scan (case-insensitive). First category with a keyword match wins. Result stored with `category_confirmed = 0`. If `category_confirmed = 1`, skip — user has locked the label.

### Dashboard Changes

**Leaderboard:**
- Category label chip beside subnet name (muted style)
- `📡` coverage badge if active analyst mention
- Left sidebar: category filter chips (multi-select OR logic; default = All)

**Subnet detail page:**
- Category dropdown (pre-filled, save button sets `category_confirmed = 1`)
- "Analyst mentions" feed: list of matching tweets with handle, timestamp, excerpt
- "Milestones" timeline: arXiv/HF entries with type icon, title, link, date

**Analysts page (`/analysts`):**
- Table of watched handles (source: config or dashboard-added)
- Add handle form (POST stores to `analyst_watchlist` table)
- Remove button (config-seeded handles shown but not removable from UI — remove from env)

---

## Feature 4: AI Signal Interpreter

Runs inside `MilestoneCollector` immediately after a new milestone row is inserted. Uses the Claude API (claude-haiku-4-5 for cost efficiency — fast, cheap, adequate for structured summaries).

### DB Changes

```sql
ALTER TABLE subnet_milestones ADD COLUMN ai_summary TEXT;   -- 1-2 sentence plain-English description
ALTER TABLE subnet_milestones ADD COLUMN ai_take TEXT;      -- 1 sentence investment implication
```

### Prompt Design

```
You are a Bittensor investment analyst. Given a new publication from a Bittensor subnet team,
write two things:
1. SUMMARY: 1-2 sentences explaining what was built or published, in plain English for a
   non-technical investor.
2. TAKE: 1 sentence on what this means for the subnet's investment thesis (positive/neutral/negative
   and why).

Subnet: {subnet_name} (SN{netuid})
Publication type: {milestone_type}  (arxiv | huggingface)
Title: {title}
URL: {url}

Reply in JSON: {{"summary": "...", "take": "..."}}
```

### Failure Handling

- If Claude API call fails or returns malformed JSON: log warning, leave `ai_summary` and `ai_take` as NULL. Milestone is still stored and alerted — AI gloss is best-effort.
- `ANTHROPIC_API_KEY` added to `.env.example` as optional. If not set, skip AI interpretation silently.

### Telegram Alert With AI Take

```
🔬 SN3 (Templar) — new arXiv paper
"SparseLoCo: Gradient Compression for Decentralized LLM Training"

Summary: The Templar team published a gradient compression technique that reduces
communication overhead by 146x, enabling more miners to participate in training runs.

Take: Strong technical signal — this is the core IP behind Covenant-72B and validates
the subnet's long-term defensibility.

→ https://arxiv.org/abs/2603.08163
```

### Config

```python
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
AI_INTERPRETER_MODEL: str = "claude-haiku-4-5-20251001"
```

---

## Feature 5: Signal Convergence Detector

Runs inside `alerts.py` at the end of each 15-min poll cycle (same cadence as existing alert checks). Looks back 24h and counts how many *independent* signal types fired for each subnet.

### Signal Types Tracked

| Signal type | Source |
|---|---|
| `milestone` | `subnet_milestones` — new arXiv or HF entry |
| `analyst_mention` | `analyst_mentions` — new KOL tweet match |
| `whale_inflow` | existing `tao_outflow` / `whale_inflow` alerts |
| `emission_spike` | existing `emission_drop` alert (inverse: spike upward) |
| `github_spike` | existing `github_spike` alert |

### Trigger Rule

Fire a convergence alert when **2 or more distinct signal types** appear for the same `netuid` within a rolling 24h window, **and** no convergence alert has fired for that subnet in the past 48h (separate cooldown from individual alerts).

Stored in `alerts` table with `alert_type = 'convergence'` — reuses existing schema.

### Alert Format

```
🚨 HIGH CONVICTION — SN3 (Templar)
3 signals converged in 24h:
  🔬 arXiv paper published
  📡 @0xai_dev mentioned it (KOL)
  🐋 Whale inflow >5% of pool

This is the cognitive-lag window. Price hasn't moved yet.
```

### Why Not Score-Based

A numeric convergence score would require calibration. A simple count of distinct signal types is transparent, debuggable, and matches how you'd reason about it manually: "two things fired at once = worth paying attention."

---

## Error Handling & Failure Modes

- **Analyst scrape fails** (Playwright timeout, X login wall): log warning, skip handle, continue. Same pattern as `XCollector`.
- **arXiv API down**: log warning, skip cycle, retry next 6h window.
- **HuggingFace search returns garbage**: URL dedup + `published_at` filter are the only validation needed — bad results simply don't match existing subnets.
- **No subnets match a tweet**: silently discard. No alert, no DB row.
- **Category auto-suggest on subnet with no README**: `category` stays NULL, shown as "Unknown" in dashboard.

---

## Testing

- `test_analyst_matching.py` — unit tests for subnet name + SN-pattern regex against sample tweet texts
- `test_category_suggest.py` — unit tests for keyword scan against sample README excerpts
- `test_milestone_dedup.py` — verify UNIQUE constraint on URL suppresses duplicate rows
- `test_convergence.py` — unit tests for signal count logic: 0/1/2/3 signal types, cooldown window, distinct-type deduplication
- `test_ai_interpreter.py` — mock Claude API response; verify JSON parsing + NULL fallback on malformed response
- Integration: existing `pytest.ini` pattern; no live network calls in tests (mock arXiv/HF/Claude responses)

---

## Out of Scope

- Discord scanning — scraping Discord without a bot token is not feasible
- Sentiment scoring of analyst tweets (positive/negative) — too noisy, not planned
- Tracking analyst follower growth over time — informational only, not needed
- Paid HuggingFace or arXiv APIs
- Auto-mapping subnet names to HuggingFace org slugs — search by name is sufficient
- Heat scoring KOL mentions by follower count — boolean coverage is sufficient for now
