import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from models import SubnetSnapshot
import config
from engine.signals import (
    compute_relative_value_scores,
    compute_swing_signal,
    compute_tradability_score,
)

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


def compute_health_score(snap: SubnetSnapshot,
                          owner_changes: int = 1,
                          prev_reg_cost: Optional[float] = None) -> float:
    """
    Health score (0–100) — protocol-native subnet health signals:

      GitHub recency (0–30 pts): team is actively shipping
        <30d = 30pts, <90d = 15pts, else 0

      Ownership stability (0–20 pts): fewer distinct owners = more stable
        1 distinct owner in past 30d = 20pts, 2 = 5pts, 3+ = 0

      Reg cost trend (0–20 pts): rising registration demand = healthy competition
        rising >10% vs 7d ago = 20pts, stable ±10% = 10pts, falling = 0

      Liquidity depth (0–30 pts): (volume_24h_alpha * alpha_price_tao) / alpha_mcap_tao
        Measures daily turnover of the TAO pool — can an investor actually exit?
        >5% daily turnover = 30pts, >1% = 20pts, >0.1% = 8pts, else 0

    Always returns a score since ownership stability is always computable.
    """
    score = 0.0
    now = datetime.now(timezone.utc)

    # GitHub recency (0–30 pts)
    if snap.gh_last_push is not None and isinstance(snap.gh_last_push, datetime):
        age_days = (now - snap.gh_last_push).days
        if age_days < 30:
            score += 30.0
        elif age_days < 90:
            score += 15.0

    # Ownership stability (0–20 pts)
    if owner_changes <= 1:
        score += 20.0
    elif owner_changes == 2:
        score += 5.0
    # 3+ owners = 0 pts

    # Reg cost trend (0–20 pts)
    if (snap.reg_cost_tao is not None
            and prev_reg_cost is not None and prev_reg_cost > 0):
        pct_change = (snap.reg_cost_tao - prev_reg_cost) / prev_reg_cost
        if pct_change > 0.10:
            score += 20.0
        elif pct_change >= -0.10:
            score += 10.0
        # falling below -10% = 0 pts

    # Liquidity depth (0–30 pts): daily volume (converted to TAO) / true market cap
    if (snap.volume_24h_alpha is not None
            and snap.alpha_price_tao is not None
            and snap.alpha_mcap_tao and snap.alpha_mcap_tao > 0):
        ratio = (snap.volume_24h_alpha * snap.alpha_price_tao) / snap.alpha_mcap_tao
        if ratio > 0.05:
            score += 30.0
        elif ratio > 0.01:
            score += 20.0
        elif ratio > 0.001:
            score += 8.0

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

    # Primary: cumulative net TAO staking flow over the observation window (+/- 35 pts)
    # net_tao_flow_tao is stored per-poll as Δ(tao_in) − emission_accrual,
    # so summing it gives total pure staking inflows/outflows with emissions stripped out.
    # Normalised by current pool size for scale-invariant comparison across subnets.
    net_flows = [h.net_tao_flow_tao for h in history
                 if h.net_tao_flow_tao is not None and h.polled_at >= week_ago]
    if net_flows and snap.alpha_mcap_tao and snap.alpha_mcap_tao > 0:
        total_net_flow = sum(net_flows)
        flow_rate = total_net_flow / snap.alpha_mcap_tao
        # +35 pts for net inflow = 50% of pool over window, -35 for -50%
        score += max(-35.0, min(35.0, flow_rate * 70.0))
    elif snap.alpha_mcap_tao and ref.alpha_mcap_tao and ref.alpha_mcap_tao > 0:
        # Fallback: crude reserve delta (mixes emission accrual + staking flows).
        # Used for old snapshots before net_tao_flow_tao was collected.
        flow_change = (snap.alpha_mcap_tao - ref.alpha_mcap_tao) / ref.alpha_mcap_tao
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


def score_snapshots(
    snapshots: list[SubnetSnapshot],
    history_by_netuid: dict[int, list[SubnetSnapshot]],
    alert_types_by_netuid: Optional[dict[int, set[str]]] = None,
    coverage_netuids: Optional[set[int]] = None,
    milestone_netuids: Optional[set[int]] = None,
) -> None:
    """
    Compute and set all scores on snapshots in-place.
    history_by_netuid: {netuid: [older_snapshots]} for momentum calculation.
    alert_types_by_netuid: {netuid: set of recent alert type strings} for swing signal boost.
    coverage_netuids: set of netuids with active analyst coverage.
    milestone_netuids: set of netuids with recent milestones.
    """
    # Compute max followers for hype normalization
    followers = [s.x_followers for s in snapshots if s.x_followers is not None]
    max_followers = max(followers) if followers else 10000
    relative_value_by_netuid = compute_relative_value_scores(snapshots)

    for snap in snapshots:
        relative_value = relative_value_by_netuid.get(snap.netuid)
        if relative_value is None:
            continue
        swing = compute_swing_signal(
            snap,
            history=history_by_netuid.get(snap.netuid, []),
            relative_value=relative_value,
            alert_types=(alert_types_by_netuid or {}).get(snap.netuid, set()),
            covered=(coverage_netuids is not None and snap.netuid in coverage_netuids),
            has_milestone=(milestone_netuids is not None and snap.netuid in milestone_netuids),
        )
        tradability = compute_tradability_score(snap)

        snap.yield_score = relative_value.score
        snap.health_score = tradability.score
        snap.momentum_score = swing.swing_score
        # Hype is computed for display but intentionally excluded from composite —
        # it is gameable (purchased followers, low-effort tweets) and protocol-external.
        snap.hype_score = compute_hype_score(snap, max_followers=max_followers)
        snap.composite_score = swing.swing_score
