# Pump Radar Phase 2 — market regime (tide) + relative-strength rotation

**Date:** 2026-07-04
**Status:** Approved (design A: magnitude + breadth; Telegram on confirmed flip)

## Problem

8 of the recorded June pumps started inside one 2-day market-wide inflow event
(Jun 17–18). The single biggest pump driver we've measured is the tide, and the
monitor has no concept of it. Phase 1's harness also showed recorded pumps are
mostly slow grinds — regime + rotation context, not per-subnet impulses, is
what catches those.

## Components

### `engine/regime.py` (pure functions, `conditions.py` style)

- `compute_tide(db, now=None) -> Optional[TideReading]`
  - `TideReading(tide_pct, breadth_pct, flows_24h_tao, pool_tao)`
  - tide_pct = Σ(per-subnet 24h net flow) ÷ Σ(latest tao_in per subnet)
  - breadth_pct = share of subnets **with flow data** whose own 24h net flow > 0
  - NULL-honest: no flow rows in 24h → `None` (never a fake neutral)
- `classify_regime(reading) -> 'risk_on' | 'neutral' | 'risk_off'`
  - risk_on: tide ≥ `REGIME_RISK_ON_TIDE_PCT` (+0.3%) AND breadth ≥ `REGIME_RISK_ON_BREADTH` (55%)
  - risk_off: tide ≤ `REGIME_RISK_OFF_TIDE_PCT` (−0.3%) OR breadth ≤ `REGIME_RISK_OFF_BREADTH` (35%)
  - else neutral. Fixed config thresholds (explicit over clever); tide/breadth
    persist so the harness can tune them later.
- `apply_rel_strength(snapshots, history_by_netuid) -> None`
  - per-subnet 24h price return (current vs nearest snapshot ≤ 24h ago, within
    a 4h tolerance) minus market median return → **0–100 percentile rank**,
    written to new persisted snapshot column `rel_strength_score` (None when no
    24h-ago price — backtestable from day 1, added to harness SIGNAL_COLUMNS)
- `evaluate_regime(db, registry) -> list[AlertRecord]`
  - computes tide, appends one row to new `market_state` table
    (polled_at PK, tide_pct, breadth_pct, flows_24h_tao, regime)
  - routes `market_risk_on` and `market_risk_off` conditions (sentinel netuid
    −1) through the existing state machine (2-poll enter / 4-poll clear)
  - confirmed transitions fire a `regime_flip` alert (🌊, immediate Telegram):
    risk-on entry includes the top-5 `rel_strength_score` subnets ("the tide is
    in — these are leading"); recovery and risk-off get plain flip messages

### Wiring

`poll_cycle`: `apply_rel_strength` after `score_snapshots` (before persistence);
`evaluate_regime` after `evaluate_ignition`. Dashboard: regime banner on index
(latest `market_state` row) + RS column in the leaderboard table. Digest: tide
line upgrades to `🌊 Tide: +4,210 τ in · breadth 62% · RISK-ON`.

### Explicitly out of scope

No effect on buy recommendations (calibration gate stands); no TAO/USD input to
regime v1; no RS-based alerts beyond the flip message; no adaptive thresholds.

## Failure modes

| Case | Handling |
|---|---|
| No flow data in 24h (fresh DB / outage) | tide `None` → no market_state row, conditions frozen (breached=None), banner shows "no data" |
| Single-subnet whale spike | breadth requirement keeps regime neutral |
| Flapping around thresholds | state-machine hysteresis (2 enter / 4 clear) |
| RS with <24h history | `rel_strength_score` None; percentile computed only over subnets with data |
| isoformat vs SQLite time traps | all comparisons `datetime()`-wrapped |

## Testing

Pure units: tide math, breadth, NULL paths, percentile ties, classify
boundaries. evaluate_regime: flip fires once (hysteresis), top-RS names in
risk-on message, market_state rows accumulate. Replay check (manual, final
verification): Jun 17–18 window on a live-DB copy must classify risk_on.
Dashboard/digest goldens.
