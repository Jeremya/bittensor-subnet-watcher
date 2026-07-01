# Portfolio Action Rationale Design

## Goal

Make portfolio recommendations explain why each action was selected, starting with decision rationale. The current action cards often show a generic phrase such as "thesis break: severe or repeated risk alerts," which does not tell the user what evidence caused the recommendation.

## Scope

This change covers the `/portfolio` recommendation cards and the compact table recommendation text. It does not change the recommendation policy, thresholds, scoring model, alerts, or portfolio accounting.

## User Experience

Each non-hold action card should keep its current title and meta line, then show concrete rationale details before the short policy phrase.

Examples:

- `Risk: liquidity_floor, tao_outflow`
- `Allocation: 7.7% of portfolio`
- `Score: 17.5 swing`
- `Context: no recent analyst coverage or milestone`

The goal is scanability. Cards should remain compact, but they should answer "why am I being told to sell or trim this?" without requiring a subnet detail page.

## Data Model

Recommendation cards will gain a `context` field:

```python
[
    {"label": "Risk", "value": "liquidity_floor, tao_outflow", "tone": "danger"},
    {"label": "Allocation", "value": "7.7% of portfolio", "tone": "neutral"},
    {"label": "Score", "value": "17.5 swing", "tone": "danger"},
]
```

The existing `reasons` list remains the policy summary and stays compatible with current templates and tests.

## Rationale Builder

Add a small helper in `engine/recommendations.py` that builds context from data already available to `build_portfolio_recommendations()`:

- alert types for risk context
- allocation percentage for position sizing context
- score used by policy for score context
- analyst coverage and milestone membership for catalyst context
- action and confidence for selecting the most useful rows

The helper should omit unavailable data rather than rendering placeholders.

## Rendering

Update `web/templates/portfolio.html`:

- Action cards render `rec.context` as compact labeled rows or chips.
- Existing `rec.reasons` remain visible under the context.
- Table recommendations show the first concrete context item when available, falling back to the first reason.

No JavaScript is required.

## Testing

Add focused tests that verify:

- sell recommendations include risk alert context
- trim recommendations include allocation context
- action cards render context labels and values
- table recommendation text uses concrete context when present

Run the full test suite after implementation.
