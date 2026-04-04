import pytest
from datetime import datetime, timezone, timedelta
from models import SubnetSnapshot
import config
from engine.scorer import (
    compute_yield_scores,
    compute_quality_score,
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


# ── Quality score ─────────────────────────────────────────────────────────────

def test_quality_score_recent_github():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, gh_last_push=now - timedelta(days=10), n_neurons=256)
    score = compute_quality_score(snap, max_neurons=512)
    assert score is not None
    assert score >= 30  # recent push gives 30 pts


def test_quality_score_old_github():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, gh_last_push=now - timedelta(days=120), n_neurons=256)
    score = compute_quality_score(snap, max_neurons=512)
    assert score is not None
    assert score < 30  # no points for old push


def test_quality_score_none_github_gives_partial():
    snap = make_snap(1, gh_last_push=None, n_neurons=256)
    score = compute_quality_score(snap, max_neurons=512)
    assert score is not None  # still gets fill ratio score


def test_quality_score_fill_ratio_uses_max_allowed_uids():
    snap = make_snap(1, n_neurons=128, max_allowed_uids=256)
    score = compute_quality_score(snap)
    # fill = 128/256 = 0.5 → 15 pts; no github, no liquidity
    assert score == pytest.approx(15.0)


def test_quality_score_liquidity_high():
    snap = make_snap(1, volume_24h_alpha=100.0, alpha_mcap_tao=1000.0)
    # ratio = 0.10 > 0.05 → 40 pts; no github, no neurons
    score = compute_quality_score(snap)
    assert score == pytest.approx(40.0)


def test_quality_score_liquidity_medium():
    snap = make_snap(1, volume_24h_alpha=20.0, alpha_mcap_tao=1000.0)
    # ratio = 0.02, between 0.01 and 0.05 → 25 pts
    score = compute_quality_score(snap)
    assert score == pytest.approx(25.0)


def test_quality_score_none_when_all_absent():
    snap = make_snap(1)
    assert compute_quality_score(snap) is None


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


# ── score_snapshots ──────────────────────────────────────────────────────────

def test_score_snapshots_sets_composite():
    snaps = [
        make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=5_000_000,
                  tao_usd_price=300.0, n_neurons=200,
                  gh_last_push=datetime.now(timezone.utc) - timedelta(days=5)),
        make_snap(2, daily_emission_tao=5.0, alpha_mcap_usd=10_000_000,
                  tao_usd_price=300.0, n_neurons=50),
    ]
    score_snapshots(snaps, history_by_netuid={})
    for s in snaps:
        assert s.composite_score is not None
        assert 0 <= s.composite_score <= 100


def test_score_snapshots_weight_renormalization():
    """When momentum_score is None, composite uses renormalized yield+quality weights."""
    snap = make_snap(
        1,
        daily_emission_tao=50.0,
        alpha_mcap_usd=5_000_000,
        tao_usd_price=300.0,
        n_neurons=200,
        gh_last_push=datetime.now(timezone.utc) - timedelta(days=5),
        # No history → momentum_score will be None
    )
    score_snapshots([snap], history_by_netuid={})
    assert snap.momentum_score is None
    assert snap.yield_score is not None
    assert snap.quality_score is not None
    w_y, w_q = config.YIELD_WEIGHT, config.QUALITY_WEIGHT
    expected = (snap.yield_score * w_y + snap.quality_score * w_q) / (w_y + w_q)
    assert snap.composite_score == pytest.approx(expected, rel=0.01)


def test_hype_score_not_included_in_composite():
    """Hype is computed for display but must not affect composite score."""
    now = datetime.now(timezone.utc)
    base = make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=5_000_000,
                     tao_usd_price=300.0, n_neurons=200,
                     gh_last_push=now - timedelta(days=5))
    with_hype = make_snap(1, daily_emission_tao=50.0, alpha_mcap_usd=5_000_000,
                          tao_usd_price=300.0, n_neurons=200,
                          gh_last_push=now - timedelta(days=5),
                          x_followers=50000, x_last_tweet=now - timedelta(days=1))
    score_snapshots([base], history_by_netuid={})
    score_snapshots([with_hype], history_by_netuid={})
    assert with_hype.hype_score is not None
    assert base.composite_score == pytest.approx(with_hype.composite_score, rel=0.01)


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
