import pytest
from datetime import datetime, timezone, timedelta
from models import SubnetSnapshot
import config
from engine.scorer import (
    compute_yield_scores,
    compute_health_score,
    compute_momentum_score,
    compute_hype_score,
    score_snapshots,
)



def make_snap(netuid: int, **kwargs) -> SubnetSnapshot:
    return SubnetSnapshot(
        netuid=netuid,
        polled_at=datetime.now(timezone.utc),
        **kwargs,
    )


# ── Yield score ───────────────────────────────────────────────────────────────

def test_yield_scores_normalized_0_to_100():
    snaps = [
        make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=10_000_000, tao_usd_price=300.0),
        make_snap(2, daily_emission_tao=10.0, alpha_mcap_usd=10_000_000, tao_usd_price=300.0),
        make_snap(3, daily_emission_tao=1.0, alpha_mcap_usd=10_000_000, tao_usd_price=300.0),
    ]
    compute_yield_scores(snaps)
    scores = [s.yield_score for s in snaps]
    assert max(scores) == pytest.approx(100.0)
    assert min(scores) == pytest.approx(0.0)
    assert scores[0] > scores[1] > scores[2]


def test_yield_score_none_when_mcap_zero():
    snaps = [make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=0.0, tao_usd_price=300.0)]
    compute_yield_scores(snaps)
    assert snaps[0].yield_score is None


def test_yield_score_all_same_yields_50():
    snaps = [make_snap(i, daily_emission_tao=10.0, alpha_mcap_usd=1_000_000,
                       tao_usd_price=300.0) for i in range(3)]
    compute_yield_scores(snaps)
    for s in snaps:
        assert s.yield_score == pytest.approx(50.0)


def test_yield_score_none_when_missing_data():
    snaps = [make_snap(1, daily_emission_tao=None, alpha_mcap_usd=1_000_000,
                       tao_usd_price=300.0)]
    compute_yield_scores(snaps)
    assert snaps[0].yield_score is None


def test_yield_score_none_below_min_mcap():
    """Micro-caps are excluded to prevent them dominating min-max normalization."""
    snaps = [make_snap(1, daily_emission_tao=10.0, alpha_mcap_usd=10_000.0,
                       tao_usd_price=300.0)]
    compute_yield_scores(snaps)
    assert snaps[0].yield_score is None


# ── Health score ──────────────────────────────────────────────────────────────

def test_health_score_recent_github():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, gh_last_push=now - timedelta(days=10))
    score = compute_health_score(snap)
    # 30 pts github + 20 pts ownership (default 1) = 50
    assert score is not None
    assert score >= 30


def test_health_score_old_github():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, gh_last_push=now - timedelta(days=120))
    score = compute_health_score(snap)
    # 0 pts github + 20 pts ownership = 20
    assert score is not None
    assert score < 30


def test_health_score_no_github_gets_ownership_pts():
    snap = make_snap(1)
    score = compute_health_score(snap)
    # No github, no liquidity, owner_changes=1 → 20 pts
    assert score == pytest.approx(20.0)


def test_health_score_ownership_stable():
    snap = make_snap(1)
    assert compute_health_score(snap, owner_changes=1) == pytest.approx(20.0)


def test_health_score_ownership_two_owners():
    snap = make_snap(1)
    assert compute_health_score(snap, owner_changes=2) == pytest.approx(5.0)


def test_health_score_ownership_three_plus():
    snap = make_snap(1)
    assert compute_health_score(snap, owner_changes=3) == pytest.approx(0.0)


def test_health_score_reg_cost_rising():
    snap = make_snap(1, reg_cost_tao=0.12)
    # >10% rise from 0.10 → 20 pts reg + 20 pts ownership = 40
    assert compute_health_score(snap, prev_reg_cost=0.10) == pytest.approx(40.0)


def test_health_score_reg_cost_stable():
    snap = make_snap(1, reg_cost_tao=0.105)
    # ±10% stable → 10 pts reg + 20 pts ownership = 30
    assert compute_health_score(snap, prev_reg_cost=0.10) == pytest.approx(30.0)


def test_health_score_reg_cost_falling():
    snap = make_snap(1, reg_cost_tao=0.05)
    # >10% fall → 0 pts reg + 20 pts ownership = 20
    assert compute_health_score(snap, prev_reg_cost=0.10) == pytest.approx(20.0)


def test_health_score_liquidity_high():
    # volume_tao = 100 * 0.01 = 1.0; ratio = 1.0 / 10.0 = 0.10 > 0.05 → 30 pts; ownership → 20 pts
    snap = make_snap(1, volume_24h_alpha=100.0, alpha_price_tao=0.01, alpha_mcap_tao=10.0)
    score = compute_health_score(snap)
    assert score == pytest.approx(50.0)


def test_health_score_liquidity_medium():
    # volume_tao = 20 * 0.01 = 0.2; ratio = 0.2 / 10.0 = 0.02, >0.01 → 20 pts; ownership → 20 pts
    snap = make_snap(1, volume_24h_alpha=20.0, alpha_price_tao=0.01, alpha_mcap_tao=10.0)
    score = compute_health_score(snap)
    assert score == pytest.approx(40.0)


# ── Momentum score ────────────────────────────────────────────────────────────

def test_momentum_score_none_without_history():
    snap = make_snap(1, alpha_mcap_tao=1000.0, emission_rank=5)
    score = compute_momentum_score(snap, history=[])
    assert score is None


def test_momentum_score_with_history_fallback():
    """Uses crude tao_in delta when net_tao_flow_tao not in history (old rows)."""
    now = datetime.now(timezone.utc)
    current = make_snap(1, alpha_mcap_tao=1200.0, emission_rank=3)
    old = make_snap(1, alpha_mcap_tao=1000.0, emission_rank=8)
    old.polled_at = now - timedelta(days=7)
    score = compute_momentum_score(current, history=[old])
    assert score is not None
    assert 0 <= score <= 100


def test_momentum_score_uses_net_flow_when_available():
    """When net_tao_flow_tao is populated, it drives the primary signal."""
    now = datetime.now(timezone.utc)
    current = make_snap(1, alpha_mcap_tao=1000.0, emission_rank=5)
    # Two recent history snapshots with emission-adjusted flow data
    h1 = make_snap(1, alpha_mcap_tao=980.0, emission_rank=6, net_tao_flow_tao=50.0)
    h1.polled_at = now - timedelta(hours=1)
    h2 = make_snap(1, alpha_mcap_tao=950.0, emission_rank=6, net_tao_flow_tao=30.0)
    h2.polled_at = now - timedelta(hours=2)
    score_inflow = compute_momentum_score(current, history=[h1, h2])

    # Negative flow should produce lower score
    h1_out = make_snap(1, alpha_mcap_tao=980.0, emission_rank=6, net_tao_flow_tao=-50.0)
    h1_out.polled_at = now - timedelta(hours=1)
    h2_out = make_snap(1, alpha_mcap_tao=950.0, emission_rank=6, net_tao_flow_tao=-30.0)
    h2_out.polled_at = now - timedelta(hours=2)
    score_outflow = compute_momentum_score(current, history=[h1_out, h2_out])

    assert score_inflow is not None
    assert score_outflow is not None
    assert score_inflow > score_outflow


def test_momentum_score_anchor_is_order_independent():
    """Descending DB history should still anchor to the newest row around the cutoff."""
    now = datetime.now(timezone.utc)
    current = make_snap(1, alpha_mcap_tao=1000.0, emission_rank=5)

    near_cutoff = make_snap(
        1,
        alpha_mcap_tao=995.0,
        emission_rank=6,
        net_tao_flow_tao=10.0,
    )
    near_cutoff.polled_at = now - timedelta(days=8)
    older = make_snap(
        1,
        alpha_mcap_tao=940.0,
        emission_rank=12,
        net_tao_flow_tao=50.0,
    )
    older.polled_at = now - timedelta(days=10)

    score_desc = compute_momentum_score(current, history=[near_cutoff, older])
    score_asc = compute_momentum_score(current, history=[older, near_cutoff])

    assert score_desc is not None
    assert score_asc is not None
    assert score_desc == pytest.approx(score_asc)


# ── score_snapshots ──────────────────────────────────────────────────────────

def test_score_snapshots_sets_composite():
    snaps = [
        make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=5_000_000,
                  tao_usd_price=300.0,
                  gh_last_push=datetime.now(timezone.utc) - timedelta(days=5)),
        make_snap(2, daily_emission_tao=5.0, alpha_mcap_usd=10_000_000,
                  tao_usd_price=300.0),
    ]
    score_snapshots(snaps, history_by_netuid={})
    for s in snaps:
        assert s.composite_score is not None
        assert 0 <= s.composite_score <= 100


def test_score_snapshots_weight_renormalization():
    """When history is missing, swing score uses available value/tradability components."""
    snap = make_snap(
        1,
        daily_emission_tao=50.0,
        alpha_mcap_usd=5_000_000,
        tao_usd_price=300.0,
        gh_last_push=datetime.now(timezone.utc) - timedelta(days=5),
        # No history → flow component is unavailable.
    )
    score_snapshots([snap], history_by_netuid={})
    assert snap.yield_score is not None
    assert snap.momentum_score == snap.composite_score
    assert snap.composite_score == pytest.approx(snap.yield_score, rel=0.01)


def test_hype_score_not_included_in_composite():
    """Hype is computed for display but must not affect composite score."""
    now = datetime.now(timezone.utc)
    base = make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=5_000_000,
                     tao_usd_price=300.0,
                     gh_last_push=now - timedelta(days=5))
    with_hype = make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=5_000_000,
                          tao_usd_price=300.0,
                          gh_last_push=now - timedelta(days=5),
                          x_followers=50000, x_last_tweet=now - timedelta(days=1))
    score_snapshots([base], history_by_netuid={})
    score_snapshots([with_hype], history_by_netuid={})
    assert with_hype.hype_score is not None
    assert base.composite_score == pytest.approx(with_hype.composite_score, rel=0.01)


def test_score_snapshots_forwards_alert_context_to_swing():
    """score_snapshots() must forward alert/coverage context to compute_swing_signal()."""
    snap_without = make_snap(
        1,
        daily_emission_tao=20.0,
        alpha_mcap_usd=600_000,
        tao_usd_price=300.0,
        volume_24h_alpha=50_000.0,
        alpha_price_tao=0.002,
        alpha_mcap_tao=1_000.0,
    )
    hist_snap = SubnetSnapshot(
        netuid=1,
        polled_at=datetime.now(timezone.utc) - timedelta(hours=6),
        net_tao_flow_tao=10.0,
    )
    hist = [hist_snap]
    score_snapshots([snap_without], {1: hist})
    score_without = snap_without.composite_score

    snap_with = make_snap(
        1,
        daily_emission_tao=20.0,
        alpha_mcap_usd=600_000,
        tao_usd_price=300.0,
        volume_24h_alpha=50_000.0,
        alpha_price_tao=0.002,
        alpha_mcap_tao=1_000.0,
    )
    score_snapshots(
        [snap_with],
        {1: hist},
        alert_types_by_netuid={1: {"convergence"}},
        coverage_netuids={1},
        milestone_netuids={1},
    )
    score_with = snap_with.composite_score

    assert score_with > score_without
    assert snap_with.momentum_score == snap_with.composite_score
    assert snap_with.yield_score is not None
    assert snap_with.health_score is not None


def test_score_snapshots_accepts_explicit_swing_context_kwargs():
    """The scorer keeps the expanded swing-context signature for planned callers."""
    snap = make_snap(1, daily_emission_tao=20.0, alpha_mcap_usd=600_000, tao_usd_price=300.0)
    hist_snap = SubnetSnapshot(
        netuid=1,
        polled_at=datetime.now(timezone.utc) - timedelta(hours=6),
        net_tao_flow_tao=10.0,
    )

    score_snapshots(
        [snap],
        {1: [hist_snap]},
        alert_types_by_netuid={1: {"analyst_mention"}},
        coverage_netuids={1},
        milestone_netuids=set(),
        owner_changes_by_netuid={1: 1},
        reg_cost_7d_by_netuid={1: 0.10},
    )

    assert snap.composite_score is not None
    assert snap.momentum_score == snap.composite_score


def test_score_snapshots_populates_explicit_signal_fields():
    snap = make_snap(
        1,
        daily_emission_tao=20.0,
        alpha_mcap_usd=600_000,
        tao_usd_price=300.0,
        volume_24h_alpha=50_000.0,
        alpha_price_tao=0.002,
        alpha_mcap_tao=1_000.0,
    )
    hist_snap = SubnetSnapshot(
        netuid=1,
        polled_at=datetime.now(timezone.utc) - timedelta(hours=6),
        net_tao_flow_tao=10.0,
        alpha_mcap_tao=1_000.0,
        emission_rank=9,
    )

    score_snapshots(
        [snap],
        {1: [hist_snap]},
        alert_types_by_netuid={1: {"convergence", "analyst_mention"}},
        coverage_netuids={1},
        milestone_netuids={1},
    )

    assert snap.flow_score is not None
    assert snap.relative_value_score is not None
    assert snap.tradability_score is not None
    assert snap.catalyst_score is not None
    assert snap.risk_penalty is not None
    assert snap.swing_score is not None
    assert snap.composite_score == snap.swing_score
    assert snap.momentum_score == snap.swing_score


# ── Hype score ────────────────────────────────────────────────────────────────

def test_hype_score_none_without_social_data():
    snap = make_snap(1)
    assert compute_hype_score(snap) is None


def test_hype_score_followers_only():
    snap = make_snap(1, x_followers=5000)
    score = compute_hype_score(snap, max_followers=10000)
    assert score == pytest.approx(30.0)  # 5000/10000 * 60 = 30


def test_hype_score_recent_tweet_bonus():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, x_followers=0, x_last_tweet=now - timedelta(days=1))
    score = compute_hype_score(snap, max_followers=10000)
    assert score == pytest.approx(40.0)  # 0 followers + <3d tweet = 40


def test_hype_score_capped_at_100():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, x_followers=10000, x_last_tweet=now - timedelta(days=1))
    score = compute_hype_score(snap, max_followers=10000)
    assert score == pytest.approx(100.0)  # 60 + 40 = 100


def test_hype_score_stale_tweet_no_bonus():
    snap = make_snap(1, x_followers=1000, x_last_tweet=datetime.now(timezone.utc) - timedelta(days=60))
    score = compute_hype_score(snap, max_followers=10000)
    assert score == pytest.approx(6.0)   # 1000/10000 * 60 = 6, tweet >30d = 0


def test_score_snapshots_composite_is_swing_score_from_flow_value_and_tradability():
    now = datetime.now(timezone.utc)
    snap = make_snap(
        1,
        daily_emission_tao=50.0,
        alpha_mcap_usd=500_000.0,
        tao_usd_price=300.0,
        alpha_mcap_tao=1_000.0,
        tao_in_tao=1_000.0,
        volume_24h_alpha=50_000.0,
        alpha_price_tao=0.002,
        emission_rank=4,
    )
    hist = [
        make_snap(
            1,
            alpha_mcap_tao=1_000.0,
            tao_in_tao=1_000.0,
            net_tao_flow_tao=30.0,
            emission_rank=8,
        )
    ]
    hist[0].polled_at = now - timedelta(hours=4)

    score_snapshots(
        [snap],
        history_by_netuid={1: hist},
        alert_types_by_netuid={1: {"convergence"}},
        coverage_netuids={1},
        milestone_netuids={1},
    )

    assert snap.composite_score is not None
    assert snap.composite_score > 60.0
    assert snap.momentum_score == snap.composite_score
    assert snap.swing_score == snap.composite_score
    assert snap.flow_score is not None
    assert snap.relative_value_score is not None
    assert snap.tradability_score is not None
    assert snap.catalyst_score is not None
    assert snap.risk_penalty is not None
