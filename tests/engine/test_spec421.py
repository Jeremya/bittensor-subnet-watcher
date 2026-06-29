from datetime import datetime, timedelta, timezone

import pytest

from engine.spec421 import (
    compute_emission_value_scores,
    compute_price_ema_score,
    compute_protocol_context_score,
    compute_spec421_signals,
)
from models import SubnetSnapshot


def make_snap(netuid: int = 1, **overrides) -> SubnetSnapshot:
    data = {
        "netuid": netuid,
        "polled_at": datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc),
        "alpha_price_tao": 1.0,
        "alpha_mcap_tao": 1_000.0,
        "alpha_mcap_usd": 300_000.0,
        "daily_emission_tao": 10.0,
        "tao_usd_price": 300.0,
        "emission_rank": 10,
        "emergence_stage": "maturing",
        "emergence_score": 45.0,
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def price_history(values: list[float]) -> list[SubnetSnapshot]:
    now = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
    rows: list[SubnetSnapshot] = []
    for i, price in enumerate(values):
        rows.append(
            make_snap(
                polled_at=now - timedelta(hours=(len(values) - i)),
                alpha_price_tao=price,
            )
        )
    return rows


def test_price_ema_score_rewards_price_above_slow_ema():
    current = make_snap(alpha_price_tao=1.35)
    rising = compute_price_ema_score(current, price_history([1.0, 1.05, 1.1, 1.2]))
    flat = compute_price_ema_score(current, price_history([1.35, 1.35, 1.35, 1.35]))

    assert rising.score is not None
    assert flat.score is not None
    assert rising.score > flat.score
    assert rising.is_positive
    assert "price above EMA proxy" in rising.reasons


def test_price_ema_score_penalizes_price_below_slow_ema():
    current = make_snap(alpha_price_tao=0.70)
    signal = compute_price_ema_score(current, price_history([1.0, 0.95, 0.90, 0.82]))

    assert signal.score is not None
    assert signal.score < 50.0
    assert signal.is_negative
    assert "price below EMA proxy" in signal.risks


def test_price_ema_score_missing_history_is_unavailable():
    current = make_snap(alpha_price_tao=1.1)
    signal = compute_price_ema_score(current, [])

    assert signal.score is None
    assert "insufficient price history" in signal.notes


def test_price_ema_score_invalid_current_price_is_unavailable():
    for invalid_price in (None, 0.0, -0.1):
        current = make_snap(alpha_price_tao=invalid_price)
        signal = compute_price_ema_score(current, price_history([1.0, 1.1, 1.2, 1.3]))

        assert signal.score is None
        assert "invalid current alpha price" in signal.notes


def test_emission_value_scores_reward_emission_discount_under_price_based_model():
    cheap = make_snap(
        netuid=1,
        daily_emission_tao=25.0,
        alpha_mcap_usd=300_000.0,
        emission_rank=5,
    )
    rich = make_snap(
        netuid=2,
        daily_emission_tao=5.0,
        alpha_mcap_usd=3_000_000.0,
        emission_rank=40,
    )

    scores = compute_emission_value_scores([cheap, rich])

    assert scores[1].score is not None
    assert scores[2].score is not None
    assert scores[1].score > scores[2].score
    assert "price-based emission value versus market cap" in scores[1].reasons


def test_emission_value_scores_suppress_micro_caps():
    micro = make_snap(netuid=1, alpha_mcap_usd=10_000.0, daily_emission_tao=50.0)

    scores = compute_emission_value_scores([micro])

    assert scores[1].score is None
    assert "below emission-value market-cap floor" in scores[1].notes


def test_emission_value_scores_use_latest_duplicate_snapshot_independent_of_order():
    older = make_snap(
        netuid=1,
        polled_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
        daily_emission_tao=100.0,
        alpha_mcap_usd=100_000.0,
        alpha_mcap_tao=10_000.0,
        emission_rank=1,
    )
    newer = make_snap(
        netuid=1,
        polled_at=datetime(2026, 6, 29, 11, 0, tzinfo=timezone.utc),
        daily_emission_tao=1.0,
        alpha_mcap_usd=4_000_000.0,
        alpha_mcap_tao=200.0,
        emission_rank=50,
    )
    peer = make_snap(
        netuid=2,
        daily_emission_tao=25.0,
        alpha_mcap_usd=250_000.0,
        alpha_mcap_tao=1_000.0,
        emission_rank=5,
    )

    first = compute_emission_value_scores([older, newer, peer])
    second = compute_emission_value_scores([peer, older, newer])

    assert first[1] == second[1]
    assert first[1].score < first[2].score


def test_emission_value_scores_tied_duplicate_conflict_raises():
    first = make_snap(netuid=1, daily_emission_tao=100.0, alpha_mcap_usd=100_000.0)
    second = make_snap(netuid=1, daily_emission_tao=1.0, alpha_mcap_usd=4_000_000.0)
    peer = make_snap(netuid=2, daily_emission_tao=25.0, alpha_mcap_usd=250_000.0)

    with pytest.raises(ValueError, match="ambiguous duplicate Spec 421 snapshot for netuid 1"):
        compute_emission_value_scores([second, first, peer])


def test_emission_value_scores_tied_identical_duplicates_are_accepted():
    first = make_snap(netuid=1)
    second = make_snap(netuid=1)
    peer = make_snap(netuid=2, daily_emission_tao=25.0, alpha_mcap_usd=250_000.0)

    scores = compute_emission_value_scores([second, first, peer])

    assert scores[1].score is not None
    assert scores[2].score is not None


def test_emission_value_scores_reject_non_positive_emission_inputs():
    for overrides in (
        {"daily_emission_tao": 0.0},
        {"daily_emission_tao": -1.0},
        {"tao_usd_price": 0.0},
        {"tao_usd_price": -300.0},
    ):
        snap = make_snap(**overrides)

        scores = compute_emission_value_scores([snap])

        assert scores[1].score is None
        assert "missing emission-value data" in scores[1].notes


def test_emission_value_scores_exclude_non_positive_tao_mcap_rank_contribution():
    for alpha_mcap_tao in (0.0, -1.0):
        snap = make_snap(
            alpha_mcap_tao=alpha_mcap_tao,
            daily_emission_tao=25.0,
            alpha_mcap_usd=300_000.0,
            emission_rank=1,
        )

        scores = compute_emission_value_scores([snap])

        assert scores[1].score == 50.0
        assert "price-based emission rank discounted by market cap" not in scores[1].reasons


def test_protocol_context_does_not_fake_uncollected_exact_factors():
    current = make_snap(emergence_stage="nascent", emergence_score=76.0)

    signal = compute_protocol_context_score(current)

    assert signal.score is not None
    assert signal.is_positive
    assert "newer subnet may benefit from root-proportion weighting" in signal.reasons
    assert "exact root_proportion not collected" in signal.notes
    assert "exact miner_burned not collected" in signal.notes


def test_spec421_signal_combines_available_components_and_notes_missing_factors():
    current = make_snap(alpha_price_tao=1.25, emergence_stage="nascent", emergence_score=75.0)
    peer = make_snap(netuid=2, alpha_price_tao=0.9, alpha_mcap_usd=2_000_000.0, daily_emission_tao=3.0)

    signals = compute_spec421_signals(
        [current, peer],
        {1: price_history([1.0, 1.03, 1.08, 1.15]), 2: price_history([1.0, 0.98, 0.95, 0.92])},
    )

    signal = signals[1]
    assert signal.spec421_score > signals[2].spec421_score
    assert signal.price_ema.score is not None
    assert signal.emission_value.score is not None
    assert signal.protocol_context.score is not None
    assert "Spec 421 score uses EMA-price proxy, not exact SubnetMovingPrice" in signal.notes


def test_spec421_signals_use_latest_duplicate_snapshot_independent_of_order():
    older = make_snap(
        netuid=1,
        polled_at=datetime(2026, 6, 29, 10, 0, tzinfo=timezone.utc),
        alpha_price_tao=1.5,
        daily_emission_tao=100.0,
        alpha_mcap_usd=100_000.0,
    )
    newer = make_snap(
        netuid=1,
        polled_at=datetime(2026, 6, 29, 11, 0, tzinfo=timezone.utc),
        alpha_price_tao=0.7,
        daily_emission_tao=1.0,
        alpha_mcap_usd=4_000_000.0,
    )
    peer = make_snap(
        netuid=2,
        alpha_price_tao=1.1,
        daily_emission_tao=25.0,
        alpha_mcap_usd=250_000.0,
    )
    history = {
        1: price_history([1.0, 0.95, 0.9, 0.82]),
        2: price_history([1.0, 1.05, 1.08, 1.1]),
    }

    first = compute_spec421_signals([older, newer, peer], history)
    second = compute_spec421_signals([peer, older, newer], history)

    assert first[1] == second[1]
    assert first[1].price_ema.is_negative


def test_spec421_signals_tied_duplicate_conflict_raises():
    first = make_snap(netuid=1, alpha_price_tao=1.4)
    second = make_snap(netuid=1, alpha_price_tao=0.7)

    with pytest.raises(ValueError, match="ambiguous duplicate Spec 421 snapshot for netuid 1"):
        compute_spec421_signals([first, second], {1: price_history([1.0, 0.95, 0.9, 0.82])})
