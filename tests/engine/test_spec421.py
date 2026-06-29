from datetime import datetime, timedelta, timezone

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
