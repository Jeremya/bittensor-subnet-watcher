# TAO Subnet Investment Criteria Framework
**Date:** 2026-04-01 (revised 2026-04-03)
**Status:** Complete (research notes from brainstorming session)

---

## Core Mechanic: dTAO Flow-Based Emissions

The fundamental dynamic in dTAO (as of November 2025):

```
TAO Staking Inflows → Emission Share → Validator Rewards → Better Service → More Stakers
```

Subnet emission share is determined by **net TAO inflows/outflows**, not alpha token price.
TAO holders vote with capital: staking in = more emissions, unstaking = fewer.

Each subnet's share is computed as:
```
share(i) = flow(i)^p / Σflow(j)^p    (p=1 by default, linear)
```
where `flow(i)` is the 86.8-day EMA of net TAO inflows into subnet i. The EMA smoothing
prevents manipulation but introduces ~3-month lag between flow shifts and emission changes.

**Implication for investors:** `alpha_mcap_tao` (TAO in pool) is the cumulative flow stock.
Its *rate of change* is the leading indicator of future emission share — not current price.

---

## Primary Signal: Emission Yield (Current-State, Lagged)

The current emission yield formula remains valid as a **snapshot metric**:

```
Annualized Yield = (daily_emission_tao × TAO_price × 365) / alpha_mcap_usd
```

**Critical caveat:** `daily_emission_tao` reflects the 86.8-day EMA of past flows.
A subnet with declining inflows today may still show high yield for months before
emission share adjusts. Yield is a coincident/lagging signal, not a leading one.

**Relative value shortcut:**

```
Emission Rank ÷ MCap Rank
```

- Ratio > 1.0 = emitting more than market cap implies → potential undervaluation
- Ratio < 1.0 = market paying premium over emission yield → overvalued or flow-driven

---

## Subnet Lifecycle (6 Stages)

| Stage | Characteristics | Action |
|---|---|---|
| 1. Launch | Low mcap, low emissions, unknown team | Watch only |
| 2. Proving | GitHub activity picks up, metagraph growing, tao_in rising | **Optimal entry** |
| 3. Growth | Inflow momentum strong, emission share growing, yield compressing | Ride or trim |
| 4. Maturity | High tao_in, stable inflows, yield normalized | Hold or exit |
| 5. Decline | Outflows starting, GitHub silent, metagraph shrinking | Exit |
| 6. Death | Net outflows sustained, near-zero volume, team gone | Avoid |

---

## Signal Stack (Ranked by Alpha)

### Tier 1 — Core (highest alpha)
1. **TAO inflow direction** — is `alpha_mcap_tao` growing or shrinking week-over-week?
   This is the actual driver of future emission share. Leading indicator.
2. **Emission rank ÷ MCap rank ratio** — relative value at current emission rate.
   Valid but lagged (reflects past flows via 86.8d EMA).
3. **Annualized emission yield** — absolute yield vs. staking alternatives.
   Same lag caveat as above.
4. **Emission rank velocity** — rank change over 7 days (lagged confirmation).

### Tier 2 — Quality Gates (filter out trash)
5. **GitHub last push date** — >60 days silence on a live subnet = red flag
6. **Metagraph health** — n_neurons growing? Registration cost stable?
7. **Commit frequency trend** — accelerating = team shipping; flat/declining = coasting
8. **Team fingerprint** — same validator hotkey across many subnets = extraction risk

### Tier 3 — Momentum Confirmation
9. **Alpha price 7d/30d change** — secondary momentum signal (flows drive price, not vice versa)
10. **24h volume vs. mcap ratio** — liquidity health
11. **Holder distribution** — Gini coefficient of alpha holdings (concentration risk)
12. **Ownership transfer count** — multiple transfers in short window = instability

### Deprioritize (low signal)
- Raw follower count (vanity metric)
- GitHub stars (gameable)
- Absolute mcap without yield context
- Whitepapers (cheap to produce)
- Alpha token price as a standalone signal (effect, not cause)

---

## Red Flags (Auto-Disqualifiers)

- GitHub repo deleted or archived while subnet is live
- Same hotkey operating >4 subnets (extraction validator)
- Ownership transferred >2 times in 30 days
- Metagraph n_neurons declining month-over-month
- Alpha mcap >$5M with zero GitHub activity in 90 days
- Emission rank dropped >5 positions in 7 days with no explanation
- Sustained net TAO outflows (tao_in declining) for >14 days

---

## Investment Framework Summary

**Buy signal:** TAO inflow growing, emission rank ÷ mcap rank > 1.3, GitHub active (<30d),
metagraph growing, team not flagged.

**Hold signal:** Inflows stable, yield compressing but quality high, team shipping.

**Exit signal:** TAO outflows beginning, emission rank falling, GitHub going silent,
or team fingerprint risk emerging.

**Avoid:** Any auto-disqualifier above, or yield < staking TAO directly (~18% APY baseline).

---

## Scoring Model Notes

The composite score uses four components, all subject to flow-based mechanics:

| Component | What it measures | Lag |
|---|---|---|
| Yield | Current emission value vs mcap | ~86.8d (EMA) |
| Quality | GitHub activity + neuron count | Real-time |
| Momentum | TAO inflow change (primary) + emission rank (secondary) | tao_in: real-time; rank: ~86.8d |
| Hype | Social presence (followers + tweet recency) | Real-time |

The momentum score now weights TAO inflow change (the actual flow driver) more heavily
than emission rank change (the lagged result), reflecting the true causal order.

---

## Data Sources

| Source | Data | Cost | Reliability |
|---|---|---|---|
| tao.app API | Prices, mcap, volume, emission ranks | Free | High |
| Bittensor SDK (`bt.AsyncSubtensor`) | On-chain emissions, metagraph, registrations | Free | High |
| GitHub API (unauthenticated) | Commits, stars, forks, issues | Free (60 req/hr) | High |
| taomarketcap.com | Subnet names, current rankings | Free (scrape) | Medium |
| backprop.finance | Subnet leaderboard, mcap rankings | Free (scrape) | Medium |
| X/Twitter | Social sentiment, team activity | Free (scrape, brittle) | Low |
| taostats.io | Historical data | Cloudflare-blocked | — |
