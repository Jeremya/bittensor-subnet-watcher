# db/database.py
import aiosqlite
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from models import SubnetSnapshot, AlertRecord
import config

logger = logging.getLogger(__name__)

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS snapshots (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid             INTEGER NOT NULL,
    polled_at          TEXT NOT NULL,
    alpha_price_tao    REAL,
    alpha_mcap_tao     REAL,
    alpha_mcap_usd     REAL,
    volume_24h_alpha   REAL,
    tao_usd_price      REAL,
    daily_emission_tao REAL,
    emission_rank      INTEGER,
    n_neurons          INTEGER,
    reg_cost_tao       REAL,
    owner_coldkey      TEXT,
    gh_last_push       TEXT,
    gh_stars           INTEGER,
    gh_forks           INTEGER,
    gh_open_issues     INTEGER,
    x_last_tweet       TEXT,
    x_followers        INTEGER,
    yield_score        REAL,
    quality_score      REAL,
    momentum_score     REAL,
    composite_score    REAL
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    fired_at      TEXT NOT NULL,
    netuid        INTEGER NOT NULL,
    subnet_name   TEXT NOT NULL,
    alert_type    TEXT NOT NULL,
    description   TEXT NOT NULL,
    current_value REAL,
    threshold     REAL,
    notified      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS subnet_registry (
    netuid     INTEGER PRIMARY KEY,
    name       TEXT,
    team       TEXT,
    website    TEXT,
    github_url TEXT,
    x_handle   TEXT,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_snapshots_netuid_time ON snapshots (netuid, polled_at);
CREATE INDEX IF NOT EXISTS idx_alerts_fired_at ON alerts (fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts (netuid, alert_type, fired_at);
"""


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    return datetime.fromisoformat(s)


async def init_db(db_path: str = config.DB_PATH) -> aiosqlite.Connection:
    """Create DB directory, initialize schema, return open connection."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_SQL)
    await conn.commit()
    return conn


async def insert_snapshot(db: aiosqlite.Connection, snap: SubnetSnapshot) -> None:
    await db.execute("""
        INSERT INTO snapshots (
            netuid, polled_at, alpha_price_tao, alpha_mcap_tao, alpha_mcap_usd,
            volume_24h_alpha, tao_usd_price, daily_emission_tao, emission_rank,
            n_neurons, reg_cost_tao, owner_coldkey,
            gh_last_push, gh_stars, gh_forks, gh_open_issues,
            x_last_tweet, x_followers,
            yield_score, quality_score, momentum_score, composite_score
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snap.netuid, _dt_to_str(snap.polled_at),
        snap.alpha_price_tao, snap.alpha_mcap_tao, snap.alpha_mcap_usd,
        snap.volume_24h_alpha, snap.tao_usd_price,
        snap.daily_emission_tao, snap.emission_rank,
        snap.n_neurons, snap.reg_cost_tao, snap.owner_coldkey,
        _dt_to_str(snap.gh_last_push), snap.gh_stars, snap.gh_forks, snap.gh_open_issues,
        _dt_to_str(snap.x_last_tweet), snap.x_followers,
        snap.yield_score, snap.quality_score, snap.momentum_score, snap.composite_score,
    ))
    await db.commit()


async def get_latest_snapshots(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Return the most recent snapshot for each netuid."""
    cursor = await db.execute("""
        SELECT s.* FROM snapshots s
        INNER JOIN (
            SELECT netuid, MAX(polled_at) AS max_ts
            FROM snapshots GROUP BY netuid
        ) latest ON s.netuid = latest.netuid AND s.polled_at = latest.max_ts
        ORDER BY s.composite_score DESC NULLS LAST
    """)
    return await cursor.fetchall()


async def get_snapshots_for_netuid(db: aiosqlite.Connection,
                                    netuid: int,
                                    limit: int = 100) -> list[aiosqlite.Row]:
    """Return recent snapshots for a single netuid (for momentum calc)."""
    cursor = await db.execute(
        "SELECT * FROM snapshots WHERE netuid=? ORDER BY polled_at DESC LIMIT ?",
        (netuid, limit)
    )
    return await cursor.fetchall()


async def get_latest_snapshots_with_registry(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Latest snapshot per netuid LEFT JOINed with subnet_registry. Ordered by composite_score DESC."""
    cursor = await db.execute("""
        SELECT s.*, r.name, r.github_url, r.x_handle, r.website
        FROM snapshots s
        INNER JOIN (
            SELECT netuid, MAX(polled_at) AS max_ts
            FROM snapshots GROUP BY netuid
        ) latest ON s.netuid = latest.netuid AND s.polled_at = latest.max_ts
        LEFT JOIN subnet_registry r ON s.netuid = r.netuid
        ORDER BY s.composite_score DESC NULLS LAST
    """)
    return await cursor.fetchall()


async def get_emission_rank_24h_ago(db: aiosqlite.Connection) -> dict[int, Optional[int]]:
    """Return {netuid: emission_rank} from the most recent snapshot ≥24h old per netuid."""
    # datetime() wrapping required: Python isoformat() produces 'T' separator + '+00:00'
    # which compares lexicographically greater than SQLite's 'YYYY-MM-DD HH:MM:SS' format.
    cursor = await db.execute("""
        SELECT netuid, emission_rank
        FROM snapshots s1
        WHERE polled_at = (
            SELECT MAX(polled_at) FROM snapshots s2
            WHERE s2.netuid = s1.netuid
            AND datetime(s2.polled_at) <= datetime('now', '-24 hours')
        )
    """)
    rows = await cursor.fetchall()
    return {row["netuid"]: row["emission_rank"] for row in rows}


async def get_subnet_detail(db: aiosqlite.Connection,
                             netuid: int) -> Optional[aiosqlite.Row]:
    """Latest snapshot for one netuid LEFT JOINed with subnet_registry."""
    cursor = await db.execute("""
        SELECT s.*, r.name, r.github_url, r.x_handle, r.website
        FROM snapshots s
        LEFT JOIN subnet_registry r ON s.netuid = r.netuid
        WHERE s.netuid = ?
        ORDER BY s.polled_at DESC LIMIT 1
    """, (netuid,))
    return await cursor.fetchone()


async def get_alerts_for_netuid(db: aiosqlite.Connection,
                                 netuid: int,
                                 limit: int = 10) -> list[aiosqlite.Row]:
    """Most recent alerts for a specific subnet."""
    cursor = await db.execute(
        "SELECT * FROM alerts WHERE netuid = ? ORDER BY fired_at DESC LIMIT ?",
        (netuid, limit)
    )
    return await cursor.fetchall()


async def insert_alert(db: aiosqlite.Connection, alert: AlertRecord) -> None:
    await db.execute("""
        INSERT INTO alerts (fired_at, netuid, subnet_name, alert_type,
                            description, current_value, threshold, notified)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        _dt_to_str(alert.fired_at), alert.netuid, alert.subnet_name,
        alert.alert_type, alert.description,
        alert.current_value, alert.threshold,
        1 if alert.notified else 0,
    ))
    await db.commit()


async def get_unsent_alerts(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM alerts WHERE notified=0 ORDER BY fired_at ASC"
    )
    return await cursor.fetchall()


async def mark_alerts_sent(db: aiosqlite.Connection, alert_ids: list[int]) -> None:
    if not alert_ids:
        return
    placeholders = ",".join("?" * len(alert_ids))
    await db.execute(
        f"UPDATE alerts SET notified=1 WHERE id IN ({placeholders})", alert_ids
    )
    await db.commit()


async def is_alert_in_cooldown(db: aiosqlite.Connection,
                                netuid: int,
                                alert_type: str,
                                cooldown_hours: int = config.ALERT_COOLDOWN_HOURS) -> bool:
    """Return True if an alert of this type was already fired for this subnet within cooldown_hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=cooldown_hours)).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM alerts WHERE netuid=? AND alert_type=? AND fired_at > ?",
        (netuid, alert_type, cutoff)
    )
    row = await cursor.fetchone()
    return row[0] > 0


async def get_last_50_alerts(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM alerts ORDER BY fired_at DESC LIMIT 50"
    )
    return await cursor.fetchall()


async def prune_old_snapshots(db: aiosqlite.Connection, days: int = 30) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    await db.execute("DELETE FROM snapshots WHERE polled_at < ?", (cutoff,))
    await db.commit()
    logger.info("Pruned snapshots older than %d days", days)


async def upsert_registry_entry(db: aiosqlite.Connection,
                                 netuid: int, name: str,
                                 github_url: Optional[str],
                                 x_handle: Optional[str],
                                 website: Optional[str] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("""
        INSERT INTO subnet_registry (netuid, name, github_url, x_handle, website, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(netuid) DO UPDATE SET
            name=excluded.name,
            github_url=excluded.github_url,
            x_handle=excluded.x_handle,
            website=excluded.website,
            updated_at=excluded.updated_at
    """, (netuid, name, github_url, x_handle, website, now))
    await db.commit()


async def get_registry(db: aiosqlite.Connection) -> dict[int, aiosqlite.Row]:
    """Return {netuid: registry_row}."""
    cursor = await db.execute("SELECT * FROM subnet_registry")
    rows = await cursor.fetchall()
    return {row["netuid"]: row for row in rows}
