"""Shared swing policy used by subnet detail and portfolio recommendations."""
from typing import Any, Optional
import config
from engine.signals import RiskSignal, SignalComponent, SwingSignal


def _is_signal(value: Any) -> bool:
    return isinstance(value, SwingSignal)


def build_signal_from_snapshot(
    snapshot: dict[str, Any],
    alert_types: set[str],
    covered: bool,
    has_milestone: bool,
) -> SwingSignal:
    flow_score = snapshot.get("flow_score")
    if flow_score is None:
        flow_score = snapshot.get("momentum_score")
    relative_value_score = snapshot.get("relative_value_score")
    if relative_value_score is None:
        relative_value_score = snapshot.get("yield_score")
    tradability_score = snapshot.get("tradability_score")
    swing_score = snapshot.get("swing_score")
    if swing_score is None:
        swing_score = snapshot.get("composite_score")
    risk_penalty = snapshot.get("risk_penalty") or 0.0
    turnover = None
    if (
        snapshot.get("volume_24h_alpha") is not None
        and snapshot.get("alpha_price_tao") is not None
        and snapshot.get("alpha_mcap_tao") is not None
        and snapshot.get("alpha_mcap_tao") > 0
    ):
        turnover = (
            snapshot["volume_24h_alpha"]
            * snapshot["alpha_price_tao"]
            / snapshot["alpha_mcap_tao"]
        )

    flow_positive = flow_score is not None and flow_score >= 70.0
    flow_negative = flow_score is not None and flow_score < 40.0
    tradability_blocks = False
    buy_slippage = snapshot.get("buy_slippage_pct")
    sell_slippage = snapshot.get("sell_slippage_pct")
    if buy_slippage is not None and buy_slippage >= config.TRADABILITY_MAX_SLIPPAGE_PCT:
        tradability_blocks = True
    if sell_slippage is not None and sell_slippage >= config.TRADABILITY_MAX_SLIPPAGE_PCT:
        tradability_blocks = True
    if turnover is not None and turnover < config.LIQUIDITY_FLOOR_RATIO:
        tradability_blocks = True
    if tradability_score is not None and tradability_score < 45.0:
        tradability_blocks = tradability_blocks or bool(
            snapshot.get("volume_24h_alpha")
            and snapshot.get("alpha_price_tao")
            and snapshot.get("alpha_mcap_tao")
        )

    catalyst_positive = (
        "convergence" in alert_types
        or "milestone" in alert_types
        or covered
        or has_milestone
        or (snapshot.get("momentum_score") or 0.0) >= 70.0
    )
    severe_risk = bool(
        ({"emission_near_zero", "liquidity_floor"} & alert_types)
        or risk_penalty >= 45.0
    )
    moderate_count = len(
        {"ownership_transfer", "hyperparameter_change", "tao_outflow", "dead_github"}
        & alert_types
    )

    flow_reasons = []
    flow_risks = []
    if flow_positive:
        flow_reasons.append("positive net TAO flow")
    if flow_negative:
        flow_risks.append("sustained net TAO outflow")

    catalyst_reasons = []
    if "convergence" in alert_types:
        catalyst_reasons.append("fresh convergence catalyst")
    if "analyst_mention" in alert_types or covered:
        catalyst_reasons.append("fresh analyst coverage")
    if "milestone" in alert_types or has_milestone:
        catalyst_reasons.append("fresh product/research milestone")
    if "github_spike" in alert_types:
        catalyst_reasons.append("GitHub attention spike")

    tradability_reasons = []
    tradability_risks = []
    if tradability_blocks:
        tradability_risks.append("liquidity below swing threshold")
        if turnover is not None and turnover < config.LIQUIDITY_FLOOR_RATIO:
            tradability_risks.append("daily turnover below swing floor")
    elif tradability_score is not None and tradability_score >= 65.0:
        tradability_reasons.append("tradable swing liquidity")

    risk_risks = []
    if severe_risk:
        risk_risks.append("severe liquidity/emission risk")
    elif moderate_count:
        risk_risks.append(
            "multiple moderate risk alerts" if moderate_count >= 2 else "moderate risk alert"
        )

    return SwingSignal(
        netuid=snapshot["netuid"],
        flow=SignalComponent(
            score=flow_score,
            reasons=flow_reasons,
            risks=flow_risks,
            is_positive=flow_positive,
            is_negative=flow_negative,
            is_strong=flow_score is not None and flow_score >= 70.0,
        ),
        relative_value=SignalComponent(
            score=relative_value_score,
            reasons=["cheap emissions versus market cap"] if (relative_value_score or 0.0) >= 75.0 else [],
            risks=["rich market cap versus emissions"] if (relative_value_score or 0.0) <= 35.0 else [],
            is_positive=relative_value_score is not None and relative_value_score >= 65.0,
            is_negative=relative_value_score is not None and relative_value_score <= 35.0,
        ),
        tradability=SignalComponent(
            score=tradability_score,
            reasons=tradability_reasons,
            risks=tradability_risks,
            is_positive=tradability_score is not None and tradability_score >= 65.0,
            is_negative=tradability_score is not None and tradability_score < 45.0,
            blocks_new_buy=tradability_blocks,
        ),
        catalyst=SignalComponent(
            score=snapshot.get("catalyst_score"),
            reasons=catalyst_reasons,
            is_positive=catalyst_positive,
            is_strong=catalyst_positive,
        ),
        risk=RiskSignal(
            penalty=round(float(risk_penalty), 2),
            risks=risk_risks,
            has_severe_risk=severe_risk,
            moderate_count=moderate_count,
        ),
        swing_score=round(float(swing_score or 0.0), 2),
        reasons=flow_reasons + catalyst_reasons + tradability_reasons,
        risks=flow_risks + tradability_risks + risk_risks,
    )


def verdict_for_subnet(
    signal: SwingSignal | None = None,
    *,
    owner_n: int | None = None,
    momentum_state: str | None = None,
    yield_state: str | None = None,
    health_risks: list[str] | None = None,
) -> str:
    """Single 1-2 week swing verdict for the subnet detail page.

    Prefer the explicit SwingSignal path. Scalar keyword args are kept as a
    compatibility adapter until all routes read persisted signal fields.
    """
    if _is_signal(signal):
        if signal.risk.has_severe_risk:
            return "Exit candidate"
        if signal.swing_score >= config.PORTFOLIO_ADD_MIN_SCORE and (
            signal.flow.is_positive or signal.catalyst.is_positive
        ):
            return "Entry signal"
        if signal.flow.is_negative:
            return "Caution"
        if signal.tradability.blocks_new_buy or signal.risk.risks:
            return "Risk flag"
        return "Monitor"

    if owner_n is None or momentum_state is None or yield_state is None:
        raise TypeError("verdict_for_subnet requires a SwingSignal or scalar keyword args")
    health_risks = health_risks or []
    if owner_n >= 3:
        return f"Governance risk — {owner_n} ownership changes in 30 days"
    if momentum_state == "fragile":
        return "Fragile — capital exiting despite rising emission rank"
    if momentum_state == "distributing" and yield_state in ("overpriced", "rich"):
        return "Exit candidate — overpriced and capital leaving"
    if momentum_state == "distributing":
        return "Caution — capital outflow, monitor emission rank"
    if yield_state == "underpriced" and momentum_state == "accumulating":
        return "Entry signal — underpriced yield with capital accumulating"
    if yield_state in ("underpriced", "discount") and momentum_state == "early_inflow":
        return "Potential entry — discount yield, inflow building"
    if yield_state in ("overpriced", "rich") and momentum_state != "accumulating":
        return "Avoid — priced above emission contribution"
    if health_risks:
        return f"Risk flag — {health_risks[0]}"
    return "Monitor — no strong entry or exit signal"


_BUY_SIDE_ACTIONS = {"add", "new_buy"}


def _apply_calibration(
    action: str,
    confidence: str,
    reasons: list[str],
    swing_score: float | None,
) -> tuple[str, str, list[str]]:
    """Reflect calibration state in buy-side recommendations without changing the action.

    Buy-side actions (add/new_buy) bet that a high score predicts future gains, but
    the swing model is not yet validated against forward returns. The first backtest
    also showed scores above SWING_EXTENDED_SCORE historically mean-revert over 14d.
    Surface both as caution and cap buy-side confidence; risk-driven sell/trim
    actions are rule-based and pass through untouched.
    """
    if action not in _BUY_SIDE_ACTIONS:
        return action, confidence, reasons
    reasons = list(reasons)
    if swing_score is not None and swing_score >= config.SWING_EXTENDED_SCORE:
        reasons.append(
            f"extended: scores above {config.SWING_EXTENDED_SCORE:g} have "
            "historically mean-reverted over 14d"
        )
    if not config.SWING_SIGNAL_VALIDATED:
        reasons.append("swing model not yet validated against forward returns")
        confidence = "low"
    return action, confidence, reasons


def _position_decision(
    *,
    swing_score: float,
    allocation_pct: float,
    catalyst: bool,
    thesis_break: bool,
    category_allocation_pct: float,
    has_outflow: bool = False,
    momentum_score: Optional[float] = None,
) -> tuple[str, str, list[str]]:
    if thesis_break:
        return "sell", "high", ["thesis break: severe or repeated risk alerts"]
    if allocation_pct >= config.PORTFOLIO_TRIM_MAX_ALLOC_PCT:
        reasons: list[str] = [f"position is {allocation_pct * 100:.1f}% of book"]
        if not catalyst:
            reasons.append("no fresh positive catalyst")
        return "trim", "high", reasons
    if (
        swing_score >= config.PORTFOLIO_ADD_MIN_SCORE
        and catalyst
        and category_allocation_pct < config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT
    ):
        return "add", "medium", ["strong held winner with room to add"]
    if swing_score < config.PORTFOLIO_HOLD_FLOOR_SCORE and (
        has_outflow
        or (momentum_score is not None and momentum_score < 40.0)
    ):
        return "trim", "medium", ["swing score deteriorating with outflow risk"]
    return "hold", "low", []


def action_for_position(
    signal: SwingSignal | None = None,
    *,
    swing_score: float | None = None,
    allocation_pct: float,
    catalyst: bool | None = None,
    thesis_break: bool | None = None,
    category_allocation_pct: float,
    has_outflow: bool = False,
    momentum_score: Optional[float] = None,
) -> tuple[str, str, list[str]] | dict[str, Any]:
    """Portfolio action for a held position.

    SwingSignal input returns a policy dict. Scalar keyword input returns the
    legacy tuple shape consumed by older tests and adapters.
    """
    if _is_signal(signal):
        action, confidence, reasons = _position_decision(
            swing_score=signal.swing_score,
            allocation_pct=allocation_pct,
            catalyst=signal.catalyst.is_positive,
            thesis_break=signal.risk.has_severe_risk
            or (
                signal.risk.moderate_count >= 2
                and signal.swing_score < config.PORTFOLIO_HOLD_FLOOR_SCORE
            ),
            category_allocation_pct=category_allocation_pct,
            has_outflow=signal.flow.is_negative,
            momentum_score=signal.flow.score,
        )
        action, confidence, reasons = _apply_calibration(
            action, confidence, reasons, signal.swing_score
        )
        return {"action": action, "confidence": confidence, "reasons": reasons}

    if swing_score is None or catalyst is None or thesis_break is None:
        raise TypeError("action_for_position requires a SwingSignal or scalar keyword args")
    return _position_decision(
        swing_score=swing_score,
        allocation_pct=allocation_pct,
        catalyst=catalyst,
        thesis_break=thesis_break,
        category_allocation_pct=category_allocation_pct,
        has_outflow=has_outflow,
        momentum_score=momentum_score,
    )


def action_for_new_candidate(
    signal: SwingSignal,
    *,
    weakest_held_score: float,
    category_allocation_pct: float,
) -> dict[str, Any] | None:
    """Portfolio action for a non-held candidate, or None when no action qualifies."""
    if signal.swing_score < config.PORTFOLIO_NEW_BUY_MIN_SCORE:
        return None
    if signal.swing_score < weakest_held_score + config.PORTFOLIO_REPLACE_SCORE_MARGIN:
        return None
    if signal.risk.has_severe_risk:
        return None
    if signal.tradability.blocks_new_buy:
        return None
    if category_allocation_pct >= config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT:
        return None
    if not signal.catalyst.is_positive:
        return None
    action, confidence, reasons = _apply_calibration(
        "new_buy",
        "medium",
        ["outranks weakest held name with a fresh catalyst"],
        signal.swing_score,
    )
    return {"action": action, "confidence": confidence, "reasons": reasons}
