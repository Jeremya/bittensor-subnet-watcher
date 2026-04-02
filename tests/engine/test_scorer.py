import pytest
from datetime import datetime, timezone, timedelta
from models import SubnetSnapshot
from engine.scorer import (
    compute_yield_scores,
    compute_quality_score,
    compute_momentum_score,
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


# ── Quality score ─────────────────────────────────────────────────────────────

def test_quality_score_recent_github():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, gh_last_push=now - timedelta(days=10), n_neurons=256)
    score = compute_quality_score(snap, max_neurons=512)
    assert score is not None
    assert score >= 40  # recent push gives 40 pts


def test_quality_score_old_github():
    now = datetime.now(timezone.utc)
    snap = make_snap(1, gh_last_push=now - timedelta(days=120), n_neurons=256)
    score = compute_quality_score(snap, max_neurons=512)
    assert score is not None
    assert score < 40  # no points for old push


def test_quality_score_none_github_gives_partial():
    snap = make_snap(1, gh_last_push=None, n_neurons=256)
    score = compute_quality_score(snap, max_neurons=512)
    assert score is not None  # still gets neurons score


# ── Momentum score ────────────────────────────────────────────────────────────

def test_momentum_score_none_without_history():
    snap = make_snap(1, alpha_mcap_tao=1000.0, emission_rank=5)
    score = compute_momentum_score(snap, history=[])
    assert score is None


def test_momentum_score_with_history():
    now = datetime.now(timezone.utc)
    current = make_snap(1, alpha_mcap_tao=1200.0, emission_rank=3)
    old = make_snap(1, alpha_mcap_tao=1000.0, emission_rank=8)
    old.polled_at = now - timedelta(days=7)
    score = compute_momentum_score(current, history=[old])
    assert score is not None
    assert 0 <= score <= 100


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
