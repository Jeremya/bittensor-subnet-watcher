# TAO Subnet Investment Criteria Framework
**Date:** 2026-04-01 (revised 2026-06-29)
**Status:** Complete (research notes from brainstorming session)

> **Spec 421 update, 2026-06:** The direct emission-share model is now price-based:
> `root_proportion × SubnetMovingPrice × (1 - MinerBurned)`. Flow remains a demand
> and risk signal, but it is no longer the direct emission-share formula.

---

## Current Core Mechanic: Spec 421 Price-Based Emissions

As of Spec 421 on mainnet, subnet emission share is price-based. The app treats
price EMA strength and emission value versus market cap as primary scoring inputs,
while keeping net TAO flow as demand confirmation and risk context.

```
Moving Price Strength → Emission Share → Validator Rewards → Better Service → More Demand
```

Each subnet's emission share is now computed from the Spec 421 structure:
```
share(i) = root_proportion × SubnetMovingPrice × (1 - MinerBurned)
```

**Implication for investors:** alpha price strength now has protocol-level importance.
`alpha_mcap_tao` and `net_tao_flow_tao` still matter, but as demand/liquidity context
and risk confirmation rather than the direct emission-share formula.

---

## Emission Yield As Current-State Value Signal

The current emission yield formula remains valid as a **snapshot metric**:

```
Annualized Yield = (daily_emission_tao × TAO_price × 365) / alpha_mcap_usd
```

**Critical caveat:** `daily_emission_tao` reflects current protocol allocation and can
still be a coincident metric. It should be interpreted alongside price EMA strength,
emission value versus market cap, and liquidity/risk context.

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
| 2. Proving | GitHub activity picks up, metagraph growing, price setup improving | **Optimal entry** |
| 3. Growth | Price EMA strong, emission setup improving, demand confirmed | Ride or trim |
| 4. Maturity | Stable price setup, stable demand, yield normalized | Hold or exit |
| 5. Decline | Price setup weakening, outflows starting, GitHub silent | Exit |
| 6. Death | Net outflows sustained, near-zero volume, team gone | Avoid |

---

## Signal Stack (Ranked by Alpha)

### Tier 1 — Core (highest alpha)
1. **Spec 421 price EMA strength** — is the subnet's price-based emission setup improving?
   Primary protocol-thesis input after Spec 421.
2. **Emission value versus market cap** — relative value at current emission rate.
   Valid as a current-state value signal, but not sufficient without trend/risk context.
3. **Annualized emission yield** — absolute yield vs. staking alternatives.
   Same current-state caveat as above.
4. **TAO inflow direction** — is `alpha_mcap_tao` growing or shrinking week-over-week?
   Demand confirmation and liquidity/risk context, not the direct emission-share formula.

### Tier 2 — Quality Gates (filter out trash)
5. **GitHub last push date** — >60 days silence on a live subnet = red flag
6. **Metagraph health** — n_neurons growing? Registration cost stable?
7. **Commit frequency trend** — accelerating = team shipping; flat/declining = coasting
8. **Team fingerprint** — same validator hotkey across many subnets = extraction risk

### Tier 3 — Momentum Confirmation
9. **Alpha price 7d/30d change** — secondary momentum signal around the protocol price context
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

**Buy signal:** Spec 421 price EMA strength improving, emission value attractive versus
market cap, GitHub active (<30d), metagraph growing, demand confirmed by non-hostile flow,
team not flagged.

**Hold signal:** Price-based emission setup remains strong, emission value is no longer
cheap but quality is high, liquidity is acceptable, team still shipping.

**Exit signal:** Price setup weakening, severe TAO outflows or liquidity risk emerging,
emission value deteriorating, GitHub going silent, or team fingerprint risk emerging.

**Avoid:** Any auto-disqualifier above, or yield < staking TAO directly (~18% APY baseline).

---

## Scoring Model Notes

The swing score now centers on Spec 421 protocol context, with flow retained as demand
and risk context:

| Component | What it measures | Lag |
|---|---|---|
| Spec 421 | Price EMA, emission value, protocol context | Current / recent price context |
| Flow | TAO demand direction and outflow risk | Real-time |
| Tradability | Liquidity, volume, and exit feasibility | Real-time |
| Catalyst | Fresh alerts, coverage, and milestones | Real-time |

Flow remains useful as a demand and risk signal, but it is not treated as the direct
driver of future emission share after Spec 421.

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
