import aiosqlite
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
from models import SubnetSnapshot, AlertRecord
from db.database import is_alert_in_cooldown, insert_alert
import config

logger = logging.getLogger(__name__)


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


def check_social_silence(snap: SubnetSnapshot) -> Optional[AlertRecord]:
    if snap.x_last_tweet is None:
        return None
    age_days = (datetime.now(timezone.utc) - snap.x_last_tweet).days
    if age_days > config.SOCIAL_SILENCE_DAYS:
        return AlertRecord(
            fired_at=datetime.now(timezone.utc),
            netuid=snap.netuid,
            subnet_name=f"SN{snap.netuid}",
            alert_type="social_silence",
            description=f"No tweet in {age_days} days",
            current_value=float(age_days),
            threshold=float(config.SOCIAL_SILENCE_DAYS),
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


async def evaluate_alerts(
    db: aiosqlite.Connection,
    snapshots: list[SubnetSnapshot],
    registry: dict,
    prev_by_netuid: dict[int, SubnetSnapshot],
    known_netuids: set[int],
) -> list[AlertRecord]:
    """
    Evaluate all 6 alert conditions across all snapshots.
    Dedup via cooldown check. Persist new alerts to DB.
    Returns list of newly fired alerts.
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

        # 5. Social silence
        candidates.append(check_social_silence(snap))

        # 6. New entry
        candidates.append(check_new_entry(snap, known_netuids))

        # Dedup and persist
        for alert in candidates:
            if alert is None:
                continue
            # Set subnet name from registry
            alert.subnet_name = (registry.get(snap.netuid, {}).get("name")
                                 or f"SN{snap.netuid}")
            in_cooldown = await is_alert_in_cooldown(
                db, snap.netuid, alert.alert_type, config.ALERT_COOLDOWN_HOURS
            )
            if not in_cooldown:
                await insert_alert(db, alert)
                fired.append(alert)
                logger.info("[ALERT] netuid=%d type=%s value=%s threshold=%s",
                            alert.netuid, alert.alert_type,
                            alert.current_value, alert.threshold)

    return fired
