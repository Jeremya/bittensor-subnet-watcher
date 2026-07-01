# main.py
# SSL fix MUST be first — before any bittensor import
import os
import certifi
os.environ['SSL_CERT_FILE'] = certifi.where()

import asyncio
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import config
config.validate_config()  # exit(1) if missing required env vars

from models import SubnetSnapshot
from db.database import init_db, insert_snapshot, get_latest_snapshots, \
    get_unsent_alerts, mark_alerts_sent, prune_old_snapshots, get_registry, \
    get_snapshots_for_netuid, \
    upsert_portfolio_position, delete_gone_positions, update_registry_category, \
    get_recent_alert_types_per_netuid, get_active_analyst_coverage_netuids, \
    get_recent_milestone_netuids, get_emergence_age_context
from collectors.analyst import AnalystCollector
from collectors.chain import ChainCollector, init_subtensor, close_subtensor
from collectors.github import GitHubCollector
from collectors.milestone import MilestoneCollector
from collectors.x_scraper import XCollector, close_browser
from collectors.registry import RegistryCollector
from collectors.portfolio import PortfolioCollector
from engine.scorer import score_snapshots
from engine.signals import SCORING_ALERT_TYPES
from engine.emergence import score_emergence
from engine.alerts import (
    evaluate_alerts,
    evaluate_convergence,
    fire_analyst_alerts,
    fire_milestone_alerts,
)
from bot.telegram import TelegramBot
from web.routes import create_app

# ── Logging ──────────────────────────────────────────────────────────────────

Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.handlers.RotatingFileHandler(
            "logs/monitor.log", maxBytes=10 * 1024 * 1024, backupCount=3
        ),
    ],
)
logger = logging.getLogger(__name__)

# ── Globals (set during startup) ─────────────────────────────────────────────
_db = None
_telegram: TelegramBot | None = None
_cycle_count = 0


# ── Poll cycle ────────────────────────────────────────────────────────────────

async def poll_cycle() -> None:
    global _cycle_count
    _cycle_count += 1
    cycle_id = _cycle_count
    start = datetime.now(timezone.utc)
    logger.info("[POLL_START] cycle_id=%d", cycle_id)

    # 1. Collect chain data (price, emission, n_neurons, etc.)
    chain_snapshots = await ChainCollector.collect()

    # Load latest stored snapshots early so portfolio valuation can fall back to
    # the last known prices when a chain cycle fails before producing snapshots.
    prev_snapshots = await get_latest_snapshots(_db)
    prev_by_netuid: dict[int, dict] = {row["netuid"]: row for row in prev_snapshots}

    # 1b. Portfolio positions (uses prices from chain_snapshots)
    if config.WALLET_COLDKEYS:
        from collectors.chain import _subtensor as _chain_subtensor
        price_by_netuid = {
            row["netuid"]: row["alpha_price_tao"]
            for row in prev_snapshots
            if row["alpha_price_tao"] is not None
        }
        price_by_netuid.update({
            s.netuid: s.alpha_price_tao for s in chain_snapshots
            if s.alpha_price_tao is not None
        })
        portfolio = await PortfolioCollector.collect(
            _chain_subtensor, config.WALLET_COLDKEYS, price_by_netuid
        )
        for coldkey, positions in portfolio.items():
            for netuid, data in positions.items():
                await upsert_portfolio_position(
                    _db, coldkey, netuid, data["alpha_amount"], data["tao_value"]
                )
            await delete_gone_positions(_db, coldkey, set(positions.keys()))

    # 2. Retrieve registry and X data
    registry = await get_registry(_db)
    try:
        x_data = await asyncio.wait_for(XCollector.collect(registry), timeout=300)
    except asyncio.TimeoutError:
        logger.warning("[POLL] XCollector timed out after 300s — skipping X data this cycle")
        x_data = {}
    except Exception as exc:
        logger.warning("[POLL] XCollector failed — skipping X data this cycle: %s", exc)
        x_data = {}

    # Merge X data into chain snapshots
    for snap in chain_snapshots:
        if snap.netuid in x_data:
            snap.x_followers = x_data[snap.netuid].get("x_followers")
            snap.x_last_tweet = x_data[snap.netuid].get("x_last_tweet")

    # Also carry forward GitHub data from previous snapshot (if available)
    for snap in chain_snapshots:
        prev = prev_by_netuid.get(snap.netuid)
        if prev and snap.gh_last_push is None:
            raw = prev["gh_last_push"]
            snap.gh_last_push = (
                datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
                if raw else None
            )
            snap.gh_stars = prev["gh_stars"]
            snap.gh_forks = prev["gh_forks"]
            snap.gh_open_issues = prev["gh_open_issues"]

    # 3a. Compute emission-adjusted net TAO flow for this poll interval.
    #     net_tao_flow = Δ(tao_in) − emission_accrual
    #     This isolates pure net staking inflows/outflows from emission padding.
    for snap in chain_snapshots:
        prev = prev_by_netuid.get(snap.netuid)
        # Use tao_in_tao (pool reserve) for flow calc — not true mcap.
        # Fall back to alpha_mcap_tao for rows collected before tao_in_tao was added.
        prev_tao_in = (prev["tao_in_tao"] if prev and prev["tao_in_tao"] is not None
                       else (prev["alpha_mcap_tao"] if prev else None))
        if prev_tao_in is not None and snap.tao_in_tao is not None:
            prev_time = datetime.fromisoformat(prev["polled_at"]).replace(tzinfo=timezone.utc)
            elapsed_days = (start - prev_time).total_seconds() / 86400
            emission_accrual = (snap.daily_emission_tao or 0.0) * elapsed_days
            snap.net_tao_flow_tao = snap.tao_in_tao - prev_tao_in - emission_accrual

    # 3b. Score
    history_by_netuid: dict[int, list[SubnetSnapshot]] = {}
    for snap in chain_snapshots:
        rows = await get_snapshots_for_netuid(_db, snap.netuid, limit=config.MOMENTUM_HISTORY_LIMIT)
        history_by_netuid[snap.netuid] = [
            SubnetSnapshot(
                netuid=r["netuid"],
                polled_at=datetime.fromisoformat(r["polled_at"]),
                alpha_price_tao=r["alpha_price_tao"],
                alpha_mcap_tao=r["alpha_mcap_tao"],
                emission_rank=r["emission_rank"],
                net_tao_flow_tao=r["net_tao_flow_tao"],
                reg_cost_tao=r["reg_cost_tao"],
                n_neurons=r["n_neurons"],
                max_allowed_uids=r["max_allowed_uids"],
            )
            for r in rows
        ]
    alert_types_for_scoring = await get_recent_alert_types_per_netuid(
        _db,
        SCORING_ALERT_TYPES,
        config.ANALYST_COVERAGE_DECAY_HOURS,
    )
    coverage_netuids_for_scoring = await get_active_analyst_coverage_netuids(
        _db, config.ANALYST_COVERAGE_DECAY_HOURS
    )
    milestone_netuids_for_scoring = await get_recent_milestone_netuids(
        _db, config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS
    )
    emergence_age_ctx = await get_emergence_age_context(_db)
    score_emergence(chain_snapshots, history_by_netuid, emergence_age_ctx, now=start)
    score_snapshots(
        chain_snapshots,
        history_by_netuid,
        alert_types_by_netuid=alert_types_for_scoring,
        coverage_netuids=coverage_netuids_for_scoring,
        milestone_netuids=milestone_netuids_for_scoring,
    )

    # 4. Persist snapshots
    for snap in chain_snapshots:
        await insert_snapshot(_db, snap)

    # 5. Health check: warn if >50% have None emission
    none_count = sum(1 for s in chain_snapshots if s.daily_emission_tao is None)
    if chain_snapshots and none_count / len(chain_snapshots) > config.HEALTH_CHECK_NONE_THRESHOLD:
        msg = (f"ChainCollector: {none_count}/{len(chain_snapshots)} subnets "
               f"missing emission data")
        logger.warning("[HEALTH] %s", msg)
        if _telegram:
            await _telegram.send_health_warning(msg)

    # 6. Fire alerts
    prev_snaps_obj: dict[int, SubnetSnapshot] = {}
    for netuid, row in prev_by_netuid.items():
        prev_snaps_obj[netuid] = SubnetSnapshot(
            netuid=netuid,
            polled_at=datetime.fromisoformat(row["polled_at"]) if row["polled_at"] else start,
            alpha_price_tao=row["alpha_price_tao"],
            emission_rank=row["emission_rank"],
            gh_stars=row["gh_stars"],
            gh_forks=row["gh_forks"],
            owner_coldkey=row["owner_coldkey"],
            reg_cost_tao=row["reg_cost_tao"],
            max_allowed_uids=row["max_allowed_uids"],
        )

    known_netuids = set(prev_by_netuid.keys())
    await evaluate_alerts(_db, chain_snapshots, registry, prev_snaps_obj, known_netuids)
    await evaluate_convergence(_db, registry)

    # 7. Send unsent alerts via Telegram
    if _telegram:
        unsent = await get_unsent_alerts(_db)
        if unsent:
            alert_ids = [row["id"] for row in unsent]
            from models import AlertRecord
            alert_objs = [
                AlertRecord(
                    fired_at=datetime.fromisoformat(row["fired_at"]),
                    netuid=row["netuid"],
                    subnet_name=row["subnet_name"],
                    alert_type=row["alert_type"],
                    description=row["description"],
                    current_value=row["current_value"],
                    threshold=row["threshold"],
                )
                for row in unsent
            ]
            sent_ids = await _telegram.send_alerts(alert_objs, alert_ids)
            await mark_alerts_sent(_db, sent_ids)
            logger.info("[TELEGRAM] sent=%d failed=%d", len(sent_ids),
                        len(alert_ids) - len(sent_ids))

    duration = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info("[POLL_END] cycle_id=%d duration=%.0fs subnets=%d",
                cycle_id, duration, len(chain_snapshots))


async def github_collect() -> None:
    """60-min GitHub data refresh. Updates snapshots and registry categories in DB."""
    registry = await get_registry(_db)
    gh_data = await GitHubCollector.collect(registry)
    for netuid, data in gh_data.items():
        gh_push = data["gh_last_push"].isoformat() if data["gh_last_push"] else None
        await _db.execute("""
            UPDATE snapshots SET gh_last_push=?, gh_stars=?, gh_forks=?, gh_open_issues=?
            WHERE id = (SELECT id FROM snapshots WHERE netuid=? ORDER BY polled_at DESC LIMIT 1)
        """, (gh_push, data["gh_stars"], data["gh_forks"], data["gh_open_issues"], netuid))
        if "category" in data:
            await update_registry_category(_db, netuid, data["category"], confirmed=False)
    await _db.commit()
    logger.info("[COLLECTOR] github_refresh complete subnets=%d", len(gh_data))


async def analyst_collect() -> None:
    """60-min analyst X handle scrape. Inserts new mentions and fires alerts."""
    registry = await get_registry(_db)
    await AnalystCollector.collect(_db, registry)
    new_alerts = await fire_analyst_alerts(_db, registry)
    logger.info("[COLLECTOR] analyst_collect done new_alerts=%d", len(new_alerts))


async def milestone_collect() -> None:
    """6-hour milestone poll (arXiv + HuggingFace). Inserts new milestones and fires alerts."""
    registry = await get_registry(_db)
    await MilestoneCollector.collect(_db, registry)
    new_alerts = await fire_milestone_alerts(_db, registry)
    logger.info("[COLLECTOR] milestone_collect done new_alerts=%d", len(new_alerts))


async def registry_refresh_and_prune() -> None:
    """Daily: refresh subnet registry and prune old snapshots."""
    from collectors.chain import _subtensor
    if _subtensor:
        dynamic_list = await _subtensor.all_subnets()
        await RegistryCollector.refresh(_db, dynamic_list)
    await prune_old_snapshots(_db, days=30)


# ── Startup ──────────────────────────────────────────────────────────────────

async def main() -> None:
    global _db, _telegram

    # Init DB
    _db = await init_db(config.DB_PATH)

    # Init Bittensor
    await init_subtensor()

    # Init Telegram (validate token at startup)
    from telegram.error import Forbidden, InvalidToken
    _telegram = TelegramBot(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID)
    try:
        await _telegram.validate_token()
    except (Forbidden, InvalidToken):
        logger.error("[STARTUP ERROR] Invalid Telegram token. Check TELEGRAM_BOT_TOKEN.")
        raise SystemExit(1)

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        poll_cycle, "interval", minutes=config.POLL_INTERVAL_MINUTES,
        max_instances=1, misfire_grace_time=60, id="poll"
    )
    scheduler.add_job(
        github_collect, "interval", minutes=60,
        max_instances=1, id="github"
    )
    scheduler.add_job(
        analyst_collect, "interval", minutes=60,
        max_instances=1, id="analyst"
    )
    scheduler.add_job(
        milestone_collect, "interval", hours=6,
        max_instances=1, id="milestone"
    )
    scheduler.add_job(
        registry_refresh_and_prune, "interval", hours=24,
        max_instances=1, id="registry"
    )
    scheduler.start()

    # Run initial poll + registry immediately
    asyncio.create_task(registry_refresh_and_prune())
    asyncio.create_task(poll_cycle())

    logger.info("[STARTUP] db=ok telegram=ok scheduler=ok dashboard=http://%s:%d",
                config.DASHBOARD_HOST, config.DASHBOARD_PORT)

    # FastAPI
    app = create_app(_db)
    server_config = uvicorn.Config(
        app, host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, log_level="warning"
    )
    server = uvicorn.Server(server_config)
    try:
        await server.serve()
    finally:
        scheduler.shutdown()
        await close_subtensor()
        await close_browser()
        if _db:
            await _db.close()


if __name__ == "__main__":
    asyncio.run(main())
