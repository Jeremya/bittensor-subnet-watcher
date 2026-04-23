# TAO Swing Signal Refactor Design

Date: 2026-04-23
Status: Draft for review

## Goal

Refactor the project from a broad subnet monitor into a 1-2 week TAO subnet swing-decision system.

The current model blends yield, health, and momentum into one composite score. That is directionally useful, but it hides the decision logic. For a 1-2 week horizon, the system should rank subnets by near-term capital flow and tradable opportunity, then use quality and catalysts as confirmation or vetoes.

## Current Mechanics Assumption

The model will follow the current Bittensor emission mechanics described in official docs:

- Subnet-level TAO emissions are flow-based.
- Net TAO inflow EMA drives each subnet's emission share.
- Sustained negative net flows can reduce or zero subnet emissions.
- Subnet alpha positions are exposed to AMM price and slippage on exit.
- Yuma Consensus still matters inside each subnet, but this app is primarily choosing which subnet alpha exposure to hold, add, avoid, or trim.

Implication: for a 1-2 week swing window, recent emission-adjusted TAO flow and tradable liquidity should outrank static GitHub/social metrics.

## New Signal Model

Replace the current implicit three-factor composite with five explicit components:

| Signal | Purpose | Direction |
| --- | --- | --- |
| Flow | Is capital entering now, and is it persistent enough to affect near-term emissions? | Leading |
| Relative Value | Is current emission yield/rank cheap relative to market cap? | Lagged but useful |
| Tradability | Can the position be entered/exited without unacceptable liquidity risk? | Gate + score |
| Catalyst | Is there a fresh reason for attention or capital to continue flowing? | Confirmation |
| Risk Penalty | Are there reasons to avoid or reduce exposure? | Veto/penalty |

The resulting score should be named `swing_score` in engine code. Existing `composite_score` can remain as the persisted DB/UI field for compatibility during the first implementation, but it should be computed from the new swing model.

## Signal Details

### 1. Flow Signal

Flow is the primary score.

Inputs:
- `net_tao_flow_tao` from snapshots
- `tao_in_tao` or `alpha_mcap_tao` as pool-size denominator
- `emission_rank` trend as lagged confirmation

Behavior:
- Use the most recent 1-day and 7-day windows from available snapshots.
- Score positive net flow as bullish only if it is meaningful relative to pool size.
- Penalize negative flow faster than positive flow is rewarded.
- Reward flow persistence across multiple recent observations, not one spike.
- Treat improving emission rank as confirmation, not the source of truth.

Initial weighting inside flow:
- 60% recent 24h flow rate
- 30% 7d flow rate
- 10% emission-rank confirmation

If only partial history exists, compute from available history but lower confidence.

### 2. Relative Value Signal

Relative value remains useful, but it is lagged.

Inputs:
- annualized emission yield
- `emission_rank`
- market-cap rank
- minimum market-cap guard

Behavior:
- Continue excluding micro-caps from yield normalization.
- Reward subnets earning more emissions than their market-cap rank implies.
- Penalize high market cap with weak emission rank.
- Do not let high lagged yield override current outflows.

This replaces the current yield score semantics with a clearer value score.

### 3. Tradability Signal

For a 1-2 week swing, tradability is not just "health"; it is a prerequisite.

Inputs:
- 24h volume converted to TAO
- `alpha_mcap_tao`
- available AMM pool reserves if present

Behavior:
- Score daily turnover relative to pool size.
- Strongly penalize low turnover.
- Block `new_buy` recommendations below the liquidity floor.
- Add a future extension point for SDK slippage estimates using `DynamicInfo.alpha_to_tao_with_slippage`.

Initial thresholds can reuse existing liquidity constants, but the output should be a score and a hard gate.

### 4. Catalyst Signal

Catalysts confirm that flow may continue.

Inputs:
- convergence alerts
- analyst mentions
- milestones
- GitHub spikes
- whale/net inflow alerts

Behavior:
- Catalyst score is additive but capped.
- Convergence is strongest because it means multiple signal classes fired.
- Analyst/milestone signals are useful only within the recommendation window.
- GitHub stars/forks alone are weak and should not create a high-conviction buy.

Catalyst should influence `add` and `new_buy`, but not mask severe risk.

### 5. Risk Penalty

Risk should reduce the swing score and drive sell/trim actions.

Inputs:
- severe alerts: `emission_near_zero`, `liquidity_floor`
- moderate alerts: `ownership_transfer`, `hyperparameter_change`, `tao_outflow`, `dead_github`
- sustained negative flow
- stale GitHub on material market cap
- social silence as low-weight context only

Behavior:
- Severe risks can force `sell` or block new exposure.
- Multiple moderate risks can create a thesis break when score is weak.
- Social silence should not be a primary sell signal.

## Composite Formula

Initial swing score:

```text
swing_score =
  0.40 * flow_score
+ 0.25 * relative_value_score
+ 0.20 * tradability_score
+ 0.15 * catalyst_score
- risk_penalty
```

Clamp to `0..100`.

If a component is unavailable, renormalize only among positive score components. Risk penalties still apply if available.

Rationale:
- Flow gets the largest weight because current mechanics make net TAO flow the leading emission driver.
- Relative value remains meaningful but is lagged.
- Tradability matters heavily for a swing horizon.
- Catalysts help timing but are less reliable than chain data.

## Recommendation Policy

Recommendations should use explicit rule gates before score ranking.

### New Buy

Required:
- `swing_score >= PORTFOLIO_NEW_BUY_MIN_SCORE`
- flow score positive or catalyst score strong
- tradability not below liquidity floor
- no severe risk
- candidate beats weakest held name by margin
- category concentration below limit

### Add

Required:
- already held
- `swing_score >= PORTFOLIO_ADD_MIN_SCORE`
- flow positive or catalyst strong
- category concentration below limit
- no thesis break

### Trim

Trigger:
- concentration above max allocation, or
- swing score deteriorates below hold floor while flow weakens, or
- tradability deteriorates enough that exit risk is rising.

### Sell

Trigger:
- severe risk, or
- multiple moderate risks plus weak swing score, or
- sustained negative flow with overvalued/weak relative-value state.

### Hold

Default when no high-conviction action is present.

## Code Architecture

Add a dedicated signal module:

```text
engine/signals.py
```

Responsibilities:
- compute `flow_score`
- compute `relative_value_score`
- compute `tradability_score`
- compute `catalyst_score`
- compute `risk_penalty`
- compute `swing_score`
- expose compact reason strings for recommendations/UI

Keep `engine/scorer.py` as the compatibility adapter:
- call `engine.signals`
- write legacy fields where possible
- set `composite_score = swing_score`

Update `engine/recommendations.py` to use the signal outputs and reason strings instead of only raw `composite_score`, alert sets, and hand-coded catalyst checks.

## Data and Schema

First implementation should avoid schema changes.

Use existing fields:
- snapshots: price, market cap, TAO reserve, volume, emission, rank, net flow, GitHub/X fields
- alerts: recent risk and catalyst types
- milestones and analyst mentions through existing helper queries

Potential later schema:
- persisted `flow_score`
- persisted `relative_value_score`
- persisted `tradability_score`
- persisted `catalyst_score`
- persisted `risk_penalty`
- persisted `swing_score`

Do not add these columns until the derived model is stable and tested.

## UI Impact

Minimal first pass:
- leaderboard can continue showing `Score`, now meaning swing score.
- subnet detail page should update language from generic "health/momentum" to 1-2 week swing reasons.
- portfolio page should show clearer reasons, such as:
  - `positive 24h/7d net TAO flow`
  - `cheap emissions versus market cap`
  - `liquidity below swing threshold`
  - `fresh convergence catalyst`
  - `sustained outflow risk`

Avoid adding a large UI rewrite in the signal refactor. The goal is better decisions first.

## Testing

Add unit tests for `engine/signals.py`:
- positive recent flow beats stale/flat flow
- negative flow is penalized faster than positive flow is rewarded
- high yield does not overcome negative flow plus risk
- illiquid candidates receive low tradability and are blocked from new buy
- convergence/analyst/milestone signals raise catalyst score
- severe risk creates a sell/block condition

Update existing scorer tests:
- `composite_score` should equal the new swing score adapter output
- old `hype_score` remains informational
- missing components renormalize correctly

Update recommendation tests:
- `new_buy` requires flow or strong catalyst
- `add` requires positive signal confirmation
- `trim` can fire on concentration or deteriorating swing score
- `sell` fires on severe risk or repeated moderate risk with weak score

## Rollout Plan

1. Add `engine/signals.py` with tests.
2. Adapt `engine/scorer.py` to compute compatibility fields from the new signals.
3. Update `engine/recommendations.py` to use swing signal gates.
4. Update route/template wording only where needed.
5. Run the full test suite.
6. Add a follow-up TODO for historical backtesting/calibration.

## Non-Goals

- No machine learning model.
- No external paid data source.
- No broad UI redesign.
- No immediate DB migration for per-signal persistence.
- No claim that recommendations are financial advice.

## Open Follow-Up

The next strategic improvement after this refactor should be a backtest report that evaluates 7d and 14d forward outcomes for historical score buckets, alerts, and recommendation actions.
