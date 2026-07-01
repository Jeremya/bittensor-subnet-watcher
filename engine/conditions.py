"""Chronic-condition state machine.

Chronic alert conditions route through here instead of firing every poll:
a condition must be breached for CONDITION_ENTER_POLLS consecutive polls to
'enter' (one alert), and clear for CONDITION_CLEAR_POLLS consecutive polls to
'recover' (one alert). breached=None (data missing) freezes the state.

Rows live in condition_states: 'pending' (breaching, unconfirmed),
'active' (confirmed), 'cleared' (historical episode). netuid -1 is a sentinel
for collector-health conditions — netuid 0 is the live root network.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

import config

logger = logging.getLogger(__name__)


async def _live_row(db: aiosqlite.Connection, netuid: int,
                    condition: str) -> Optional[aiosqlite.Row]:
    cursor = await db.execute(
        """
        SELECT * FROM condition_states
        WHERE netuid=? AND condition=? AND status IN ('pending', 'active')
        """,
        (netuid, condition),
    )
    return await cursor.fetchone()


async def advance_condition(db: aiosqlite.Connection,
                            netuid: int,
                            condition: str,
                            breached: Optional[bool],
                            value: Optional[float] = None,
                            now: Optional[datetime] = None) -> Optional[str]:
    """Feed one observation into the state machine.

    Returns 'entered' or 'recovered' on a confirmed transition, else None.
    """
    if breached is None:
        return None   # missing data: freeze
    now = now or datetime.now(timezone.utc)
    now_s = now.isoformat()
    row = await _live_row(db, netuid, condition)

    if row is None:
        if breached:
            await db.execute(
                """
                INSERT INTO condition_states
                    (netuid, condition, status, first_breach_at,
                     breach_streak, clear_streak, last_value, updated_at)
                VALUES (?, ?, 'pending', ?, 1, 0, ?, ?)
                """,
                (netuid, condition, now_s, value, now_s),
            )
            await db.commit()
        return None

    key = (row["netuid"], row["condition"], row["first_breach_at"])

    if row["status"] == "pending":
        if not breached:
            await db.execute(
                "DELETE FROM condition_states WHERE netuid=? AND condition=? AND first_breach_at=?",
                key,
            )
            await db.commit()
            return None
        streak = row["breach_streak"] + 1
        if streak >= config.CONDITION_ENTER_POLLS:
            await db.execute(
                """
                UPDATE condition_states
                SET status='active', entered_at=?, breach_streak=?, last_value=?, updated_at=?
                WHERE netuid=? AND condition=? AND first_breach_at=?
                """,
                (now_s, streak, value, now_s, *key),
            )
            await db.commit()
            logger.info("[CONDITION] entered netuid=%d condition=%s", netuid, condition)
            return "entered"
        await db.execute(
            """
            UPDATE condition_states
            SET breach_streak=?, last_value=?, updated_at=?
            WHERE netuid=? AND condition=? AND first_breach_at=?
            """,
            (streak, value, now_s, *key),
        )
        await db.commit()
        return None

    # status == 'active'
    if breached:
        await db.execute(
            """
            UPDATE condition_states
            SET clear_streak=0, last_value=?, updated_at=?
            WHERE netuid=? AND condition=? AND first_breach_at=?
            """,
            (value, now_s, *key),
        )
        await db.commit()
        return None
    clear_streak = row["clear_streak"] + 1
    if clear_streak >= config.CONDITION_CLEAR_POLLS:
        await db.execute(
            """
            UPDATE condition_states
            SET status='cleared', cleared_at=?, clear_streak=?, updated_at=?
            WHERE netuid=? AND condition=? AND first_breach_at=?
            """,
            (now_s, clear_streak, now_s, *key),
        )
        await db.commit()
        logger.info("[CONDITION] recovered netuid=%d condition=%s", netuid, condition)
        return "recovered"
    await db.execute(
        """
        UPDATE condition_states
        SET clear_streak=?, updated_at=?
        WHERE netuid=? AND condition=? AND first_breach_at=?
        """,
        (clear_streak, now_s, *key),
    )
    await db.commit()
    return None


async def get_active_conditions(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM condition_states WHERE status='active' ORDER BY condition, netuid"
    )
    return await cursor.fetchall()


async def get_condition_transitions_since(db: aiosqlite.Connection,
                                          since_iso: str) -> list[aiosqlite.Row]:
    """Episodes that entered or recovered since `since_iso` (for the digest)."""
    cursor = await db.execute(
        """
        SELECT * FROM condition_states
        WHERE (entered_at IS NOT NULL AND entered_at > ?)
           OR (cleared_at IS NOT NULL AND cleared_at > ?)
        ORDER BY condition, netuid
        """,
        (since_iso, since_iso),
    )
    return await cursor.fetchall()
