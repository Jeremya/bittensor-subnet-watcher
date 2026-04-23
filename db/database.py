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
    tao_in_tao         REAL,
    volume_24h_alpha   REAL,
    tao_usd_price      REAL,
    daily_emission_tao REAL,
    emission_rank      INTEGER,
    net_tao_flow_tao   REAL,
    n_neurons          INTEGER,
    max_allowed_uids   INTEGER,
    reg_cost_tao       REAL,
    owner_coldkey      TEXT,
    gh_last_push       TEXT,
    gh_stars           INTEGER,
    gh_forks           INTEGER,
    gh_open_issues     INTEGER,
    x_last_tweet       TEXT,
    x_followers        INTEGER,
    yield_score        REAL,
    health_score       REAL,
    momentum_score     REAL,
    hype_score         REAL,
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
    category   TEXT,
    category_confirmed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    coldkey            TEXT NOT NULL,
    netuid             INTEGER NOT NULL,
    alpha_amount       REAL NOT NULL,
    tao_value          REAL NOT NULL,
    baseline_tao_value REAL NOT NULL,
    first_seen_at      TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    PRIMARY KEY (coldkey, netuid)
);

CREATE TABLE IF NOT EXISTS analyst_watchlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    handle    TEXT NOT NULL UNIQUE,
    added_at  TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'dashboard'
);

CREATE TABLE IF NOT EXISTS analyst_mentions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    analyst_handle TEXT NOT NULL,
    netuid         INTEGER NOT NULL,
    tweet_url      TEXT NOT NULL,
    tweet_text     TEXT,
    mentioned_at   TEXT NOT NULL,
    notified       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (analyst_handle, netuid, tweet_url)
);

CREATE TABLE IF NOT EXISTS subnet_milestones (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    netuid         INTEGER NOT NULL,
    milestone_type TEXT NOT NULL,
    title          TEXT NOT NULL,
    url            TEXT NOT NULL,
    published_at   TEXT NOT NULL,
    ai_summary     TEXT,
    ai_take        TEXT,
    notified       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (netuid, url)
);

CREATE TABLE IF NOT EXISTS collector_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_netuid_time ON snapshots (netuid, polled_at);
CREATE INDEX IF NOT EXISTS idx_alerts_fired_at ON alerts (fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts (netuid, alert_type, fired_at);
CREATE INDEX IF NOT EXISTS idx_analyst_mentions_netuid ON analyst_mentions (netuid, mentioned_at);
CREATE INDEX IF NOT EXISTS idx_milestones_netuid ON subnet_milestones (netuid, published_at);
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
    # Migrate existing DBs: add columns introduced after initial schema
    cursor = await conn.execute("PRAGMA table_info(snapshots)")
    existing_cols = {row[1] for row in await cursor.fetchall()}
    for col, definition in [
        ("hype_score", "REAL"),
        ("net_tao_flow_tao", "REAL"),
        ("max_allowed_uids", "INTEGER"),
        ("tao_in_tao", "REAL"),
    ]:
        if col not in existing_cols:
            await conn.execute(f"ALTER TABLE snapshots ADD COLUMN {col} {definition}")
    # health_score migration — three cases:
    #   (a) quality_score exists, health_score absent → rename (preserves data)
    #   (b) neither exists (very old DB) → add as new column
    #   (c) health_score already present → nothing to do (covers both clean installs
    #       and DBs where the ADD COLUMN already ran before the rename was attempted)
    has_quality = "quality_score" in existing_cols
    has_health = "health_score" in existing_cols
    if has_quality and not has_health:
        await conn.execute(
            "ALTER TABLE snapshots RENAME COLUMN quality_score TO health_score"
        )
    elif not has_quality and not has_health:
        await conn.execute("ALTER TABLE snapshots ADD COLUMN health_score REAL")

    cursor = await conn.execute("PRAGMA table_info(subnet_registry)")
    registry_cols = {row[1] for row in await cursor.fetchall()}
    for col, definition in [
        ("category", "TEXT"),
        ("category_confirmed", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if col not in registry_cols:
            await conn.execute(
                f"ALTER TABLE subnet_registry ADD COLUMN {col} {definition}"
            )

    cursor = await conn.execute("PRAGMA table_info(subnet_milestones)")
    milestone_cols = {row[1] for row in await cursor.fetchall()}
    for col in ("ai_summary", "ai_take"):
        if milestone_cols and col not in milestone_cols:
            await conn.execute(
                f"ALTER TABLE subnet_milestones ADD COLUMN {col} TEXT"
            )
    await conn.commit()
    return conn


async def insert_snapshot(db: aiosqlite.Connection, snap: SubnetSnapshot) -> None:
    await db.execute("""
        INSERT INTO snapshots (
            netuid, polled_at, alpha_price_tao, alpha_mcap_tao, alpha_mcap_usd,
            tao_in_tao, volume_24h_alpha, tao_usd_price, daily_emission_tao, emission_rank,
            net_tao_flow_tao, n_neurons, max_allowed_uids, reg_cost_tao, owner_coldkey,
            gh_last_push, gh_stars, gh_forks, gh_open_issues,
            x_last_tweet, x_followers,
            yield_score, health_score, momentum_score, hype_score, composite_score
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        snap.netuid, _dt_to_str(snap.polled_at),
        snap.alpha_price_tao, snap.alpha_mcap_tao, snap.alpha_mcap_usd,
        snap.tao_in_tao, snap.volume_24h_alpha, snap.tao_usd_price,
        snap.daily_emission_tao, snap.emission_rank,
        snap.net_tao_flow_tao,
        snap.n_neurons, snap.max_allowed_uids, snap.reg_cost_tao, snap.owner_coldkey,
        _dt_to_str(snap.gh_last_push), snap.gh_stars, snap.gh_forks, snap.gh_open_issues,
        _dt_to_str(snap.x_last_tweet), snap.x_followers,
        snap.yield_score, snap.health_score, snap.momentum_score, snap.hype_score,
        snap.composite_score,
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
        SELECT s.*, r.name, r.github_url, r.x_handle, r.website,
               r.category, r.category_confirmed
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


async def get_owner_change_counts(db: aiosqlite.Connection,
                                   days: int = 30) -> dict[int, int]:
    """Return {netuid: distinct_owner_count} over the last `days` days.
    Subnets with no owner_coldkey data are omitted (caller defaults to 1 = stable)."""
    cursor = await db.execute("""
        SELECT netuid, COUNT(DISTINCT owner_coldkey) AS owner_count
        FROM snapshots
        WHERE datetime(polled_at) >= datetime('now', ? || ' days')
          AND owner_coldkey IS NOT NULL
        GROUP BY netuid
    """, (f"-{days}",))
    rows = await cursor.fetchall()
    return {row["netuid"]: row["owner_count"] for row in rows}


async def get_reg_cost_7d_ago(db: aiosqlite.Connection) -> dict[int, Optional[float]]:
    """Return {netuid: reg_cost_tao} from the most recent snapshot ≥7 days old per netuid."""
    cursor = await db.execute("""
        SELECT netuid, reg_cost_tao
        FROM snapshots s1
        WHERE polled_at = (
            SELECT MAX(polled_at) FROM snapshots s2
            WHERE s2.netuid = s1.netuid
            AND datetime(s2.polled_at) <= datetime('now', '-7 days')
        )
    """)
    rows = await cursor.fetchall()
    return {row["netuid"]: row["reg_cost_tao"] for row in rows}


async def get_subnet_detail(db: aiosqlite.Connection,
                             netuid: int) -> Optional[aiosqlite.Row]:
    """Latest snapshot for one netuid LEFT JOINed with subnet_registry."""
    cursor = await db.execute("""
        SELECT s.*, r.name, r.github_url, r.x_handle, r.website,
               r.category, r.category_confirmed
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


async def upsert_portfolio_position(db: aiosqlite.Connection,
                                     coldkey: str, netuid: int,
                                     alpha_amount: float, tao_value: float) -> None:
    """Insert or update a portfolio position. baseline_tao_value is frozen once > 0."""
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("""
        INSERT INTO portfolio_positions
            (coldkey, netuid, alpha_amount, tao_value, baseline_tao_value, first_seen_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(coldkey, netuid) DO UPDATE SET
            alpha_amount = excluded.alpha_amount,
            tao_value = excluded.tao_value,
            updated_at = excluded.updated_at,
            baseline_tao_value = CASE
                WHEN excluded.tao_value > 0 AND portfolio_positions.baseline_tao_value = 0
                THEN excluded.tao_value
                ELSE portfolio_positions.baseline_tao_value
            END
    """, (coldkey, netuid, alpha_amount, tao_value, tao_value, now, now))
    await db.commit()


async def delete_gone_positions(db: aiosqlite.Connection,
                                 coldkey: str,
                                 current_netuids: set[int]) -> None:
    """Remove positions for coldkey that are no longer in current_netuids (fully unstaked)."""
    if not current_netuids:
        await db.execute("DELETE FROM portfolio_positions WHERE coldkey = ?", (coldkey,))
    else:
        placeholders = ",".join("?" * len(current_netuids))
        await db.execute(
            f"DELETE FROM portfolio_positions WHERE coldkey = ? AND netuid NOT IN ({placeholders})",
            (coldkey, *current_netuids),
        )
    await db.commit()


async def get_portfolio_positions(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """All positions LEFT JOINed with subnet_registry, ordered by coldkey then netuid."""
    cursor = await db.execute("""
        SELECT p.*, r.name, r.category, s.tao_usd_price
        FROM portfolio_positions p
        LEFT JOIN subnet_registry r ON p.netuid = r.netuid
        LEFT JOIN (
            SELECT netuid, tao_usd_price, MAX(polled_at) AS max_ts
            FROM snapshots GROUP BY netuid
        ) s ON p.netuid = s.netuid
        ORDER BY p.coldkey, p.netuid
    """)
    return await cursor.fetchall()


async def get_staked_netuids(db: aiosqlite.Connection) -> set[int]:
    """Return set of netuids with any active portfolio position."""
    cursor = await db.execute("SELECT DISTINCT netuid FROM portfolio_positions")
    rows = await cursor.fetchall()
    return {row["netuid"] for row in rows}


async def get_active_analyst_coverage_netuids(db: aiosqlite.Connection,
                                              decay_hours: int) -> set[int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=decay_hours)).isoformat()
    cursor = await db.execute(
        "SELECT DISTINCT netuid FROM analyst_mentions WHERE mentioned_at > ?",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return {row["netuid"] for row in rows}


async def get_recent_milestone_netuids(db: aiosqlite.Connection,
                                       hours: int) -> set[int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    cursor = await db.execute(
        "SELECT DISTINCT netuid FROM subnet_milestones WHERE published_at > ?",
        (cutoff,),
    )
    rows = await cursor.fetchall()
    return {row["netuid"] for row in rows}


async def get_analyst_watchlist(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM analyst_watchlist ORDER BY added_at DESC"
    )
    return await cursor.fetchall()


async def add_analyst_handle(db: aiosqlite.Connection,
                             handle: str,
                             source: str = "dashboard") -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        """
        INSERT OR IGNORE INTO analyst_watchlist (handle, added_at, source)
        VALUES (?, ?, ?)
        """,
        (handle.lstrip("@"), now, source),
    )
    await db.commit()


async def remove_analyst_handle(db: aiosqlite.Connection, handle: str) -> None:
    await db.execute(
        "DELETE FROM analyst_watchlist WHERE handle = ? AND source = 'dashboard'",
        (handle.lstrip("@"),),
    )
    await db.commit()


async def insert_analyst_mention(db: aiosqlite.Connection,
                                 handle: str,
                                 netuid: int,
                                 tweet_url: str,
                                 tweet_text: str,
                                 mentioned_at: datetime) -> bool:
    try:
        await db.execute(
            """
            INSERT INTO analyst_mentions
                (analyst_handle, netuid, tweet_url, tweet_text, mentioned_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (handle.lstrip("@"), netuid, tweet_url, tweet_text, mentioned_at.isoformat()),
        )
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_unnotified_analyst_mentions(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM analyst_mentions WHERE notified=0 ORDER BY mentioned_at ASC"
    )
    return await cursor.fetchall()


async def mark_analyst_mentions_notified(db: aiosqlite.Connection,
                                         ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE analyst_mentions SET notified=1 WHERE id IN ({placeholders})",
        ids,
    )
    await db.commit()


async def get_analyst_mentions_for_netuid(db: aiosqlite.Connection,
                                          netuid: int,
                                          limit: int = 10) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        """
        SELECT * FROM analyst_mentions
        WHERE netuid=?
        ORDER BY mentioned_at DESC LIMIT ?
        """,
        (netuid, limit),
    )
    return await cursor.fetchall()


async def has_active_analyst_coverage(db: aiosqlite.Connection,
                                      netuid: int,
                                      decay_hours: int) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=decay_hours)).isoformat()
    cursor = await db.execute(
        "SELECT COUNT(*) FROM analyst_mentions WHERE netuid=? AND mentioned_at > ?",
        (netuid, cutoff),
    )
    row = await cursor.fetchone()
    return row[0] > 0


async def get_covered_netuids(db: aiosqlite.Connection, decay_hours: int) -> set[int]:
    """Return the set of netuids with at least one analyst mention within decay_hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=decay_hours)).isoformat()
    cursor = await db.execute(
        "SELECT DISTINCT netuid FROM analyst_mentions WHERE mentioned_at > ?",
        (cutoff,),
    )
    return {row[0] for row in await cursor.fetchall()}


async def insert_milestone(db: aiosqlite.Connection,
                           netuid: int,
                           milestone_type: str,
                           title: str,
                           url: str,
                           published_at: datetime,
                           ai_summary: Optional[str] = None,
                           ai_take: Optional[str] = None) -> bool:
    try:
        await db.execute(
            """
            INSERT INTO subnet_milestones
                (netuid, milestone_type, title, url, published_at, ai_summary, ai_take)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (netuid, milestone_type, title, url, published_at.isoformat(), ai_summary, ai_take),
        )
        await db.commit()
        return True
    except aiosqlite.IntegrityError:
        return False


async def get_unnotified_milestones(db: aiosqlite.Connection) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        "SELECT * FROM subnet_milestones WHERE notified=0 ORDER BY published_at ASC"
    )
    return await cursor.fetchall()


async def mark_milestones_notified(db: aiosqlite.Connection,
                                   ids: list[int]) -> None:
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    await db.execute(
        f"UPDATE subnet_milestones SET notified=1 WHERE id IN ({placeholders})",
        ids,
    )
    await db.commit()


async def get_milestones_for_netuid(db: aiosqlite.Connection,
                                    netuid: int,
                                    limit: int = 10) -> list[aiosqlite.Row]:
    cursor = await db.execute(
        """
        SELECT * FROM subnet_milestones
        WHERE netuid=?
        ORDER BY published_at DESC LIMIT ?
        """,
        (netuid, limit),
    )
    return await cursor.fetchall()


async def get_collector_state(db: aiosqlite.Connection, key: str) -> Optional[str]:
    cursor = await db.execute(
        "SELECT value FROM collector_state WHERE key=?",
        (key,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def set_collector_state(db: aiosqlite.Connection, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO collector_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )
    await db.commit()


async def update_registry_category(db: aiosqlite.Connection,
                                   netuid: int,
                                   category: str,
                                   confirmed: bool = False) -> None:
    if confirmed:
        await db.execute(
            """
            UPDATE subnet_registry
            SET category=?, category_confirmed=1
            WHERE netuid=?
            """,
            (category, netuid),
        )
    else:
        await db.execute(
            """
            UPDATE subnet_registry
            SET category=?
            WHERE netuid=? AND (category_confirmed IS NULL OR category_confirmed=0)
            """,
            (category, netuid),
        )
    await db.commit()


async def get_recent_alert_types_per_netuid(db: aiosqlite.Connection,
                                            alert_types: list[str],
                                            hours: int) -> dict[int, set[str]]:
    if not alert_types:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    placeholders = ",".join("?" * len(alert_types))
    cursor = await db.execute(
        f"""
        SELECT netuid, alert_type
        FROM alerts
        WHERE alert_type IN ({placeholders}) AND fired_at > ?
        """,
        (*alert_types, cutoff),
    )
    rows = await cursor.fetchall()
    result: dict[int, set[str]] = {}
    for row in rows:
        result.setdefault(row["netuid"], set()).add(row["alert_type"])
    return result
