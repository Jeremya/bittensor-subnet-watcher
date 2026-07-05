"""Owner locked-alpha sweep (daily).

Teams locking their alpha is a supply/conviction signal that cannot be
backfilled — collection starts early, consumers (lock-delta catalysts,
float-adjusted rotation) come once history accrues. `get_coldkey_lock`
returns {'locked_mass': Balance, 'conviction': float, 'last_update': block};
a None lock is a MEASURED zero (owner holds no lock), stored as 0.0, while a
query error stores nothing.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_CONCURRENCY = 8


def _locked_alpha_from(lock: Optional[dict]) -> float:
    if not lock:
        return 0.0
    mass = lock.get("locked_mass")
    if mass is None:
        return 0.0
    return float(getattr(mass, "tao", mass))


class LockCollector:
    @staticmethod
    async def collect(subtensor, db: aiosqlite.Connection) -> int:
        """Sweep owner locks for every subnet with a known owner. Returns rows written."""
        from db.database import insert_owner_lock

        cursor = await db.execute(
            """
            SELECT s.netuid, s.owner_coldkey, s.alpha_price_tao, s.alpha_mcap_tao
            FROM snapshots s
            INNER JOIN (
                SELECT netuid, MAX(polled_at) AS mt FROM snapshots GROUP BY netuid
            ) latest ON s.netuid = latest.netuid AND s.polled_at = latest.mt
            WHERE s.owner_coldkey IS NOT NULL
            """
        )
        targets = await cursor.fetchall()

        now = datetime.now(timezone.utc)
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        written = zero = errors = 0

        async def sweep_one(row) -> Optional[tuple]:
            async with semaphore:
                try:
                    lock = await subtensor.get_coldkey_lock(
                        row["owner_coldkey"], row["netuid"])
                except Exception as exc:
                    logger.debug("[COLLECTOR] locks: netuid=%s error=%s",
                                 row["netuid"], exc)
                    return None
            return (row, _locked_alpha_from(lock))

        results = await asyncio.gather(*(sweep_one(r) for r in targets))
        for result in results:
            if result is None:
                errors += 1
                continue
            row, locked_alpha = result
            price = row["alpha_price_tao"]
            mcap = row["alpha_mcap_tao"]
            locked_tao = locked_pct = None
            if price and price > 0:
                locked_tao = locked_alpha * price
                if mcap and mcap > 0:
                    supply = mcap / price
                    if supply > 0:
                        locked_pct = locked_alpha / supply
            await insert_owner_lock(
                db, row["netuid"], now,
                locked_alpha=round(locked_alpha, 6),
                locked_tao=round(locked_tao, 6) if locked_tao is not None else None,
                locked_pct=round(locked_pct, 6) if locked_pct is not None else None,
            )
            written += 1
            if locked_alpha == 0.0:
                zero += 1

        logger.info("[COLLECTOR] name=locks ok=%d zero=%d errors=%d",
                    written, zero, errors)
        return written
