from datetime import datetime, timedelta, timezone

from engine.signals import (
    SwingSignal,
    compute_catalyst_score,
    compute_flow_score,
    compute_relative_value_scores,
    compute_risk_penalty,
    compute_swing_signal,
    compute_tradability_score,
)
from models import SubnetSnapshot


def make_snap(netuid: int = 1, **overrides) -> SubnetSnapshot:
    data = {
        "netuid": netuid,
        "polled_at": datetime.now(timezone.utc),
        "alpha_mcap_tao": 1_000.0,
        "alpha_mcap_usd": 300_000.0,
        "tao_in_tao": 1_000.0,
        "daily_emission_tao": 10.0,
        "tao_usd_price": 300.0,
        "emission_rank": 10,
        "volume_24h_alpha": 50_000.0,
        "alpha_price_tao": 0.002,
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def history_flow(values: list[float], *, hours_step: int = 6) -> list[SubnetSnapshot]:
    now = datetime.now(timezone.utc)
    rows = []
    for i, value in enumerate(values, start=1):
        rows.append(
            make_snap(
                polled_at=now - timedelta(hours=i * hours_step),
                net_tao_flow_tao=value,
                alpha_mcap_tao=1_000.0,
                emission_rank=12,
            )
        )
    return rows


def test_flow_score_rewards_persistent_recent_inflow():
    current = make_snap(emission_rank=8)
    positive = compute_flow_score(current, history_flow([20.0, 15.0, 10.0, 8.0]))
    flat = compute_flow_score(current, history_flow([0.0, 0.0, 0.0, 0.0]))

    assert positive.score > flat.score
    assert positive.is_positive
    assert "positive net TAO flow" in positive.reasons


def test_flow_score_penalizes_negative_flow_faster_than_positive():
    current = make_snap()
    positive = compute_flow_score(current, history_flow([20.0, 20.0]))
    negative = compute_flow_score(current, history_flow([-20.0, -20.0]))

    assert positive.score - 50.0 < 50.0 - negative.score
    assert negative.is_negative
    assert "sustained net TAO outflow" in negative.risks


def test_relative_value_scores_reward_emission_discount():
    cheap = make_snap(
        netuid=1, daily_emission_tao=20.0, alpha_mcap_usd=300_000.0, emission_rank=5
    )
    rich = make_snap(
        netuid=2, daily_emission_tao=5.0, alpha_mcap_usd=3_000_000.0, emission_rank=40
    )

    scores = compute_relative_value_scores([cheap, rich])

    assert scores[1].score > scores[2].score
    assert "cheap emissions versus market cap" in scores[1].reasons


def test_tradability_score_blocks_illiquid_subnet():
    liquid = compute_tradability_score(
        make_snap(volume_24h_alpha=50_000.0, alpha_price_tao=0.002, alpha_mcap_tao=1_000.0)
    )
    illiquid = compute_tradability_score(
        make_snap(volume_24h_alpha=1.0, alpha_price_tao=0.001, alpha_mcap_tao=100_000.0)
    )

    assert liquid.score > illiquid.score
    assert illiquid.blocks_new_buy
    assert "liquidity below swing threshold" in illiquid.risks


def test_tradability_score_uses_slippage_when_available():
    tight = compute_tradability_score(
        make_snap(
            buy_slippage_pct=0.75,
            sell_slippage_pct=0.9,
            volume_24h_alpha=10_000.0,
            alpha_price_tao=0.01,
            alpha_mcap_tao=100_000.0,
        )
    )
    wide = compute_tradability_score(
        make_snap(
            buy_slippage_pct=9.5,
            sell_slippage_pct=12.0,
            volume_24h_alpha=10_000.0,
            alpha_price_tao=0.01,
            alpha_mcap_tao=100_000.0,
        )
    )

    assert tight.score > wide.score
    assert "low slippage on 5 TAO swing trade" in tight.reasons
    assert wide.blocks_new_buy
    assert any("slippage above" in risk for risk in wide.risks)


def test_catalyst_score_weights_convergence_highest():
    score = compute_catalyst_score(
        {"convergence", "analyst_mention", "github_spike"},
        covered=True,
        has_milestone=False,
    )

    assert score.score >= 80.0
    assert score.is_strong
    assert "fresh convergence catalyst" in score.reasons


def test_risk_penalty_severe_risk_blocks_new_exposure():
    penalty = compute_risk_penalty({"liquidity_floor", "tao_outflow"}, flow_negative=True)

    assert penalty.penalty >= 40.0
    assert penalty.has_severe_risk
    assert "severe liquidity/emission risk" in penalty.risks


def test_swing_signal_uses_catalyst_and_coverage_context():
    current = make_snap(emission_rank=4)
    history = history_flow([20.0, 10.0, 5.0])
    relative = compute_relative_value_scores([current])[current.netuid]

    neutral = compute_swing_signal(
        current,
        history,
        relative,
        set(),
        covered=False,
        has_milestone=False,
    )
    catalyzed = compute_swing_signal(
        current,
        history,
        relative,
        {"convergence", "analyst_mention"},
        covered=True,
        has_milestone=True,
    )

    assert isinstance(catalyzed, SwingSignal)
    assert catalyzed.swing_score > neutral.swing_score
    assert "fresh convergence catalyst" in catalyzed.reasons
    assert "fresh analyst coverage" in catalyzed.reasons
    assert "fresh product/research milestone" in catalyzed.reasons


def test_swing_signal_penalizes_outflow_and_liquidity_risk():
    current = make_snap(
        volume_24h_alpha=1.0,
        alpha_price_tao=0.001,
        alpha_mcap_tao=100_000.0,
    )
    history = history_flow([-30.0, -20.0, -10.0])
    relative = compute_relative_value_scores([current])[current.netuid]

    signal = compute_swing_signal(
        current,
        history,
        relative,
        {"tao_outflow", "liquidity_floor"},
        covered=False,
        has_milestone=False,
    )

    assert isinstance(signal, SwingSignal)
    assert signal.swing_score < 50.0
    assert signal.risk.has_severe_risk
    assert signal.tradability.blocks_new_buy
    assert "sustained net TAO outflow" in signal.flow.risks


def test_important_buy_counts_as_large_inflow_catalyst():
    score = compute_catalyst_score({"important_buy"}, covered=False, has_milestone=False)

    assert score.score == 25.0
    assert score.is_positive
    assert "large net inflow catalyst" in score.reasons


def test_important_sell_counts_as_moderate_risk():
    penalty = compute_risk_penalty({"important_sell"}, flow_negative=False)

    assert penalty.penalty == 12.0
    assert penalty.moderate_count == 1
    assert "moderate risk alert" in penalty.risks


def test_outflow_aliases_count_as_one_moderate_risk():
    penalty = compute_risk_penalty({"tao_outflow", "important_sell"}, flow_negative=False)

    assert penalty.penalty == 12.0
    assert penalty.moderate_count == 1
    assert "moderate risk alert" in penalty.risks
