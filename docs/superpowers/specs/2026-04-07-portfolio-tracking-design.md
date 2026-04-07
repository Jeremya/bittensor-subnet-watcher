# Portfolio Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track the user's staked subnet positions across multiple wallets, display them highlighted on the main dashboard, and provide a dedicated `/portfolio` page with current TAO/USD values and P&L since monitoring began.

**Architecture:** A new `PortfolioCollector` runs inside the existing 15-min poll cycle (after chain snapshots, so prices are already in memory). Positions are stored in a new `portfolio_positions` table. The P&L baseline (`baseline_tao_value`) is set on first detection and never updated. Two new web routes surface the data.

**Tech Stack:** Python/asyncio, aiosqlite, bittensor `AsyncSubtensor.get_stake_info_for_coldkey`, FastAPI + Jinja2, existing patterns from `collectors/chain.py` and `db/database.py`.

---

## Config

`WALLET_COLDKEYS` — comma-separated SS58 coldkey addresses in `.env`. Optional parallel `WALLET_LABELS` list for display names; falls back to "Wallet 1", "Wallet 2", etc. if absent or shorter than the coldkey list.

```
WALLET_COLDKEYS=5HK...,5Gx...,5Df...
WALLET_LABELS=Main,Trading,Cold
```

Parsed in `config.py` as:
```python
WALLET_COLDKEYS: list[str] = [k.strip() for k in os.getenv("WALLET_COLDKEYS", "").split(",") if k.strip()]
WALLET_LABELS: list[str] = [l.strip() for l in os.getenv("WALLET_LABELS", "").split(",") if l.strip()]
```

Label for index `i`: `WALLET_LABELS[i]` if it exists, else `f"Wallet {i+1}"`.

---

## Database

New table added to `SCHEMA_SQL` in `db/database.py`:

```sql
CREATE TABLE IF NOT EXISTS portfolio_positions (
    coldkey            TEXT NOT NULL,
    netuid             INTEGER NOT NULL,
    alpha_amount       REAL NOT NULL,        -- sum across all hotkeys for this subnet
    tao_value          REAL NOT NULL,        -- alpha_amount × current alpha_price_tao
    baseline_tao_value REAL NOT NULL,        -- tao_value when first seen (never updated)
    first_seen_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (coldkey, netuid)
);
```

**Upsert logic:** `INSERT INTO ... ON CONFLICT(coldkey, netuid) DO UPDATE SET alpha_amount=..., tao_value=..., updated_at=...` — `baseline_tao_value` and `first_seen_at` are excluded from the UPDATE clause so they are frozen on first insert.

Positions no longer present in the collector result (fully unstaked) are deleted after each poll.

**Upsert baseline rule:** `baseline_tao_value` is only frozen once `tao_value > 0`. If the first detection has missing price (`tao_value = 0`), the row is inserted with `baseline_tao_value = 0` as a sentinel, and the upsert updates `baseline_tao_value` on the next poll where `tao_value > 0`. Implemented via: `DO UPDATE SET baseline_tao_value = CASE WHEN excluded.tao_value > 0 AND portfolio_positions.baseline_tao_value = 0 THEN excluded.tao_value ELSE portfolio_positions.baseline_tao_value END, ...`

**New DB functions:**
- `upsert_portfolio_position(db, coldkey, netuid, alpha_amount, tao_value)` — insert or update, preserving baseline once set
- `delete_gone_positions(db, coldkey, current_netuids: set[int])` — delete rows for coldkey where netuid NOT IN current_netuids
- `get_portfolio_positions(db)` — returns all rows LEFT JOINed with `subnet_registry` for subnet name, ordered by coldkey, netuid
- `get_staked_netuids(db)` — returns `set[int]` of netuids with any active position (for dashboard badge)

---

## Collector

New file `collectors/portfolio.py`:

```python
class PortfolioCollector:
    @staticmethod
    async def collect(
        subtensor: bt.AsyncSubtensor,
        coldkeys: list[str],
        price_by_netuid: dict[int, float],   # alpha_price_tao from chain snapshots
    ) -> dict[str, dict[int, dict]]:
        """
        Returns {coldkey: {netuid: {"alpha_amount": float, "tao_value": float}}}
        Aggregates stakes across all hotkeys per (coldkey, netuid).
        Skips coldkeys that fail with a warning log.
        """
```

Called once per coldkey via `subtensor.get_stake_info_for_coldkey(coldkey)`. Each `StakeInfo` entry has a `netuid` and `stake` (alpha Balance). TAO value = `stake.tao * price_by_netuid.get(netuid, 0)`. If price is unavailable, `tao_value = 0` (shown as `—` in UI).

---

## Poll Cycle Integration (`main.py`)

After chain snapshots are collected and before GitHub carry-forward, add:

```python
if config.WALLET_COLDKEYS:
    price_by_netuid = {s.netuid: s.alpha_price_tao for s in chain_snapshots
                       if s.alpha_price_tao is not None}
    portfolio = await PortfolioCollector.collect(
        subtensor, config.WALLET_COLDKEYS, price_by_netuid
    )
    from collectors.chain import _subtensor as subtensor
    for coldkey, positions in portfolio.items():
        for netuid, data in positions.items():
            await upsert_portfolio_position(
                _db, coldkey, netuid, data["alpha_amount"], data["tao_value"]
            )
        await delete_gone_positions(_db, coldkey, set(positions.keys()))
```

---

## Web Routes (`web/routes.py`)

### Main dashboard (`/`)

Add `staked_netuids = await get_staked_netuids(db)` to the dashboard handler. Pass to template. Rows where `s["netuid"] in staked_netuids` get a `staked=True` flag in the enriched snapshot list.

Template: staked rows show a small green `●` badge in the subnet name column.

### Portfolio page (`/portfolio`)

New route `GET /portfolio` → `portfolio.html`.

Handler:
1. `rows = await get_portfolio_positions(db)` 
2. Build wallet label map from `config.WALLET_COLDKEYS` + `config.WALLET_LABELS`
3. Group rows by coldkey, compute per-wallet and grand totals
4. Compute P&L per position: `tao_pnl = tao_value - baseline_tao_value`; `pnl_pct = tao_pnl / baseline_tao_value * 100` — only when `baseline_tao_value > 0`, otherwise `None` (displayed as `—`)
5. Pull `tao_usd_price` from `rows[0]["tao_usd_price"]` if rows exist, else `None`
6. Subnet name from `get_portfolio_positions` JOIN — available as `row["name"]` (may be None for unregistered subnets; fall back to `f"SN{netuid}"`)
7. Pass to template: `wallets` (list of `{label, coldkey, positions, total_tao, total_usd, total_pnl_tao, total_pnl_pct}`), `grand_total_*`, `tao_usd_price`

Template `portfolio.html`: table per wallet, grand total row at bottom. P&L positive = green, negative = red. Subnet name links to `/subnet/{netuid}`. Positions with `baseline_tao_value = 0` show `—` for P&L (price was unavailable at first detection).

---

## Error Handling

- `get_stake_info_for_coldkey` failure: log `WARNING [PORTFOLIO] coldkey_failed coldkey={truncated} error={e}`, skip that wallet, continue with others
- Missing price for a netuid: `tao_value = 0.0`, displayed as `—` in UI, P&L shown as `—`
- `WALLET_COLDKEYS` empty: entire collector block is skipped, no DB writes, no new routes rendered (portfolio page returns empty state)
- Fully unstaked position (`alpha_amount = 0`): deleted from DB, disappears from portfolio page on next poll

---

## Testing

- `tests/collectors/test_portfolio.py`: mock `get_stake_info_for_coldkey` returning known StakeInfo list; verify aggregation across hotkeys, price multiplication, missing-price fallback
- `tests/db/test_portfolio_db.py`: verify upsert preserves `baseline_tao_value` on second call; verify baseline updates from 0→value when price becomes available; verify `delete_gone_positions` removes absent netuids; verify `get_staked_netuids` returns correct set
- `tests/web/test_portfolio_route.py`: verify `/portfolio` renders with empty positions (no crash); verify P&L calculation in handler
