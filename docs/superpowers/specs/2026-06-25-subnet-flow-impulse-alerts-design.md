# Subnet Flow Impulse Alerts Design

Date: 2026-06-25
Status: Draft for review

## Goal

Add an easy first version of "important buy" and "important sell" monitoring for TAO
subnets.

The first implementation should use data the app already collects every poll:
emission-adjusted net TAO flow, pool size, price, volume, and reference slippage. It
should not claim wallet attribution, transaction attribution, or exact per-trade impact.
Those belong in the medium version.

The user-facing result is a clear alert when a subnet sees unusually large net buy or
sell pressure during one poll interval, similar in usefulness to a Telegram trade alert
but honest about its source:

- `important_buy`: capital is entering a subnet fast enough to matter.
- `important_sell`: capital is leaving a subnet fast enough to matter.

## Current Context

The app already has most of the easy-path inputs:

- `SubnetSnapshot.net_tao_flow_tao`: calculated in `main.py` as
  `delta(tao_in) - emission_accrual`, so emissions do not masquerade as new capital.
- `SubnetSnapshot.alpha_mcap_tao`: current denominator used by the existing
  `whale_inflow` and `tao_outflow` alerts.
- `SubnetSnapshot.tao_in_tao`: raw pool TAO reserve used to compute net flow.
- `SubnetSnapshot.alpha_price_tao` and previous snapshot price for price confirmation.
- `SubnetSnapshot.volume_24h_alpha`, `buy_slippage_pct`, and `sell_slippage_pct` for
  liquidity context.
- `alerts` table, Telegram delivery, dashboard alert feed, and subnet detail alert feed.

The existing `whale_inflow` and `tao_outflow` checks are close, but they are too thin for
the desired experience. They fire on one normalized threshold and provide limited
context. The older notes also identify a blocked wallet-level "single wallet stakes
more than 5%" design because the public SDK path for enumerating all stakers by subnet is
not confirmed. This spec re-scopes that blocked item into a snapshot-level v1 while
leaving the interface open for later event or wallet attribution.

## Approaches Considered

### 1. Retune the existing flow alerts only

This would lower or raise `WHALE_INFLOW_PCT` and `NET_OUTFLOW_ALERT_PCT`, then improve the
Telegram copy.

Pros:
- Fastest change.
- Almost no new code.

Cons:
- Still cannot express "important buy" and "important sell" as a distinct feature.
- Hard to add price, slippage, source, or later wallet fields without bloating
  `engine/alerts.py`.
- Does not solve duplicate semantics between "whale" and "net flow" language.

### 2. Recommended: add a flow impulse classifier

Add a small pure module that turns current and previous snapshots into a typed
`FlowImpulse` object. Alerting code converts that object into `AlertRecord`.

Pros:
- Uses current data and can be implemented quickly.
- Produces richer, more useful alert descriptions.
- Keeps later wallet/event attribution behind the same conceptual interface.
- Easier to unit test and backtest than alert-copy-only logic.

Cons:
- Slightly more code than retuning thresholds.
- Requires careful integration so Telegram does not emit both old and new flow alerts.

### 3. Medium path: parse chain events or wallet-level stake changes

Collect actual stake, unstake, swap, or pool events and attribute them to wallets,
validators, or transactions.

Pros:
- Can eventually produce transaction links, actor labels, gross buy/sell split, and real
  impact estimates.
- More similar to the reference Telegram alert.

Cons:
- Needs chain query experiments and a new event storage model.
- Higher operational risk and likely more edge cases.
- Not required for a useful first version.

## V1 Behavior

V1 detects subnet-level net flow impulses, not individual trades.

A flow impulse is eligible when all required fields are present:

- `net_tao_flow_tao` is present and non-zero.
- `alpha_mcap_tao` is present and greater than zero.
- Absolute net flow is at least `FLOW_IMPULSE_MIN_TAO`.
- Relative flow is above the direction-specific threshold:
  - buy: `net_tao_flow_tao / alpha_mcap_tao >= FLOW_IMPULSE_BUY_PCT`
  - sell: `abs(net_tao_flow_tao) / alpha_mcap_tao >= FLOW_IMPULSE_SELL_PCT`
- If `alpha_mcap_usd` is present, it must be at least `FLOW_IMPULSE_MIN_MCAP_USD`.
  Missing USD data should not suppress an otherwise valid TAO-denominated alert.

Initial config:

```python
FLOW_IMPULSE_MIN_TAO: float = 25.0
FLOW_IMPULSE_BUY_PCT: float = 0.05
FLOW_IMPULSE_SELL_PCT: float = 0.03
FLOW_IMPULSE_MIN_MCAP_USD: float = 50_000.0
FLOW_IMPULSE_COOLDOWN_HOURS: int = 2
```

These defaults intentionally match the existing relative thresholds for buy and sell
pressure, while adding an absolute TAO floor and a separate cooldown. They should be
calibrated with recent local snapshot history before Telegram is enabled.

## FlowImpulse Model

Add `engine/flow_impulse.py` with a small typed model:

```python
@dataclass(frozen=True)
class FlowImpulse:
    netuid: int
    direction: Literal["buy", "sell"]
    alert_type: Literal["important_buy", "important_sell"]
    source: Literal["snapshot_net_flow"]
    flow_tao: float
    relative_flow_pct: float
    threshold_pct: float
    impact_score: float
    price_move_pct: float | None = None
    volume_turnover_pct: float | None = None
    buy_slippage_pct: float | None = None
    sell_slippage_pct: float | None = None
    reasons: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
```

Expose one pure classifier:

```python
def classify_flow_impulse(
    current: SubnetSnapshot,
    previous: SubnetSnapshot | None = None,
) -> FlowImpulse | None:
    ...
```

The classifier owns:

- direction detection
- threshold checks
- relative-flow calculation
- price move calculation when previous price exists
- volume turnover calculation when volume and price exist
- impact score calculation
- reason and risk strings

The alert layer owns:

- converting `FlowImpulse` into `AlertRecord`
- registry name hydration
- cooldown and persistence
- notification delivery through existing unsent-alert flow

## Impact Score

The impact score is not the trigger. Threshold checks are the trigger. The score is a
compact way to rank and read alerts.

Use a deterministic bounded score:

```text
relative_multiple = relative_flow_pct / threshold_pct
absolute_multiple = abs(flow_tao) / FLOW_IMPULSE_MIN_TAO
price_confirmation = 1 when price moved in the impulse direction, otherwise 0

impact_score =
  50
+ 25 * min(max(relative_multiple - 1, 0), 2) / 2
+ 15 * min(max(absolute_multiple - 1, 0), 4) / 4
+ 10 * price_confirmation
```

Clamp to `0..100` and round to two decimals.

Price confirmation is useful context but must not be required. A subnet can have real
accumulation before price visibly moves, and a net outflow can matter even when price has
not broken yet.

## Alert Semantics

Add two user-facing alert types:

- `important_buy`
- `important_sell`

Example descriptions:

```text
Important buy pressure: +60.0 TAO net flow in one poll, 6.0% of pool
(threshold 5.0%). Price +2.1% since prior poll. Buy slippage 3.4% on reference size.
Source: emission-adjusted snapshot net flow, not wallet-attributed.
```

```text
Important sell pressure: -40.0 TAO net flow in one poll, 4.0% of pool
(threshold 3.0%). Price -1.5% since prior poll. Sell slippage 5.2% on reference size.
Source: emission-adjusted snapshot net flow, not wallet-attributed.
```

`AlertRecord.current_value` should store `relative_flow_pct` as a decimal fraction, same
as the existing flow alerts. `AlertRecord.threshold` should store the matching direction
threshold. `impact_score` lives in the description for v1 because the alerts table does
not have metadata columns.

## Duplicate Alert Rule

Do not emit both the old flow alert and the new flow impulse alert for the same subnet and
poll.

Implementation should preserve the existing helper functions for compatibility and
historic tests:

- `check_whale_inflow`
- `check_tao_outflow`

But `evaluate_alerts` should use the new flow impulse alert path for user-facing flow
monitoring. It should not append `check_whale_inflow` and `check_tao_outflow` candidates
in the same pass unless a future compatibility flag explicitly asks for legacy flow
alerts.

Existing historic alert types should still be understood by scoring and policy code.
New alert types should be aliases:

- `important_buy` counts as a positive catalyst wherever `whale_inflow` counts.
- `important_sell` counts as a moderate risk wherever `tao_outflow` counts.
- convergence should include `important_buy` alongside `whale_inflow`.

This keeps old database rows meaningful while making new notifications clearer.

## Data and Schema

No schema change is required for v1.

The existing `alerts` table is enough:

- `alert_type`: `important_buy` or `important_sell`
- `current_value`: relative flow percentage as a decimal fraction
- `threshold`: direction threshold as a decimal fraction
- `description`: readable context, including impact score and source

Do not add snapshot columns for v1. If backtesting shows impact score is worth charting or
ranking over time, add persisted fields in a later schema migration.

## Calibration Script

Add a small offline calibration script before enabling the alerts in production:

```text
scripts/backtest_flow_impulses.py
```

The script should:

- read snapshots from a selected SQLite database
- reconstruct previous snapshots per netuid
- run `classify_flow_impulse`
- simulate `FLOW_IMPULSE_COOLDOWN_HOURS`
- print alert counts by direction, netuid, and day
- print top examples with netuid, timestamp, flow TAO, relative flow, price move, and
  impact score

This is not a trading backtest. Its purpose is alert-volume calibration, duplicate
prevention, and catching thresholds that are obviously too noisy or too quiet.

## UI and Telegram

Keep UI changes minimal:

- Existing dashboard alert feed should show the new alert types automatically.
- Existing subnet detail alert list should show the new alert types automatically.
- Add Telegram emoji labels for `important_buy` and `important_sell`.
- Keep `parse_mode=None` and plain-text messages.

Do not add a new dashboard tab in v1. This is an alerting feature, not a new ranking
surface.

## Medium Extension Point

The medium version can add wallet or event attribution without replacing the user-facing
alert semantics.

Future additions should extend the model conceptually like this:

```python
source: Literal["snapshot_net_flow", "chain_event", "wallet_delta"]
actor_coldkey: str | None
actor_label: str | None
tx_hash: str | None
gross_buy_tao: float | None
gross_sell_tao: float | None
event_count: int | None
```

If event-level collection becomes reliable, add a separate storage table such as
`subnet_flow_events`. The classifier can then prefer event-level impulses when present
and fall back to snapshot net flow when not.

The v1 source string must remain explicit so users do not confuse net-flow inference with
wallet-attributed transactions.

## Testing

Add focused tests before implementation:

- `tests/engine/test_flow_impulse.py`
  - fires `important_buy` above relative and absolute thresholds
  - fires `important_sell` above relative and absolute thresholds
  - suppresses small relative flow
  - suppresses tiny absolute flow on micro pools
  - suppresses below-minimum USD market cap when USD market cap is present
  - does not require price confirmation
  - includes price move when previous price exists
  - includes volume turnover when volume and price exist
- `tests/engine/test_alerts.py`
  - `evaluate_alerts` persists new flow impulse alerts
  - `evaluate_alerts` does not emit duplicate legacy flow alerts in the same pass
  - direction-specific cooldown is respected
- `tests/engine/test_signals.py` or policy tests
  - `important_buy` is treated like `whale_inflow` for catalyst scoring
  - `important_sell` is treated like `tao_outflow` for risk scoring
- `tests/bot/test_telegram.py`
  - new alert types format with expected labels
- script test
  - calibration script can run against a minimal temp database and print counts

Run at least:

```text
.venv/bin/pytest tests/engine/test_flow_impulse.py tests/engine/test_alerts.py tests/engine/test_signals.py tests/bot/test_telegram.py -q
```

Run the full suite before merging because alert types affect scoring, policy, Telegram,
and dashboard behavior.

## Acceptance Criteria

- The monitor emits `important_buy` for large positive emission-adjusted net TAO flow.
- The monitor emits `important_sell` for large negative emission-adjusted net TAO flow.
- Alert descriptions clearly state that v1 is snapshot net-flow based, not
  wallet-attributed.
- No subnet gets both a new flow impulse alert and legacy `whale_inflow` or `tao_outflow`
  alert from the same evaluation pass.
- Existing old alert rows still influence scoring and policy correctly.
- New alert rows influence scoring and policy through explicit aliases.
- No database migration is required.
- A calibration script reports expected alert volume before production use.
- Focused tests and full test suite pass.
