# TODOS

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

### Persist explicit signal columns
**What:** Add to `snapshots` table: `flow_score`, `relative_value_score`,
`tradability_score`, `catalyst_score`, `risk_penalty`, `swing_score`. Write them in
`engine/scorer.py` after `compute_swing_signal()`. Add migration path in `db/database.py`.

**Why:** Enables backtesting over historical data, richer `/api/snapshots` responses, and
lets the portfolio route read pre-computed signal fields from DB instead of recomputing them
at request time (which currently requires loading history per subnet).

**Depends on:** Tasks 1+4 (wire context + unify policy) merged first so signal values are
stabilized before you start logging them.

**Where to start:** `db/database.py` `SCHEMA_SQL`, migration `ADD COLUMN` block, then
`engine/scorer.py` after `swing = compute_swing_signal(...)`.

**Effort:** S (2 hours)
**Priority:** P1

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
