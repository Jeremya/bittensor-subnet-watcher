import pytest
import config

from engine.recommendations import (
    build_portfolio_ledger,
    build_portfolio_recommendations,
)


def make_row(**overrides):
    row = {
        "coldkey": "ck1",
        "netuid": 3,
        "alpha_amount": 100.0,
        "tao_value": 6.0,
        "baseline_tao_value": 5.0,
        "name": "Templar",
        "category": "AI Training",
        "tao_usd_price": 300.0,
    }
    row.update(overrides)
    return row


def make_snapshot(**overrides):
    snap = {
        "netuid": 3,
        "name": "Templar",
        "category": "AI Training",
        "composite_score": 80.0,
        "yield_score": 78.0,
        "health_score": 76.0,
        "momentum_score": 74.0,
    }
    snap.update(overrides)
    return snap


def test_build_portfolio_ledger_uses_single_usd_price_for_all_rows():
    rows = [
        make_row(netuid=3, tao_value=6.0, tao_usd_price=None),
        make_row(
            netuid=56,
            name="Gradients",
            tao_value=4.0,
            baseline_tao_value=3.0,
            tao_usd_price=300.0,
        ),
    ]

    ledger = build_portfolio_ledger(rows, ["ck1"], ["Main"])
    values = {
        p["netuid"]: p["usd_value"]
        for p in ledger["wallets"][0]["positions"]
    }

    assert values[3] == pytest.approx(1800.0)
    assert values[56] == pytest.approx(1200.0)


def test_build_portfolio_ledger_excludes_zero_baseline_from_pnl_totals():
    rows = [
        make_row(netuid=3, tao_value=6.0, baseline_tao_value=5.0, tao_usd_price=300.0),
        make_row(netuid=56, name="Gradients", tao_value=8.0, baseline_tao_value=0.0, tao_usd_price=300.0),
    ]

    ledger = build_portfolio_ledger(rows, ["ck1"], ["Main"])
    assert ledger["grand_pnl_tao"] == pytest.approx(1.0)
    assert ledger["grand_pnl_pct"] == pytest.approx(20.0)


def test_recommendations_sell_on_thesis_break(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    positions = {
        21: {
            "netuid": 21,
            "subnet_name": "Vector",
            "category": "Infrastructure",
            "tao_value": 12.0,
            "allocation_pct": 0.30,
        }
    }
    snapshots = [make_snapshot(netuid=21, name="Vector", category="Infrastructure", composite_score=41.0)]
    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={21: {"liquidity_floor", "tao_outflow"}},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["table_actions"][21]["action"] == "sell"
    assert result["portfolio_actions"][0]["action"] == "sell"


def test_recommendations_trim_on_concentration_without_thesis_break(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    positions = {
        3: {
            "netuid": 3,
            "subnet_name": "Templar",
            "category": "AI Training",
            "tao_value": 34.0,
            "allocation_pct": 0.34,
        }
    }
    snapshots = [make_snapshot(netuid=3, composite_score=81.0)]
    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["table_actions"][3]["action"] == "trim"


def test_recommendations_blocks_new_buy_on_illiquid_candidate(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_NEW_BUY_MIN_SCORE", 78.0)
    monkeypatch.setattr(config, "PORTFOLIO_REPLACE_SCORE_MARGIN", 8.0)
    monkeypatch.setattr(config, "LIQUIDITY_FLOOR_RATIO", 0.001)
    positions = {
        7: {
            "netuid": 7,
            "subnet_name": "Cortex",
            "category": "Infrastructure",
            "tao_value": 8.0,
            "allocation_pct": 0.08,
        }
    }
    # SN96: high score, has catalyst, but near-zero liquidity
    snapshots = [
        make_snapshot(netuid=7, name="Cortex", category="Infrastructure", composite_score=62.0),
        make_snapshot(
            netuid=96, name="SN96", category="AI Training", composite_score=90.0,
            volume_24h_alpha=1.0, alpha_price_tao=0.001, alpha_mcap_tao=100_000.0,  # ratio = 0.00001
        ),
    ]
    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={},
        coverage_netuids={96},
        milestone_netuids=set(),
    )

    assert all(c["netuid"] != 96 for c in result["new_candidates"])


def test_recommendations_emit_new_buy_when_candidate_outranks_weakest_held(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_NEW_BUY_MIN_SCORE", 78.0)
    monkeypatch.setattr(config, "PORTFOLIO_REPLACE_SCORE_MARGIN", 8.0)
    positions = {
        7: {
            "netuid": 7,
            "subnet_name": "Cortex",
            "category": "Infrastructure",
            "tao_value": 8.0,
            "allocation_pct": 0.08,
        }
    }
    snapshots = [
        make_snapshot(netuid=7, name="Cortex", category="Infrastructure", composite_score=62.0),
        make_snapshot(netuid=14, name="Macro", category="AI Training", composite_score=82.0),
    ]
    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={},
        coverage_netuids={14},
        milestone_netuids=set(),
    )

    assert result["new_candidates"][0]["netuid"] == 14
    assert result["new_candidates"][0]["action"] == "new_buy"


def test_new_buy_requires_positive_flow_or_strong_catalyst(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_NEW_BUY_MIN_SCORE", 78.0)
    monkeypatch.setattr(config, "PORTFOLIO_REPLACE_SCORE_MARGIN", 8.0)
    positions = {
        7: {
            "netuid": 7,
            "subnet_name": "Cortex",
            "category": "Infrastructure",
            "tao_value": 8.0,
            "allocation_pct": 0.08,
        }
    }
    snapshots = [
        make_snapshot(netuid=7, name="Cortex", category="Infrastructure", composite_score=62.0),
        make_snapshot(
            netuid=14,
            name="Macro",
            category="AI Training",
            composite_score=90.0,
            momentum_score=45.0,
        ),
    ]

    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["new_candidates"] == []


def test_trim_on_weak_swing_score_and_outflow_risk(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_HOLD_FLOOR_SCORE", 55.0)
    positions = {
        3: {
            "netuid": 3,
            "subnet_name": "Templar",
            "category": "AI Training",
            "tao_value": 10.0,
            "allocation_pct": 0.10,
        }
    }
    snapshots = [make_snapshot(netuid=3, composite_score=48.0, momentum_score=35.0)]

    result = build_portfolio_recommendations(
        positions_by_netuid=positions,
        snapshots=snapshots,
        alert_types_by_netuid={3: {"tao_outflow"}},
        coverage_netuids=set(),
        milestone_netuids=set(),
    )

    assert result["table_actions"][3]["action"] == "trim"
    assert "swing score deteriorating with outflow risk" in result["table_actions"][3]["reasons"]
