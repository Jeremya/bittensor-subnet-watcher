# Owner locked-alpha collection (v1)

**Date:** 2026-07-05
**Status:** Approved

## Problem

Locked alpha (teams locking their emissions) is a supply/conviction signal we
don't collect, and it cannot be backfilled — every week of delay is lost
history for its future consumers (float-adjusted rotation, lock-delta
catalysts, harness validation). Spike verified: SDK 10.5
`get_coldkey_lock(owner, netuid)` returns
`{'locked_mass': Balance(alpha), 'conviction': float, 'last_update': block}`;
SN51's owner lock (339,736 α ≈ 17.9k τ) matches taoflute's published number.
No expiry field exists (conviction model) — unlock-cliff alerts are off the
table; deltas remain the event signal (deferred).

## Design

New table:

```sql
CREATE TABLE IF NOT EXISTS owner_locks (
    netuid       INTEGER NOT NULL,
    checked_at   TEXT NOT NULL,
    locked_alpha REAL NOT NULL,      -- 0.0 = measured "owner holds no lock"
    locked_tao   REAL,               -- locked_alpha * alpha_price_tao at sweep
    locked_pct   REAL,               -- locked_alpha / (alpha_mcap_tao/alpha_price_tao)
    PRIMARY KEY (netuid, checked_at)
);
```

`collectors/locks.py` — `LockCollector.collect(subtensor, db) -> int`:
- Reads latest snapshot per netuid (owner_coldkey, alpha_price_tao,
  alpha_mcap_tao); skips subnets missing an owner.
- `get_coldkey_lock(owner, netuid)` under an asyncio.Semaphore(8).
- `None` lock → locked_alpha 0.0 (measured zero, zero-vs-missing doctrine);
  per-subnet exception → skip (no row), count errors, one summary log line
  `[COLLECTOR] name=locks ok=N zero=M errors=K`.
- locked_tao / locked_pct computed when price/supply are valid, else NULL.

`main.py`: daily `lock_sweep` job (interval 24h) using the chain singleton.

DB helpers: `insert_owner_lock`, `get_owner_locks_for_netuid(db, netuid,
limit)` (newest first — subnet page shows latest + delta vs previous sweep).

Subnet detail page: "Owner lock" block — `339,736 α (~17,942 τ · 20.3% of
supply)` plus `Δ +X α since last sweep` when a previous row exists; "none"
when locked_alpha is 0; absent when never measured.

## Deferred (TODOS)

Lock-delta catalyst alerts, rotation/float conditioning, harness integration,
all-staker aggregate (rides the Phase 4 whale spike).

## Testing

Collector with mocked subtensor: lock present / None→0 / exception→skip;
Balance→float conversion; pct/tao math incl. missing-price NULLs. DB helper
round-trip. Route test: block renders with lock, "none" at zero.
