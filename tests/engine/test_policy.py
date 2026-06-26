from engine.policy import (
    build_signal_from_snapshot,
    action_for_new_candidate,
    action_for_position,
    verdict_for_subnet,
)
from engine.signals import RiskSignal, SignalComponent, SwingSignal
import config
import pytest


def make_signal(
    swing_score=82.0,
    flow_positive=True,
    catalyst_strong=True,
    severe_risk=False,
    tradability_blocks=False,
):
    return SwingSignal(
        netuid=3,
        flow=SignalComponent(
            score=82.0,
            reasons=["positive net TAO flow"] if flow_positive else [],
            risks=[] if flow_positive else ["sustained net TAO outflow"],
            is_positive=flow_positive,
            is_negative=not flow_positive,
        ),
        relative_value=SignalComponent(
            score=74.0,
            reasons=["cheap emissions versus market cap"],
            is_positive=True,
        ),
        tradability=SignalComponent(
            score=88.0 if not tradability_blocks else 10.0,
            reasons=["tradable daily turnover"] if not tradability_blocks else [],
            risks=[] if not tradability_blocks else ["liquidity below swing threshold"],
            is_positive=not tradability_blocks,
            blocks_new_buy=tradability_blocks,
        ),
        catalyst=SignalComponent(
            score=76.0 if catalyst_strong else None,
            reasons=["fresh convergence catalyst"] if catalyst_strong else [],
            is_positive=catalyst_strong,
            is_strong=catalyst_strong,
        ),
        risk=RiskSignal(
            penalty=45.0 if severe_risk else 0.0,
            risks=["severe liquidity/emission risk"] if severe_risk else [],
            has_severe_risk=severe_risk,
        ),
        swing_score=swing_score,
        reasons=[],
        risks=[],
    )


# ── verdict_for_subnet ──────────────────────────────────────────────────────

def test_verdict_governance_risk():
    assert "Governance risk" in verdict_for_subnet(owner_n=3, momentum_state="accumulating", yield_state="underpriced", health_risks=[])


def test_verdict_entry_signal():
    assert verdict_for_subnet(owner_n=1, momentum_state="accumulating", yield_state="underpriced", health_risks=[]) == "Entry signal — underpriced yield with capital accumulating"


def test_verdict_exit_candidate():
    assert verdict_for_subnet(owner_n=1, momentum_state="distributing", yield_state="rich", health_risks=[]) == "Exit candidate — overpriced and capital leaving"


def test_verdict_fragile():
    assert "Fragile" in verdict_for_subnet(owner_n=1, momentum_state="fragile", yield_state="underpriced", health_risks=[])


def test_verdict_caution():
    assert "Caution" in verdict_for_subnet(owner_n=1, momentum_state="distributing", yield_state="fair", health_risks=[])


def test_verdict_potential_entry():
    assert "Potential entry" in verdict_for_subnet(owner_n=1, momentum_state="early_inflow", yield_state="discount", health_risks=[])


def test_verdict_avoid():
    assert "Avoid" in verdict_for_subnet(owner_n=1, momentum_state="neutral", yield_state="rich", health_risks=[])


def test_verdict_risk_flag():
    assert "Risk flag" in verdict_for_subnet(owner_n=1, momentum_state="neutral", yield_state="fair", health_risks=["exit liquidity risk: <0.1% daily turnover"])


def test_verdict_monitor():
    assert "Monitor" in verdict_for_subnet(owner_n=1, momentum_state="neutral", yield_state="fair", health_risks=[])


def test_verdict_accepts_swing_signal():
    assert verdict_for_subnet(make_signal()) == "Entry signal"
    assert verdict_for_subnet(make_signal(flow_positive=False, severe_risk=True)) == "Exit candidate"


def test_build_signal_from_snapshot_uses_persisted_fields_and_context():
    signal = build_signal_from_snapshot(
        {
            "netuid": 7,
            "flow_score": 78.0,
            "relative_value_score": 72.0,
            "tradability_score": 44.0,
            "buy_slippage_pct": 10.0,
            "sell_slippage_pct": 11.0,
            "catalyst_score": 81.0,
            "risk_penalty": 12.0,
            "swing_score": 66.0,
        },
        {"convergence", "analyst_mention"},
        covered=True,
        has_milestone=True,
    )

    assert signal.flow.is_positive
    assert signal.tradability.blocks_new_buy
    assert signal.catalyst.is_positive
    assert signal.risk.penalty == pytest.approx(12.0)
    assert signal.swing_score == pytest.approx(66.0)


def test_build_signal_from_snapshot_treats_important_buy_as_catalyst():
    signal = build_signal_from_snapshot(
        {
            "netuid": 7,
            "flow_score": 55.0,
            "relative_value_score": 60.0,
            "tradability_score": 70.0,
            "catalyst_score": 25.0,
            "risk_penalty": 0.0,
            "swing_score": 68.0,
        },
        {"important_buy"},
        covered=False,
        has_milestone=False,
    )

    assert signal.catalyst.is_positive
    assert "large net inflow catalyst" in signal.catalyst.reasons


def test_build_signal_from_snapshot_treats_important_sell_as_moderate_risk():
    signal = build_signal_from_snapshot(
        {
            "netuid": 7,
            "flow_score": 55.0,
            "relative_value_score": 60.0,
            "tradability_score": 70.0,
            "catalyst_score": None,
            "risk_penalty": 12.0,
            "swing_score": 58.0,
        },
        {"important_sell"},
        covered=False,
        has_milestone=False,
    )

    assert signal.risk.moderate_count == 1
    assert "moderate risk alert" in signal.risk.risks


# ── action_for_position ─────────────────────────────────────────────────────

def test_action_sell_on_thesis_break(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    action, confidence, reasons = action_for_position(
        swing_score=40.0, allocation_pct=0.10, catalyst=False,
        thesis_break=True, category_allocation_pct=0.15,
    )
    assert action == "sell"
    assert confidence == "high"
    assert "thesis break" in reasons[0]


def test_action_trim_on_concentration(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    action, confidence, reasons = action_for_position(
        swing_score=80.0, allocation_pct=0.30, catalyst=True,
        thesis_break=False, category_allocation_pct=0.30,
    )
    assert action == "trim"
    assert confidence == "high"


def test_action_add_on_strong_winner(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    monkeypatch.setattr(config, "PORTFOLIO_ADD_MIN_SCORE", 75.0)
    monkeypatch.setattr(config, "PORTFOLIO_CATEGORY_MAX_ALLOC_PCT", 0.45)
    action, confidence, _ = action_for_position(
        swing_score=82.0, allocation_pct=0.10, catalyst=True,
        thesis_break=False, category_allocation_pct=0.20,
    )
    assert action == "add"
    assert confidence == "medium"


def test_action_trim_on_weak_score_and_outflow(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    monkeypatch.setattr(config, "PORTFOLIO_HOLD_FLOOR_SCORE", 55.0)
    action, confidence, reasons = action_for_position(
        swing_score=48.0, allocation_pct=0.10, catalyst=False,
        thesis_break=False, category_allocation_pct=0.15,
        has_outflow=True,
    )
    assert action == "trim"
    assert "swing score deteriorating" in reasons[0]


def test_action_hold_when_nothing_fires(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_TRIM_MAX_ALLOC_PCT", 0.25)
    monkeypatch.setattr(config, "PORTFOLIO_ADD_MIN_SCORE", 75.0)
    action, _, _ = action_for_position(
        swing_score=65.0, allocation_pct=0.10, catalyst=False,
        thesis_break=False, category_allocation_pct=0.20,
    )
    assert action == "hold"


def test_action_for_position_accepts_swing_signal(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_ADD_MIN_SCORE", 75.0)
    monkeypatch.setattr(config, "SWING_SIGNAL_VALIDATED", False)
    monkeypatch.setattr(config, "SWING_EXTENDED_SCORE", 80.0)
    result = action_for_position(
        make_signal(),  # swing_score=82.0 → in the extended band
        allocation_pct=0.10,
        category_allocation_pct=0.20,
    )

    # Action is unchanged; only the calibration disclosure is added.
    assert result["action"] == "add"
    assert result["confidence"] == "low"  # capped while the model is unvalidated
    assert result["reasons"][0] == "strong held winner with room to add"
    assert any("not yet validated" in r for r in result["reasons"])
    assert any("mean-reverted" in r for r in result["reasons"])


def test_action_for_new_candidate_uses_swing_signal(monkeypatch):
    monkeypatch.setattr(config, "PORTFOLIO_NEW_BUY_MIN_SCORE", 78.0)
    monkeypatch.setattr(config, "PORTFOLIO_REPLACE_SCORE_MARGIN", 8.0)
    monkeypatch.setattr(config, "SWING_SIGNAL_VALIDATED", False)
    monkeypatch.setattr(config, "SWING_EXTENDED_SCORE", 80.0)
    result = action_for_new_candidate(
        make_signal(swing_score=90.0),
        weakest_held_score=70.0,
        category_allocation_pct=0.20,
    )

    assert result["action"] == "new_buy"
    assert result["confidence"] == "low"
    assert result["reasons"][0] == "outranks weakest held name with a fresh catalyst"
    assert any("not yet validated" in r for r in result["reasons"])
    assert any("mean-reverted" in r for r in result["reasons"])
