# TODOS

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
