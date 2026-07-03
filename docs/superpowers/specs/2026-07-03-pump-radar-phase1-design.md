# Pump Radar Phase 1 — pump-event registry, lead/lag harness, ignition detector

**Date:** 2026-07-03
**Status:** Approved (decisions taken during the 2026-07-03 CEO review, EXPANSION mode;
roadmap option A + five scope votes recorded in TODOS.md "Pump Radar" section)

## Problem & evidence

15 pump events (≥1.5x within 3 days, max 6.3x) occurred Jun 15–Jul 3. Zero were
flagged in advance by any existing signal (emergence < 70 on all; swing ≥ 75 on
2/15; catalyst NULL on all; prior-24h flows ±3% of pool, volume flat). On-chain
*levels* do not lead pumps. Strategy: stop predicting — **record pumps, grade
every signal against them, and alert within minutes of ignition.**

## Components

### 1. Pump-event registry

New table:

```sql
CREATE TABLE IF NOT EXISTS pump_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid        INTEGER NOT NULL,
    start_at      TEXT NOT NULL,     -- last local price min before threshold crossing
    peak_at       TEXT,
    start_price   REAL NOT NULL,
    peak_price    REAL,
    ratio         REAL,              -- peak/start
    retrace_pct   REAL,              -- (peak-end)/(peak-start) once event closes
    status        TEXT NOT NULL,     -- 'active' | 'closed'
    start_mcap_usd REAL,
    detected_at   TEXT NOT NULL,
    UNIQUE (netuid, start_at)
);
```

`engine/pump_events.py` — pure detection over a price series:
- Event trigger: price reaches ≥ `PUMP_MIN_RATIO` (1.5) × a trailing local
  minimum within `PUMP_WINDOW_HOURS` (72).
- `start_at` = the local-minimum snapshot; `peak_at` tracks the running max;
  event closes when price retraces ≥ `PUMP_CLOSE_RETRACE` (50%) of the gain or
  `PUMP_WINDOW_HOURS` pass after the peak.
- Guards: min mcap `PUMP_MIN_MCAP_USD` ($250k) at start; series resets at
  `owner_coldkey` change (recycled netuids); skip series segments spanning a
  data gap > `PUMP_MAX_GAP_HOURS` (6) — no event may straddle an outage.
- Hourly scheduler job scans the last 7 days per subnet; idempotent upsert on
  `(netuid, start_at)`; also re-closes still-`active` events.
- One-time backfill run over the full live DB and (best effort, old schema) the
  Apr–May archive via `scripts/backfill_pump_events.py`.

### 2. Signal lead/lag harness

`scripts/signal_leadlag.py`:
- For each **closed** pump event, sample every persisted signal column
  (swing, spec421, flow, emergence, reg_demand, slot_fill, flow_accel,
  catalyst, tradability) at offsets T-24h, T-12h, T-6h, T-1h, T0 relative to
  `start_at` (nearest snapshot ≤ offset, within 2h tolerance; else "no data").
- Per signal: hit-rate (value ≥ per-signal threshold, default 70, before T0),
  median value at each offset, and n. NULL samples are counted separately —
  never treated as 0 or 50.
- Grades `pump_ignition` alerts: **hit** = alert within [start, start+6h];
  **late** = (start+6h, peak]; **false** = no event within 72h of the alert.
- Output: console table + JSON (`--output`), same pattern as
  `scripts/backtest_signals.py`.

### 3. Ignition detector

`engine/ignition.py` — pure function `detect_ignition(snap, history) ->
Optional[IgnitionSignal]` combining, over the last 1–2 polls:
- price impulse ≥ `IGNITION_PRICE_IMPULSE_PCT`
- volume expansion: volume_24h ≥ `IGNITION_VOLUME_EXPANSION` × volume 24h ago
- flow surge: net inflow ≥ `IGNITION_FLOW_PCT` of pool
- eligibility: mcap ≥ `PUMP_MIN_MCAP_USD`; **prev-poll age ≤ 2× POLL_INTERVAL
  (hard gate — first poll after an outage must never read as an impulse)**.

Fires a new **acute** alert type `pump_ignition` (immediate Telegram, per-netuid
cooldown `IGNITION_COOLDOWN_HOURS` = 6), evaluated in `poll_cycle` via a new
`evaluate_ignition(db, snapshots, history_by_netuid, registry)` — poll_cycle
already holds `history_by_netuid`. Watch-only: does NOT feed buy
recommendations until the harness validates it.

Initial thresholds are set by `scripts/tune_ignition.py`: replay the detector
over the recorded events, report (events caught within N polls of start,
false fires/day) per threshold grid, pick defaults; store chosen values in
`config.py` with the tuning date in a comment.

Alert copy includes entry context (delight votes): buy slippage at reference
size, and — when the registry has ≥ 5 closed events — "median recorded pump
peaked +X% above this point over ~Y days".

### 4. Neutral-50 → NULL audit (bundled)

Audit every score component that emits ~50 when inputs are missing (emergence
components default to 50; momentum baseline 50; flow neutral). Where the value
means "no data" (not "genuinely average"), return/persist None. The existing
`_weighted_score` already skips None components (catalyst proves the pattern).
Per-component note required in the plan: does the change alter `swing_score`
values going forward? If a component change would materially shift swing_score
mid-calibration-window, flag it in the commit message — harness honesty wins,
but the Jul 31 calibration must know the semantics changed and on which date.

### 5. Dashboard & digest (bundled delights)

- `/pumps` page: closed + active events, sortable (subnet, start, ratio,
  duration, retrace), with per-event "signals that led" column from the harness
  sampling (computed at request time is fine at this volume).
- Subnet detail page: "Pump record" block (n events, ratios, dates, retraces).
- Daily digest: 🌊 tide line — signed 24h aggregate net TAO flow across all
  subnets ("+4,210 τ in" / "−2,113 τ out").

## Failure modes (named in review)

| Codepath | Failure | Handling |
|---|---|---|
| ignition | post-outage fake impulse | prev-age gate (above) — with a unit test |
| ignition | zero/near-zero pool or price | guard → None (never 50) |
| ignition | market-wide cluster spam | per-netuid cooldown; multi-ignition polls collapse into one Telegram message listing all igniting subnets |
| pump_events | overlapping/re-trigger | idempotent upsert + skip-forward past peak |
| pump_events | netuid recycling | reset series at owner_coldkey change |
| leadlag | pre-window spans gap | event counted "unmeasurable", excluded from rates, reported |
| leadlag | isoformat vs SQLite datetime | all comparisons via `datetime()` wrapping (documented trap) |

## Testing

Pure-function unit tests (synthetic series): trigger/no-trigger boundaries,
gap handling, owner-change reset, spike-and-revert must NOT fire ignition,
slow grind must NOT fire. Golden fixture extracted from the live DB: SN16
Jun 17 (6.33x) must be detected with start/peak/ratio asserted; replay of
Jun 17–18 must catch the cluster while the quiet Jun 21–24 stretch stays
below the false-positive cap. Digest/alert copy golden-tested. Harness tested
on a fixture DB with two synthetic events.

## Observability

Ignition alerts log their feature values. The daily digest gains an ignition
scorecard line once grading data exists ("ignition 30d: 2 fired, 1 hit,
1 false"). `/pumps` page is the registry's face. Detection job logs
events-found/closed per run.

## Out of scope (queued in TODOS.md)

Regime/rotation module (P2), catalyst feed collectors (P2), whale fingerprints
(P3), locked-alpha collection (P2), Telegram deep links, any swing-weight
retuning (waits for Jul 31 calibration).
