import aiosqlite
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from models import SubnetSnapshot, AlertRecord
from db.database import is_alert_in_cooldown, insert_alert
from engine.flow_impulse import FlowImpulse, classify_flow_impulse
from engine.signals import FLOW_CATALYST_ALERTS
import config

logger = logging.getLogger(__name__)


def _registry_name(registry: dict, netuid: int) -> str:
    row = registry.get(netuid)
    if row is None:
        return f"SN{netuid}"
    if isinstance(row, dict):
        return row.get("name") or f"SN{netuid}"
    try:
        return row["name"] or f"SN{netuid}"
    except (KeyError, TypeError, IndexError):
        return getattr(row, "name", None) or f"SN{netuid}"


def check_emission_divergence(snap: SubnetSnapshot,
                               emission_rank: int,
                               mcap_rank: int) -> Optional[AlertRecord]:
    if emission_rank is None or emission_rank == 0 or mcap_rank is None or mcap_rank == 0:
        return None
    ratio = mcap_rank / emission_rank
    if ratio > config.EMISSION_DIVERGENCE_RATIO:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="emission_divergence",
            description=(f"Emission rank #{emission_rank} / MCap rank #{mcap_rank} "
                         f"→ ratio {ratio:.1f}x"),
            current_value=round(ratio, 2),
            threshold=config.EMISSION_DIVERGENCE_RATIO,
        )
    return None


def check_dead_github(snap: SubnetSnapshot) -> Optional[AlertRecord]:
    if snap.gh_last_push is None:
        return None
    if snap.alpha_mcap_usd is None or snap.alpha_mcap_usd < config.DEAD_GITHUB_MIN_MCAP_USD:
        return None
    age_days = (datetime.now(timezone.utc) - snap.gh_last_push).days
    if age_days > config.DEAD_GITHUB_DAYS:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="dead_github",
            description=f"No GitHub commit in {age_days} days (mcap ${snap.alpha_mcap_usd:,.0f})",
            current_value=float(age_days),
            threshold=float(config.DEAD_GITHUB_DAYS),
        )
    return None


def check_emission_drop(current: SubnetSnapshot,
                         prev: SubnetSnapshot) -> Optional[AlertRecord]:
    if current.emission_rank is None or prev.emission_rank is None:
        return None
    drop = current.emission_rank - prev.emission_rank
    if drop > config.EMISSION_DROP_RANKS:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=current.netuid,
            subnet_name=f"SN{current.netuid}",
            alert_type="emission_drop",
            description=(f"Emission rank dropped from #{prev.emission_rank} "
                         f"to #{current.emission_rank} ({drop} positions)"),
            current_value=float(drop),
            threshold=float(config.EMISSION_DROP_RANKS),
        )
    return None


def check_github_spike(current: SubnetSnapshot,
                        prev: SubnetSnapshot) -> Optional[AlertRecord]:
    if current.gh_stars is None or prev.gh_stars is None:
        return None
    if prev.gh_stars > 0 and current.gh_stars >= prev.gh_stars * config.GITHUB_SPIKE_MULTIPLIER:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=current.netuid,
            subnet_name=f"SN{current.netuid}",
            alert_type="github_spike",
            description=(f"GitHub stars jumped from {prev.gh_stars} to {current.gh_stars}"),
            current_value=float(current.gh_stars),
            threshold=float(prev.gh_stars * config.GITHUB_SPIKE_MULTIPLIER),
        )
    if (current.gh_forks is not None and prev.gh_forks is not None
            and prev.gh_forks > 0
            and current.gh_forks >= prev.gh_forks * config.GITHUB_SPIKE_MULTIPLIER):
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=current.netuid,
            subnet_name=f"SN{current.netuid}",
            alert_type="github_spike",
            description=(f"GitHub forks jumped from {prev.gh_forks} to {current.gh_forks}"),
            current_value=float(current.gh_forks),
            threshold=float(prev.gh_forks * config.GITHUB_SPIKE_MULTIPLIER),
        )
    return None


def check_tao_outflow(snap: SubnetSnapshot) -> Optional[AlertRecord]:
    """Net TAO outflow > NET_OUTFLOW_ALERT_PCT of pool in one poll → capital flight."""
    if snap.net_tao_flow_tao is None or snap.alpha_mcap_tao is None:
        return None
    if snap.alpha_mcap_tao <= 0:
        return None
    outflow_pct = -snap.net_tao_flow_tao / snap.alpha_mcap_tao
    if outflow_pct > config.NET_OUTFLOW_ALERT_PCT:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="tao_outflow",
            description=(
                f"Net TAO outflow {outflow_pct*100:.1f}% of pool in one poll "
                f"({snap.net_tao_flow_tao:+.1f} τ)"
            ),
            current_value=round(outflow_pct, 4),
            threshold=config.NET_OUTFLOW_ALERT_PCT,
        )
    return None


def check_whale_inflow(snap: SubnetSnapshot) -> Optional[AlertRecord]:
    """Net TAO inflow > WHALE_INFLOW_PCT of pool in one poll → large capital entry."""
    if snap.net_tao_flow_tao is None or snap.alpha_mcap_tao is None:
        return None
    if snap.alpha_mcap_tao <= 0:
        return None
    inflow_pct = snap.net_tao_flow_tao / snap.alpha_mcap_tao
    if inflow_pct > config.WHALE_INFLOW_PCT:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="whale_inflow",
            description=(
                f"Net TAO inflow {inflow_pct*100:.1f}% of pool in one poll "
                f"({snap.net_tao_flow_tao:+.1f} τ)"
            ),
            current_value=round(inflow_pct, 4),
            threshold=config.WHALE_INFLOW_PCT,
        )
    return None


def _flow_impulse_description(impulse: FlowImpulse) -> str:
    direction = impulse.direction
    slippage = (
        impulse.buy_slippage_pct
        if direction == "buy"
        else impulse.sell_slippage_pct
    )
    parts = [
        (
            f"Important {direction} pressure: {impulse.flow_tao:+.1f} TAO net flow "
            f"in one poll, {impulse.relative_flow_pct * 100:.1f}% of pool "
            f"(threshold {impulse.threshold_pct * 100:.1f}%)."
        )
    ]
    context: list[str] = []
    if impulse.price_move_pct is not None:
        context.append(f"Price {impulse.price_move_pct:+.1f}% since prior poll")
    if slippage is not None:
        context.append(
            f"{direction.title()} slippage {slippage:.1f}% on reference size"
        )
    if impulse.volume_turnover_pct is not None:
        context.append(f"24h turnover {impulse.volume_turnover_pct:.2f}% of pool")
    context.append(f"Impact score {impulse.impact_score:.0f}/100")
    parts.append(". ".join(context) + ".")
    parts.append("Source: emission-adjusted snapshot net flow, not wallet-attributed.")
    return " ".join(parts)


def check_flow_impulse(
    current: SubnetSnapshot,
    prev: SubnetSnapshot | None = None,
) -> Optional[AlertRecord]:
    """Important buy/sell pressure from emission-adjusted snapshot net flow."""
    impulse = classify_flow_impulse(current, prev)
    if impulse is None:
        return None
    return AlertRecord(
        fired_at=datetime.now(timezone.utc),
        netuid=current.netuid,
        subnet_name=f"SN{current.netuid}",
        alert_type=impulse.alert_type,
        description=_flow_impulse_description(impulse),
        current_value=impulse.relative_flow_pct,
        threshold=impulse.threshold_pct,
    )


def check_emission_near_zero(snap: SubnetSnapshot) -> Optional[AlertRecord]:
    """Daily emission below EMISSION_NEAR_ZERO_TAO — subnet losing its emission share."""
    if snap.daily_emission_tao is None:
        return None
    if snap.alpha_mcap_usd is None or snap.alpha_mcap_usd < config.EMISSION_NEAR_ZERO_MIN_MCAP_USD:
        return None
    if snap.daily_emission_tao < config.EMISSION_NEAR_ZERO_TAO:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="emission_near_zero",
            description=(
                f"Daily emission {snap.daily_emission_tao:.2f} τ/day "
                f"(threshold {config.EMISSION_NEAR_ZERO_TAO} τ/day)"
            ),
            current_value=round(snap.daily_emission_tao, 4),
            threshold=config.EMISSION_NEAR_ZERO_TAO,
        )
    return None


def check_liquidity_floor(snap: SubnetSnapshot) -> Optional[AlertRecord]:
    """Daily volume/pool ratio below LIQUIDITY_FLOOR_RATIO — investors may be trapped."""
    if (snap.volume_24h_alpha is None or snap.alpha_price_tao is None
            or snap.alpha_mcap_tao is None):
        return None
    if snap.alpha_mcap_tao <= 0:
        return None
    if snap.alpha_mcap_usd is None or snap.alpha_mcap_usd < config.LIQUIDITY_MIN_MCAP_USD:
        return None
    ratio = (snap.volume_24h_alpha * snap.alpha_price_tao) / snap.alpha_mcap_tao
    if ratio < config.LIQUIDITY_FLOOR_RATIO:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="liquidity_floor",
            description=(
                f"24h volume/pool ratio {ratio*100:.3f}% — effectively illiquid "
                f"(threshold {config.LIQUIDITY_FLOOR_RATIO*100:.1f}%)"
            ),
            current_value=round(ratio, 6),
            threshold=config.LIQUIDITY_FLOOR_RATIO,
        )
    return None


def check_hyperparameter_change(current: SubnetSnapshot,
                                  prev: SubnetSnapshot) -> Optional[AlertRecord]:
    """Reg cost change > REG_COST_CHANGE_PCT or max_allowed_uids change — owner intervention."""
    # Reg cost shift
    if (current.reg_cost_tao is not None and prev.reg_cost_tao is not None
            and prev.reg_cost_tao > 0):
        pct = (current.reg_cost_tao - prev.reg_cost_tao) / prev.reg_cost_tao
        if abs(pct) > config.REG_COST_CHANGE_PCT:
            direction = "↑" if pct > 0 else "↓"
            return AlertRecord(
                fired_at=datetime.now(timezone.utc),
                netuid=current.netuid,
                subnet_name=f"SN{current.netuid}",
                alert_type="hyperparameter_change",
                description=(
                    f"Reg cost {direction}{abs(pct)*100:.0f}%: "
                    f"{prev.reg_cost_tao:.4f} → {current.reg_cost_tao:.4f} τ"
                ),
                current_value=round(pct, 4),
                threshold=config.REG_COST_CHANGE_PCT,
            )
    # Capacity ceiling shift
    if (current.max_allowed_uids is not None and prev.max_allowed_uids is not None
            and current.max_allowed_uids != prev.max_allowed_uids):
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=current.netuid,
            subnet_name=f"SN{current.netuid}",
            alert_type="hyperparameter_change",
            description=(
                f"max_allowed_uids changed: {prev.max_allowed_uids} → {current.max_allowed_uids}"
            ),
            current_value=float(current.max_allowed_uids),
            threshold=float(prev.max_allowed_uids),
        )
    return None


def check_new_entry(snap: SubnetSnapshot, known_netuids: set[int]) -> Optional[AlertRecord]:
    if snap.netuid not in known_netuids:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="new_entry",
            description=f"New subnet SN{snap.netuid} appeared in registry",
            current_value=float(snap.netuid),
            threshold=None,
        )
    return None


def check_ownership_transfer(current: SubnetSnapshot,
                              prev: SubnetSnapshot) -> Optional[AlertRecord]:
    if current.owner_coldkey is None or prev.owner_coldkey is None:
        return None
    if current.owner_coldkey == prev.owner_coldkey:
        return None
    return AlertRecord(
        fired_at=datetime.now(timezone.utc),
        netuid=current.netuid,
        subnet_name=f"SN{current.netuid}",
        alert_type="ownership_transfer",
        description=(
            f"Owner changed: {prev.owner_coldkey[:8]}... → {current.owner_coldkey[:8]}..."
        ),
        current_value=None,
        threshold=None,
    )


def check_emergence_watch(snap: SubnetSnapshot) -> Optional[AlertRecord]:
    """Watch-only alert for emerging subnets; never feeds buy recommendations."""
    if snap.emergence_score is None or snap.emergence_stage == "established":
        return None
    if snap.emergence_score < config.EMERGENCE_WATCH_SCORE:
        return None
    return AlertRecord(
        fired_at=datetime.now(timezone.utc),
        netuid=snap.netuid,
        subnet_name=f"SN{snap.netuid}",
        alert_type="emergence_watch",
        description=(
            f"Emerging ({snap.emergence_stage}): emergence score "
            f"{snap.emergence_score:.0f} - watch only, not yet tradable"
        ),
        current_value=round(snap.emergence_score, 2),
        threshold=config.EMERGENCE_WATCH_SCORE,
    )


def _cooldown_hours_for_alert(alert_type: str) -> int:
    if alert_type == "emergence_watch":
        return config.EMERGENCE_WATCH_COOLDOWN_HOURS
    if alert_type in {"important_buy", "important_sell"}:
        return config.FLOW_IMPULSE_COOLDOWN_HOURS
    return config.ALERT_COOLDOWN_HOURS


async def evaluate_alerts(
    db: aiosqlite.Connection,
    snapshots: list[SubnetSnapshot],
    registry: dict,
    prev_by_netuid: dict[int, SubnetSnapshot],
    known_netuids: set[int],
) -> list[AlertRecord]:
    """
    Evaluate all alert conditions across all snapshots.
    Dedup via cooldown check. Persist new alerts to DB.
    Returns list of newly fired alerts.

    Project-monitoring alerts: emission_divergence, dead_github, emission_drop,
      github_spike, ownership_transfer, new_entry
    Capital-protection alerts: important_buy, important_sell,
      emission_near_zero, liquidity_floor, hyperparameter_change
    Legacy helpers kept for compatibility: tao_outflow, whale_inflow
    Watch-only alerts: emergence_watch
    """
    # Build mcap rank (sort by alpha_mcap_tao descending)
    valid_mcap = [(s.netuid, s.alpha_mcap_tao)
                  for s in snapshots if s.alpha_mcap_tao is not None]
    valid_mcap.sort(key=lambda x: x[1], reverse=True)
    mcap_rank_by_netuid = {netuid: rank + 1
                           for rank, (netuid, _) in enumerate(valid_mcap)}

    fired: list[AlertRecord] = []

    for snap in snapshots:
        candidates: list[Optional[AlertRecord]] = []
        prev = prev_by_netuid.get(snap.netuid)

        # ── Project-monitoring ────────────────────────────────────────────────
        # 1. Emission divergence
        em_rank = snap.emission_rank
        mc_rank = mcap_rank_by_netuid.get(snap.netuid)
        if em_rank is not None and mc_rank is not None:
            candidates.append(check_emission_divergence(snap, em_rank, mc_rank))

        # 2. Dead GitHub
        candidates.append(check_dead_github(snap))

        # 3. Emission drop (requires prev snapshot)
        if prev:
            candidates.append(check_emission_drop(snap, prev))

        # 4. GitHub spike (requires prev)
        if prev:
            candidates.append(check_github_spike(snap, prev))

        # 5. Ownership transfer (requires prev)
        if prev:
            candidates.append(check_ownership_transfer(snap, prev))

        # 7. New entry
        candidates.append(check_new_entry(snap, known_netuids))

        # ── Capital-protection ────────────────────────────────────────────────
        # 8. Important buy/sell pressure from emission-adjusted net flow.
        candidates.append(check_flow_impulse(snap, prev))

        # 10. Emission approaching zero
        candidates.append(check_emission_near_zero(snap))

        # 11. Liquidity floor breached
        candidates.append(check_liquidity_floor(snap))

        # 12. Owner hyperparameter change (requires prev)
        if prev:
            candidates.append(check_hyperparameter_change(snap, prev))

        # 13. Emerging candidate crossing watch threshold
        candidates.append(check_emergence_watch(snap))

        # Dedup and persist
        for alert in candidates:
            if alert is None:
                continue
            # Set subnet name from registry
            alert.subnet_name = _registry_name(registry, snap.netuid)
            cooldown_hours = _cooldown_hours_for_alert(alert.alert_type)
            in_cooldown = await is_alert_in_cooldown(
                db, snap.netuid, alert.alert_type, cooldown_hours
            )
            if not in_cooldown:
                await insert_alert(db, alert)
                fired.append(alert)
                logger.info("[ALERT] netuid=%d type=%s value=%s threshold=%s",
                            alert.netuid, alert.alert_type,
                            alert.current_value, alert.threshold)

    return fired


_CONVERGENCE_SIGNAL_TYPES = [
    "milestone",
    "analyst_mention",
    "whale_inflow",
    "important_buy",
    "github_spike",
]


def _normalize_convergence_signal_types(signal_types: set[str]) -> set[str]:
    return {
        "flow_inflow" if signal_type in FLOW_CATALYST_ALERTS else signal_type
        for signal_type in signal_types
    }


def _display_convergence_signal_types(signal_types: set[str]) -> list[str]:
    display_types = [
        signal_type
        for signal_type in signal_types
        if signal_type not in FLOW_CATALYST_ALERTS
    ]
    flow_aliases = sorted(FLOW_CATALYST_ALERTS & signal_types)
    if flow_aliases:
        display_types.append(f"flow_inflow ({', '.join(flow_aliases)})")
    return sorted(display_types)


def _count_convergence_signals(signals_by_netuid: dict[int, set[str]],
                               min_signals: int) -> dict[int, set[str]]:
    return {
        netuid: signal_types
        for netuid, signal_types in signals_by_netuid.items()
        if len(_normalize_convergence_signal_types(signal_types)) >= min_signals
    }


async def fire_analyst_alerts(
    db: aiosqlite.Connection,
    registry: dict,
) -> list[AlertRecord]:
    from db.database import (
        get_unnotified_analyst_mentions,
        mark_analyst_mentions_notified,
    )

    rows = await get_unnotified_analyst_mentions(db)
    fired: list[AlertRecord] = []
    notified_ids: list[int] = []

    for row in rows:
        netuid = row["netuid"]
        handle = row["analyst_handle"]
        tweet_text = row["tweet_text"] or ""
        text_preview = tweet_text[:120]
        if len(tweet_text) > 120:
            text_preview += "…"

        alert = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=netuid,
            subnet_name=_registry_name(registry, netuid),
            alert_type="analyst_mention",
            description=(
                f"@{handle} mentioned {_registry_name(registry, netuid)}: "
                f"\"{text_preview}\"\n→ {row['tweet_url']}"
            ),
            current_value=None,
            threshold=None,
        )
        in_cooldown = await is_alert_in_cooldown(
            db,
            netuid,
            "analyst_mention",
            config.ALERT_COOLDOWN_HOURS,
        )
        if not in_cooldown:
            await insert_alert(db, alert)
            fired.append(alert)
            logger.info("[ALERT] analyst_mention netuid=%d handle=%s", netuid, handle)
        notified_ids.append(row["id"])

    await mark_analyst_mentions_notified(db, notified_ids)
    return fired


async def fire_milestone_alerts(
    db: aiosqlite.Connection,
    registry: dict,
) -> list[AlertRecord]:
    from db.database import get_unnotified_milestones, mark_milestones_notified

    rows = await get_unnotified_milestones(db)
    fired: list[AlertRecord] = []
    notified_ids: list[int] = []

    for row in rows:
        netuid = row["netuid"]
        subnet_name = _registry_name(registry, netuid)
        type_emoji = "🔬" if row["milestone_type"] == "arxiv" else "🤗"

        desc_parts = [
            f"{type_emoji} {subnet_name} — new {row['milestone_type']}: {row['title']}",
        ]
        if row["ai_summary"]:
            desc_parts.append(f"Summary: {row['ai_summary']}")
        if row["ai_take"]:
            desc_parts.append(f"Take: {row['ai_take']}")
        desc_parts.append(f"→ {row['url']}")

        alert = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=netuid,
            subnet_name=subnet_name,
            alert_type="milestone",
            description="\n".join(desc_parts),
            current_value=None,
            threshold=None,
        )
        in_cooldown = await is_alert_in_cooldown(
            db,
            netuid,
            "milestone",
            config.ALERT_COOLDOWN_HOURS,
        )
        if not in_cooldown:
            await insert_alert(db, alert)
            fired.append(alert)
            logger.info(
                "[ALERT] milestone netuid=%d type=%s title=%r",
                netuid,
                row["milestone_type"],
                row["title"],
            )
        notified_ids.append(row["id"])

    await mark_milestones_notified(db, notified_ids)
    return fired


async def evaluate_convergence(
    db: aiosqlite.Connection,
    registry: dict,
) -> list[AlertRecord]:
    from db.database import get_recent_alert_types_per_netuid

    signals_by_netuid = await get_recent_alert_types_per_netuid(
        db,
        _CONVERGENCE_SIGNAL_TYPES,
        config.CONVERGENCE_SIGNAL_WINDOW_HOURS,
    )
    triggered = _count_convergence_signals(
        signals_by_netuid,
        config.CONVERGENCE_MIN_SIGNALS,
    )

    fired: list[AlertRecord] = []
    for netuid, signal_types in triggered.items():
        in_cooldown = await is_alert_in_cooldown(
            db,
            netuid,
            "convergence",
            config.CONVERGENCE_COOLDOWN_HOURS,
        )
        if in_cooldown:
            continue

        subnet_name = _registry_name(registry, netuid)
        logical_signal_count = len(_normalize_convergence_signal_types(signal_types))
        display_signal_types = _display_convergence_signal_types(signal_types)
        type_lines = "\n".join(
            f"  • {signal_type}" for signal_type in display_signal_types
        )
        alert = AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=netuid,
            subnet_name=subnet_name,
            alert_type="convergence",
            description=(
                f"HIGH CONVICTION — {subnet_name}\n"
                f"{logical_signal_count} signals converged in "
                f"{config.CONVERGENCE_SIGNAL_WINDOW_HOURS}h:\n"
                f"{type_lines}"
            ),
            current_value=float(logical_signal_count),
            threshold=float(config.CONVERGENCE_MIN_SIGNALS),
        )
        await insert_alert(db, alert)
        fired.append(alert)
        logger.info(
            "[ALERT] convergence netuid=%d signals=%s",
            netuid,
            sorted(signal_types),
        )

    return fired
