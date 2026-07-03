# TODOS

## P1 — Pump Radar (CEO review 2026-07-03, EXPANSION mode)

**Evidence that reframed the roadmap:** 15 pump events (≥1.5x in 3d, up to 6.3x)
recorded Jun 15–Jul 3. ZERO had emergence_score ≥ 70 at pump start; 2/15 had
swing ≥ 75; catalyst NULL on all 15; prior-24h flows ±3% of pool, volume flat.
On-chain levels do NOT lead pumps. 8/15 started Jun 17–18 (market-wide tide).
Strategy: detect ignition in minutes + know the tide + see catalysts — don't predict.

### ✅ Phase 1 — DONE (2026-07-03): Pump-event registry + ignition detector
Shipped: `engine/pump_events.py` + `pump_events` table + hourly scan + backfill
script; `scripts/signal_leadlag.py` (first run: swing 14% hit-rate, emergence/
flow 0%, catalyst all-NULL — CEO finding now a repeatable measurement);
`engine/ignition.py` + `pump_ignition` acute alert (tuned 6%/2% ≈ 1 false/day;
outage gate; cluster collapse); emergence fake-0 → NULL; 🌊 tide + 🔥 scorecard
digest lines; `/pumps` page; subnet pump-record block.
**Honesty note:** time-based windows found 7 real events (max 2.73x/2.6d) — the
CEO review's "15 pumps incl. 6.33x in 3d" was partly an index-vs-time artifact
(SN16's 6.4x unfolded over weeks). Slow-grind pumps are Phase 2's target.
**Post-merge:** restart monitor, then run
`.venv/bin/python -m scripts.backfill_pump_events` against the live DB.

### Phase 1 original scope (for reference)
- `engine/pump_events.py` — pure detection over snapshot series → `pump_events`
  table (start/peak/retrace); hourly job; idempotent; owner-change resets;
  skip events whose pre-window spans a data gap; min-mcap floor.
- `scripts/signal_leadlag.py` — samples every signal at T-24h…T0 per event;
  hit-rate + lead-time per signal; grades ignition alerts hit/late/false.
- `engine/ignition.py` — price impulse + volume expansion + flow surge in 1–2
  polls → `pump_ignition` ACUTE alert. **Must gate on prev-poll age ≤ 2× poll
  interval** (first poll after outage = fake impulse — same bug class as the
  backtest horizon fix). Tune thresholds on the 15 recorded events.
- Scope also includes (user-approved): neutral-50 → NULL score audit (no fake
  50s feeding the harness), 🌊 tide line in daily digest, runway-enriched
  ignition alerts (slippage at size + median pump peak/duration from registry),
  `/pumps` dashboard page, pump-record block on subnet detail pages.

### Phase 2 (queued): Regime & rotation — P2
Aggregate net-TAO-flow tide dial + per-subnet relative-strength ranking when
risk turns on. 8/15 pumps were one market event. Existing data; one engine
module + dashboard banner. Judged by the lead/lag harness from day one. Effort: M.

### Phase 3 (queued): Catalyst feeds — P2
Automated collectors from durable sources feeding the existing catalyst/
convergence pipeline: GitHub releases/tags first (API already integrated),
then project blog/site RSS, taostats/exchange listing announcements. The pumps
were news-driven; our catalyst inputs are near-empty. Effort: M–L.

### Phase 4 (queued): Whale fingerprints — P3
First step is a 1-hour spike: test per-subnet staker enumeration on SDK 10.5
(supersedes the old blocked whale-inflow design below). If feasible: staker-
delta collection → wallet→pump-lead history from the registry → alert when a
repeat pre-pump wallet enters. Only signal class that can LEAD news-driven
pumps. Effort: L–XL, hypothesis until the harness grades it.

### Small (queued): Locked-alpha collection — P2 (idea via @TAOTemplar, 2026-07-03)
Daily sweep: `get_coldkey_lock(owner_coldkey, netuid)` for all 129 subnets (SDK
10.5 verified; DynamicInfo does NOT carry it) → persist owner-locked alpha
(τ value + % of supply + delta). Three uses, all harness-testable once data
accrues: (1) float compression as pump-MAGNITUDE conditioner for Phase 2
rotation ranking (MVTRX/Swarm ~50% locked = same inflow, ~2x the move);
(2) team-conviction health signal; (3) lock-delta / approaching-unlock-cliff
as catalyst/risk EVENTS (LockState rolls forward → expiry visible). Cannot be
backfilled — start persisting early. Full all-staker aggregate joins the
Phase 4 whale enumeration spike. Also: consider effective-float-adjusted
tradability (mcap currently counts locked alpha as float).

### Small (queued): Telegram deep links
`DASHBOARD_PUBLIC_URL` config + per-alert link to /subnet/{netuid}. Queue with
the decision on remote dashboard access (Tailscale?) — localhost-only until then.

## ✅ Trust Phase — DONE (2026-07-01)
Spec: `docs/superpowers/specs/2026-07-01-trust-phase-design.md`.
X scraping removed (never produced data) → manual tweet curation on subnet pages +
`/analysts`. Chronic alerts (emission_near_zero, emission_divergence, dead_github,
emission_drop, liquidity_floor) route through `condition_states` state machine —
fire once on entry, once on recovery. Daily 08:00 Telegram digest. Collector health
panel (`/api/health`) + `collector_stale` alerts. Backups only on schema change;
30-day snapshot hard delete replaced by 90-day downsample (1 row/subnet/hr).

### Deferred from trust phase
- **Registry backfill** — 20 subnets missing `github_url`, 22 missing `category`
  (limits dead_github coverage + category views). Effort: S, mostly data entry.
- ✅ **Slippage null investigation — DONE (2026-07-02).** Root cause: under the
  pre-Spec-421 SDK bulk path, slippage computation failed deterministically for a
  fixed set of 47 subnets (100% null Jun 15–25, ~30% of rows) while price/volume/
  reserves were fine. The chain's runtime upgrade (~Jun 26) removed
  `Swap.AlphaSqrtPrice`, causing a 5-day full outage (Jun 26–30, no snapshots);
  the Jul 1 fallback fix (74f0147) also healed slippage — 0.1% nulls since, all
  47 subnets clean. Scores degraded gracefully in the bad window (tradability
  avg 94.2 vs 99.6; swing_score always present). No further code fix needed.

### New follow-ups (from 2026-07-02 investigation)
- ✅ **SDK upgrade — DONE (2026-07-03).** bittensor 10.2.0 → 10.5.0. Verified in a
  scratch venv first, then live: `ChainCollector.collect()` returns 129/129 prices
  and slippage via the bulk path, no `chain_bulk_price_storage_missing` fallback.
  Upgrade note: uninstall `scalecodec`+`cyscale`, force-reinstall `cyscale`
  (namespace conflict). Fallback code kept as insurance for the next runtime
  upgrade — it is dormant now. **Monitor restart required to load 10.5.0.**
- **Calibration window caveat** — the Jun 26–30 outage means a contiguous clean
  30-day swing history lands ~Jul 31, not Jul 15. A Jul 15 run can still include
  Jun 15–25 (swing_score fully populated) with the 47-subnet tradability caveat.

## P0 — Calibration Gate (active)

### ✅ Backtest runner + first calibration run — DONE (2026-05-29)
**What:** `scripts/backtest_signals.py` runs `engine/backtest.py` over the live DB, reports
score-field coverage (swing vs composite), prints a bucket table, and can write JSON.
First run committed to memory + `data/backtest_composite_2026-05.json`.

**First result (legacy `composite_score`, 30 days — `swing_score` has NO history yet):**
score predicts in the 60–80 band (~60% 7d/14d win-rate) but **inverts above 80** (14d median
−11.3%, 3% win-rate, n=31). Use median + win-rate, not mean (means inflated by lottery pumps).

### ✅ Gate the UI on calibration state — DONE (2026-05-29)
Buy-side recs (`add`/`new_buy`) now carry `confidence="low"` + "swing model not yet validated"
while `config.SWING_SIGNAL_VALIDATED=False`, and an "extended / mean-reverts" caution at/above
`config.SWING_EXTENDED_SCORE`. Risk-driven `sell`/`trim` unchanged. Decision logic NOT tuned.

### ⏭️ NEXT: get swing_score history, then re-run + flip the gate
**What:** Restart the monitor so the new signal columns populate. After ~30 days of real
`swing_score` data, re-run `scripts/backtest_signals.py` (it auto-detects swing coverage).
Only then consider setting `SWING_SIGNAL_VALIDATED=True` and/or tuning thresholds — and only
if buckets separate. Do NOT tune off the legacy-composite single-window result.
**Effort:** S to re-run · **Priority:** P0 · **Depends on:** monitor uptime + 30d history

---

### ✅ Spec 421 scoring refactor — DONE (late June, entry never checked off)
Shipped across the spec-421 commit chain ending c56cbe3. `engine/spec421.py`
computes price EMA + price-based emission value + protocol context;
`compute_swing_signal` weights spec421 at 0.40 (flow demoted to 0.20 demand
signal, tradability 0.25, catalyst 0.15). All four spec421 columns persisted
and 100% populated since 2026-06-15 — validated by the 2026-07-03 backtest
dry run.

---

### ✅ Persist explicit signal columns — DONE (entry never checked off)
All six columns (`flow_score`, `relative_value_score`, `tradability_score`,
`catalyst_score`, `risk_penalty`, `swing_score`) exist in `SCHEMA_SQL` + the
migration list, are written in `engine/scorer.py` after `compute_swing_signal()`,
and are populated on 100% of rows since 2026-06-15 (verified 2026-07-03).
Note: `catalyst_score` NULL is intentional — `compute_catalyst_score` returns
None when no catalyst is active. Backtest runs off stored `swing_score`;
routes read pre-computed fields via `build_signal_from_snapshot`.

---

### Slippage-based tradability
**What:** Capture `alpha_in_tao` and `alpha_out_tao` pool reserve fields in
`collectors/chain.py`. Add a reserve-based slippage estimate to `compute_tradability_score()`
in `engine/signals.py`: for a configurable trade size, estimate price impact via the
constant-product AMM formula (`trade_size / (alpha_in_tao + trade_size)`). If estimated
slippage exceeds a threshold, lower the tradability score even when 24h volume looks fine.

**Why:** A subnet with 10 TAO daily volume looks "tradable" by turnover ratio, but exiting
a 50-TAO position would move the price 10%. Volume turnover is a bad proxy for large-position
exit cost. SN96-class situations (high emission rank, near-zero reserves) would be caught.

**Depends on:** Nothing — fully independent of other deferred work.

**Where to start:** `collectors/chain.py` (add reserve fields to `SubnetSnapshot`),
`engine/signals.py` `compute_tradability_score()`, then `tests/engine/test_signals.py`.

**Effort:** S–M (3–4 hours)
**Priority:** P1

---

## P1 — Follow-up (next PR)

### ✅ Ownership transfer alert (alert #7) — DONE
Implemented in `engine/alerts.py` as `check_ownership_transfer`. Fires when `owner_coldkey`
changes between consecutive snapshots. Guarded against None values on both sides.

---

### Whale inflow alert (alert #8) — PENDING design confirmation
**What:** Detect when a single wallet stakes >5% of alpha supply in one poll.

**Blocked on:** Confirming the correct SDK call to enumerate all stakers for a subnet.
`bt.AsyncSubtensor.get_stake_info_for_coldkey(coldkey)` pulls by coldkey, not by subnet —
there is no apparent `get_all_stakers_for_subnet(netuid)` in the public API.

**Design options to validate before implementing:**
1. Raw substrate: `substrate.query_map("SubtensorModule", "Stake", [netuid])` — undocumented
2. Re-scope: only fire for the two P2 tracked wallets (simpler, misses unknown whales)
3. Defer entirely until P2 portfolio integration provides the tracked-wallet context

**Effort:** M (4 hours once design is confirmed)
**Priority:** P1
**Depends on:** Design confirmation via live chain query experiment

---

---

## P1 — Analyst/Milestone Feature Follow-ups

### Handle cleanup on config handle removal
**What:** When a handle is removed from the `ANALYST_HANDLES` env var, its old
`analyst_mentions` rows remain in the DB and are still counted toward coverage badges.
The handle silently disappears from the `/analysts` UI without explicit removal.

**Why:** Can confuse the coverage badge display and the analyst feed on subnet detail pages
if the removed handle's historical mentions are still visible and counted as "active" within
the 72h decay window.

**Current state:** Config handles are never written to `analyst_watchlist` — only
dashboard-added handles are. So removing from env removes from the collection loop
immediately. But historical `analyst_mentions` rows remain indefinitely.

**Where to start:** `AnalystCollector._all_handles()` in `collectors/analyst.py`. On each
run, compare the union of config + DB handles against previously stored handle set. Consider
adding a `last_seen_at` or `active` flag to `analyst_watchlist` to tombstone removed handles.

**Effort:** S (2 hours)
**Priority:** P1 — low urgency, no correctness impact
**Depends on:** nothing

---

## P2 — Phase 2 Vision

### Portfolio wallet integration
**What:** Add coldkey wallet tracking — link your two wallets, overlay current holdings on the leaderboard, show P&L per subnet position.

**Why:** Closes the loop between signals and your actual portfolio. You'd see your positions ranked by composite score, instantly surfacing which holdings are deteriorating.

**Pros:** Directly actionable — turns the monitor into a portfolio management tool. The `bt.AsyncSubtensor.get_stake_info_for_coldkey()` method already works (validated in the due diligence session).

**Cons:** Additional on-chain queries per poll cycle. Adds `wallets` and `holdings` tables to the schema.

**Context:** Wallets: `5F6apr8Krey5S3A8sPf8UVQAiXgrahW5zjHFmnSQyiJxBRn5` and `5E1uZ8JXzb6GprqafNFs7VFVB8W5FF63g4JX7GKipw7q2w52`. The SSL certifi fix and `get_stake_info_for_coldkey()` call pattern are already documented. Architecture fully supports this as a new collector + new tables, no existing code changes needed.

**Effort:** M (4–6 hours)  
**Priority:** P2  
**Depends on:** v1 shipping and running stably for at least a week
