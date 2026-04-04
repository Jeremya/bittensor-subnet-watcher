import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from models import SubnetSnapshot
import config

logger = logging.getLogger(__name__)


def _raw_yield(snap: SubnetSnapshot) -> Optional[float]:
    """Annualized emission yield: (daily_emission_tao * tao_price * 365) / alpha_mcap_usd

    NOTE: daily_emission_tao reflects a subnet's current share of total TAO emissions,
    which is determined by net TAO inflows smoothed over an 86.8-day EMA. This means
    the yield is a lagged metric — it reflects capital flows from the past ~3 months,
    not the current flow direction. Use momentum (tao_in change) as the leading indicator.

    Returns None for micro-caps below YIELD_MIN_MCAP_USD — illiquid subnets produce
    extreme ratios that swamp min-max normalization.
    """
    if (snap.daily_emission_tao is None
            or snap.tao_usd_price is None
            or not snap.alpha_mcap_usd
            or snap.alpha_mcap_usd <= 0):
        return None
    if snap.alpha_mcap_usd < config.YIELD_MIN_MCAP_USD:
        return None
    return (snap.daily_emission_tao * snap.tao_usd_price * 365) / snap.alpha_mcap_usd


def compute_yield_scores(snapshots: list[SubnetSnapshot]) -> None:
    """
    Compute and set yield_score (0–100) on each snapshot in-place.
    Uses min-max normalization across all valid subnets.
    If all yields are identical (stddev=0), defaults all to 50.
    """
    raw: dict[int, float] = {}
    for snap in snapshots:
        r = _raw_yield(snap)
        if r is not None:
            raw[snap.netuid] = r

    if not raw:
        return

    min_r, max_r = min(raw.values()), max(raw.values())
    for snap in snapshots:
        r = raw.get(snap.netuid)
        if r is None:
            snap.yield_score = None
            continue
        if max_r == min_r:
            snap.yield_score = 50.0  # all identical
        else:
            snap.yield_score = (r - min_r) / (max_r - min_r) * 100.0


def compute_quality_score(snap: SubnetSnapshot,
                           max_neurons: int = 512) -> Optional[float]:
    """
    Quality score (0–100):
      GitHub recency: <30d = 40pts, <90d = 20pts, else 0
      n_neurons normalized to 0–60pts (relative to max_neurons)
    """
    score = 0.0
    now = datetime.now(timezone.utc)

    # GitHub recency (0–40 pts)
    if snap.gh_last_push is not None:
        age_days = (now - snap.gh_last_push).days
        if age_days < 30:
            score += 40.0
        elif age_days < 90:
            score += 20.0
        # else 0

    # n_neurons (0–60 pts)
    if snap.n_neurons is not None and max_neurons > 0:
        score += min(snap.n_neurons / max_neurons, 1.0) * 60.0

    # If no data at all, return None
    if snap.gh_last_push is None and snap.n_neurons is None:
        return None

    return round(score, 2)


def compute_momentum_score(snap: SubnetSnapshot,
                            history: list[SubnetSnapshot]) -> Optional[float]:
    """
    Momentum score (0–100) based on TAO inflow direction and emission rank trend.

    Subnet emission share is determined by net TAO flows (staking inflows minus
    outflows), smoothed over an 86.8-day EMA. alpha_mcap_tao is the cumulative
    TAO staked in the pool; its week-over-week change is the actual flow signal
    and the leading indicator of future emission share.

    Emission rank change is kept as a secondary lagged confirmation (+/- 15 pts)
    because it reflects EMA-smoothed flows from ~3 months prior.

    Returns None if no historical snapshot exists (new subnet).
    """
    if not history:
        return None

    # Find the oldest snapshot within ~7 days
    now = snap.polled_at or datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    past = [h for h in history if h.polled_at <= week_ago]
    if not past:
        past = history  # use whatever we have

    ref = past[-1]  # oldest available

    score = 50.0  # neutral baseline

    # Primary: TAO inflow change (+/- 35 pts)
    # alpha_mcap_tao = tao_in (TAO staked into the subnet pool).
    # Its percentage change is the net flow direction — the actual driver of
    # future emission share. This is the leading indicator.
    if snap.alpha_mcap_tao and ref.alpha_mcap_tao and ref.alpha_mcap_tao > 0:
        flow_change = (snap.alpha_mcap_tao - ref.alpha_mcap_tao) / ref.alpha_mcap_tao
        # +35 pts for +50% inflow growth, -35 pts for -50% outflow (capped)
        score += max(-35.0, min(35.0, flow_change * 70.0))

    # Secondary: emission rank change (+/- 15 pts)
    # Lagged confirmation — reflects EMA-smoothed flows from ~86.8 days ago.
    # Better rank = lower number = larger share of total emissions.
    if snap.emission_rank is not None and ref.emission_rank is not None:
        rank_improvement = ref.emission_rank - snap.emission_rank
        # +15 pts for improving 5 positions, -15 pts for losing 5 positions (capped)
        score += max(-15.0, min(15.0, rank_improvement * 3.0))

    return round(max(0.0, min(100.0, score)), 2)


def compute_hype_score(snap: SubnetSnapshot,
                        max_followers: int = 10000) -> Optional[float]:
    """
    Hype score (0–100) based on X/social presence:
      x_followers normalized to 0–60pts (relative to max across subnets)
      tweet recency: <3d = 40pts, <7d = 30pts, <14d = 20pts, <30d = 10pts, else 0
    Returns None if no social data at all.
    """
    if snap.x_followers is None and snap.x_last_tweet is None:
        return None

    score = 0.0
    now = datetime.now(timezone.utc)

    # Followers component (0–60 pts)
    if snap.x_followers is not None and max_followers > 0:
        score += min(snap.x_followers / max_followers, 1.0) * 60.0

    # Tweet recency component (0–40 pts)
    if snap.x_last_tweet is not None:
        age_days = (now - snap.x_last_tweet).days
        if age_days < 3:
            score += 40.0
        elif age_days < 7:
            score += 30.0
        elif age_days < 14:
            score += 20.0
        elif age_days < 30:
            score += 10.0

    return round(score, 2)


def score_snapshots(snapshots: list[SubnetSnapshot],
                    history_by_netuid: dict[int, list[SubnetSnapshot]]) -> None:
    """
    Compute and set all scores on snapshots in-place.
    history_by_netuid: {netuid: [older_snapshots]} for momentum calculation.
    """
    # Compute yield scores (requires cross-subnet normalization, done in batch)
    compute_yield_scores(snapshots)

    # Compute max neurons for quality normalization
    neurons = [s.n_neurons for s in snapshots if s.n_neurons is not None]
    max_neurons = max(neurons) if neurons else 512

    # Compute max followers for hype normalization
    followers = [s.x_followers for s in snapshots if s.x_followers is not None]
    max_followers = max(followers) if followers else 10000

    for snap in snapshots:
        snap.quality_score = compute_quality_score(snap, max_neurons=max_neurons)
        snap.momentum_score = compute_momentum_score(
            snap, history=history_by_netuid.get(snap.netuid, [])
        )
        snap.hype_score = compute_hype_score(snap, max_followers=max_followers)

        # Composite: weighted sum of available sub-scores
        parts = []
        if snap.yield_score is not None:
            parts.append((snap.yield_score, config.YIELD_WEIGHT))
        if snap.quality_score is not None:
            parts.append((snap.quality_score, config.QUALITY_WEIGHT))
        if snap.momentum_score is not None:
            parts.append((snap.momentum_score, config.MOMENTUM_WEIGHT))
        if snap.hype_score is not None:
            parts.append((snap.hype_score, config.HYPE_WEIGHT))

        if parts:
            total_weight = sum(w for _, w in parts)
            snap.composite_score = round(
                sum(s * w for s, w in parts) / total_weight, 2
            )
        else:
            snap.composite_score = None
