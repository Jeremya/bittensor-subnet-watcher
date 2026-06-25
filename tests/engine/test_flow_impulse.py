from datetime import datetime, timezone

import pytest

import config
from engine.flow_impulse import classify_flow_impulse
from models import SubnetSnapshot


def make_snap(**overrides) -> SubnetSnapshot:
    data = {
        "netuid": 101,
        "polled_at": datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc),
        "alpha_price_tao": 1.0,
        "alpha_mcap_tao": 1_000.0,
        "alpha_mcap_usd": 100_000.0,
        "tao_in_tao": 1_000.0,
        "volume_24h_alpha": 100.0,
        "buy_slippage_pct": 3.4,
        "sell_slippage_pct": 4.2,
        "net_tao_flow_tao": 0.0,
    }
    data.update(overrides)
    return SubnetSnapshot(**data)


def test_important_buy_fires_above_relative_and_absolute_thresholds():
    previous = make_snap(alpha_price_tao=1.0)
    current = make_snap(net_tao_flow_tao=60.0, alpha_price_tao=1.02)

    impulse = classify_flow_impulse(current, previous)

    assert impulse is not None
    assert impulse.alert_type == "important_buy"
    assert impulse.direction == "buy"
    assert impulse.source == "snapshot_net_flow"
    assert impulse.flow_tao == pytest.approx(60.0)
    assert impulse.relative_flow_pct == pytest.approx(0.06)
    assert impulse.threshold_pct == pytest.approx(config.FLOW_IMPULSE_BUY_PCT)
    assert impulse.price_move_pct == pytest.approx(2.0)
    assert impulse.buy_slippage_pct == pytest.approx(3.4)
    assert impulse.impact_score > 50.0


def test_important_sell_fires_above_relative_and_absolute_thresholds():
    previous = make_snap(alpha_price_tao=1.0)
    current = make_snap(net_tao_flow_tao=-40.0, alpha_price_tao=0.985)

    impulse = classify_flow_impulse(current, previous)

    assert impulse is not None
    assert impulse.alert_type == "important_sell"
    assert impulse.direction == "sell"
    assert impulse.flow_tao == pytest.approx(-40.0)
    assert impulse.relative_flow_pct == pytest.approx(0.04)
    assert impulse.threshold_pct == pytest.approx(config.FLOW_IMPULSE_SELL_PCT)
    assert impulse.price_move_pct == pytest.approx(-1.5)
    assert impulse.sell_slippage_pct == pytest.approx(4.2)


def test_small_relative_flow_is_suppressed():
    current = make_snap(net_tao_flow_tao=30.0, alpha_mcap_tao=1_000.0)

    assert classify_flow_impulse(current) is None


def test_tiny_absolute_flow_on_micro_pool_is_suppressed():
    current = make_snap(
        net_tao_flow_tao=5.0,
        alpha_mcap_tao=50.0,
        alpha_mcap_usd=None,
    )

    assert classify_flow_impulse(current) is None


def test_below_minimum_usd_market_cap_is_suppressed_when_present():
    current = make_snap(
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1_000.0,
        alpha_mcap_usd=10_000.0,
    )

    assert classify_flow_impulse(current) is None


def test_missing_usd_market_cap_does_not_suppress_tao_denominated_alert():
    current = make_snap(
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1_000.0,
        alpha_mcap_usd=None,
    )

    impulse = classify_flow_impulse(current)

    assert impulse is not None
    assert impulse.alert_type == "important_buy"


def test_price_confirmation_is_not_required():
    previous = make_snap(alpha_price_tao=1.0)
    current = make_snap(net_tao_flow_tao=60.0, alpha_price_tao=0.95)

    impulse = classify_flow_impulse(current, previous)

    assert impulse is not None
    assert impulse.alert_type == "important_buy"
    assert impulse.price_move_pct == pytest.approx(-5.0)
    assert "price moved against impulse" in impulse.risks


def test_volume_turnover_is_included_when_fields_exist():
    current = make_snap(
        net_tao_flow_tao=60.0,
        volume_24h_alpha=100.0,
        alpha_price_tao=0.5,
        alpha_mcap_tao=1_000.0,
    )

    impulse = classify_flow_impulse(current)

    assert impulse is not None
    assert impulse.volume_turnover_pct == pytest.approx(5.0)
