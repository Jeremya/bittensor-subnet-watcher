# Trust Phase — make the monitor's data and alerts believable

**Date:** 2026-07-01
**Status:** Approved
**Origin:** Database review of `data/monitor.db` (62,565 snapshots, Jun 15 – Jul 1 2026, 129 subnets).

## Problem

The database review found the monitor's biggest risks are not missing features but
broken trust in what it already reports:

1. **The X pipeline has never produced data in the live DB.** `x_last_tweet` and
   `x_followers` are NULL on every row; `analyst_mentions` has 0 rows despite 8
   watchlist handles. `hype_score` and the analyst feature run on air, and nothing
   surfaced this for weeks.
2. **Alert fatigue.** 16,449 alerts total; `emission_near_zero` fired 7,723 times
   (~48/day currently), `emission_divergence` 4,464. The handful of genuinely
   actionable alerts (important_buy/sell, whale_inflow, liquidity_floor) are buried.
3. **No pipeline observability.** A collector can die silently (see #1) with no
   dashboard or Telegram signal.
4. **Hygiene:** `backup_db_file` runs on every process start (5 `.bak` files created
   on 2026-07-01 alone from restarts), and `snapshots` has no retention policy.

Decision (user-confirmed): fix trust before building new capability. Registry
backfill (20 missing `github_url`, 22 missing `category`) and the ~21% slippage
null-rate investigation are **deferred** — record both in `TODOS.md`.

## Workstream 1: Cut the X pipeline cleanly

- Remove `XCollector` scheduling from `main.py`; delete `collectors/x_scraper.py`.
- `collectors/analyst.py` stops polling. The analyst dashboard tab remains but shows
  a "dormant: no data source" state instead of silently-empty feeds.
- `compute_hype_score` (`engine/scorer.py`) is removed from composite/swing
  weighting; `hype_score` is no longer written (column retained for history —
  honest NULL beats fabricated 0). The `social_silence` alert check is removed.
- Schema unchanged: `x_last_tweet`, `x_followers`, `analyst_mentions`,
  `analyst_watchlist` stay dormant. Re-enabling later (e.g. paid X API) is additive.
- New config flag `X_PIPELINE_ENABLED = False` guards the analyst engine wiring
  rather than deleting tested code whose only defect is a dead data source.

## Workstream 2: Alert state machine + daily digest

### New table

```sql
CREATE TABLE condition_states (
    netuid     INTEGER NOT NULL,
    condition  TEXT NOT NULL,           -- e.g. 'emission_near_zero', 'dead_github'
    entered_at TEXT NOT NULL,
    cleared_at TEXT,                    -- NULL = currently active
    last_value REAL,
    PRIMARY KEY (netuid, condition, entered_at)
);
```

Migration lives in `db/database.py` alongside existing `ADD COLUMN` migrations.

### Alert classes

- **Chronic conditions** — routed through the state machine; fire once on entering
  the bad state, once on recovery, silent in between:
  `emission_near_zero`, `emission_divergence`, `dead_github`, `emission_drop`,
  `liquidity_floor`, plus the new `collector_stale` (workstream 3).
- **Acute events** — unchanged immediate behavior with existing cooldowns
  (`_cooldown_hours_for_alert`): `important_buy`, `important_sell`, `whale_inflow`,
  `tao_outflow`, `ownership_transfer`, `new_entry`, `hyperparameter_change`,
  `emergence_watch`, flow-impulse alerts, `milestone`, `convergence`.

### Hysteresis (anti-flap)

A condition must hold for **2 consecutive polls** before `entered` fires, and clear
for **4 consecutive polls** before `recovered` fires. Both constants in `config.py`.

### Daily digest

New scheduled job (08:00 local, Telegram): one message summarizing active
conditions grouped by type — e.g. "emission_near_zero: 41 subnets (3 new,
1 recovered)" — plus collector-health status. Reads `condition_states` directly;
no scan of alert history.

### Compatibility

The `alerts` table is untouched. State transitions still insert alert rows (with
`entered` / `recovered` phrasing in the description), so alert history and the
dashboard alert feed keep working. Expected Telegram volume drops from ~570/week
to tens/week.

## Workstream 3: Collector health panel

- `/api/health` endpoint + dashboard panel, computed at request time from existing
  data (no new collection infra). Per collector:
  - **chain:** max `polled_at`; rows in last 24h; null-rate on
    `alpha_price_tao`, `buy_slippage_pct` over last 24h.
  - **github:** max `polled_at` where `gh_stars` IS NOT NULL; null-rate on
    `gh_last_push`.
  - **milestones:** `collector_state` last-check keys.
- Config thresholds: chain stale > 45 min, GitHub stale > 3 h, key-field null-rate
  > 30%.
- A breached threshold renders red on the dashboard **and** feeds a
  `collector_stale` chronic condition (sentinel netuid −1; netuid 0 is the live
  root network and appears in `snapshots`) through the workstream-2
  state machine — so pipeline death pings Telegram once and appears in every daily
  digest until fixed (the digest and dashboard render sentinel rows as
  "Collector health", not as a subnet). This is the structural fix for the failure class behind the
  dead X pipeline.

## Workstream 4: Data hygiene

- **Backups:** `backup_db_file` (`db/database.py`) currently runs on every start.
  Change: back up only when pending migrations will actually alter the schema
  (diff expected columns / `PRAGMA user_version` first). Keep the prune-to-5 logic.
- **Retention:** weekly job downsamples `snapshots` rows older than 90 days to
  1 row per subnet per hour (keeps backtestability, caps growth ~46 MB/16 days
  today). The Apr–May recovered archive (`monitor_archive_aprmay.db`) is untouched.

## Testing

- `tests/engine/test_condition_states.py`: enter / clear / flap / hysteresis /
  restart-persistence cases.
- Digest formatting golden tests.
- Health endpoint tested against a fixture DB containing a deliberately stale
  collector and a high null-rate field.
- X-cut verified by: scorer tests updated (no hype in weights), monitor boots with
  `X_PIPELINE_ENABLED=False`, analyst tab renders dormant state.

## Rollout

Each workstream is an independent PR, landed in order:

1. X cut
2. Condition state machine
3. Daily digest
4. Health panel (depends on 2 for `collector_stale`)
5. Hygiene

Nothing here blocks the standing P0: the swing-score calibration re-run when 30
days of `swing_score` history exist (~2026-07-15).

## Out of scope (deferred to TODOS.md)

- Registry backfill (github_url × 20, category × 22; x_handle moot after X cut).
- Slippage null-rate investigation (~21% of recent rows).
- Whale-inflow staker enumeration design (pre-existing TODO).
- Any new signal work (emergence validation, convergence tuning).
