# Discord Intel Scoring Design

## Goal

Use Discord screenshots as short-lived qualitative intelligence for subnet
ranking. The system should automatically process screenshots dropped into a
local folder, detect the subnet from the Discord header, classify operational
and catalyst signals, and apply a bounded temporary adjustment to the actionable
swing score.

The raw market/protocol scores must remain visible and unchanged. Discord intel
adjusts the decision score, not the underlying Spec 421 or flow calculations.

## Non-Goals

- No dashboard upload flow in V1.
- No full admin review UI in V1.
- No direct mutation of `spec421_score`, `flow_score`, or raw `swing_score`.
- No scoring impact when the subnet cannot be identified with confidence.

## File Flow

Default folders:

- `data/discord_intel/inbox/`
- `data/discord_intel/processed/`
- `data/discord_intel/needs_review/`

The server scans the inbox every 15 minutes. For each image:

1. Compute a SHA256 hash and skip already processed files.
2. Extract text from the screenshot with OCR/vision.
3. Detect the subnet from the visible Discord header:
   - `# 114`
   - `114 • soma`
   - `SN114`
   - channel/subnet name fallback, validated against `subnet_registry`
4. If subnet confidence is below `0.80`, move the file to `needs_review/` and
   do not affect scoring.
5. Classify one or more events from the screenshot text.
6. Insert structured events into the database.
7. Move the file to `processed/`.

## Data Model

Add `discord_intel_events`:

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `image_hash TEXT NOT NULL`
- `netuid INTEGER NOT NULL`
- `subnet_name_detected TEXT`
- `source_image_path TEXT NOT NULL`
- `observed_at TEXT`
- `processed_at TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `severity TEXT NOT NULL` (`low`, `medium`, `high`)
- `confidence REAL NOT NULL`
- `direction TEXT NOT NULL` (`positive`, `negative`, `mixed`, `neutral`)
- `summary TEXT NOT NULL`
- `evidence_text TEXT NOT NULL`
- `score_impact REAL NOT NULL`
- `expires_at TEXT NOT NULL`
- `status TEXT NOT NULL DEFAULT 'active'`

Use indexes on `(netuid, status, expires_at)` and `image_hash`.

## Event Types

Initial event vocabulary:

- `team_responsive`: team gives a clear answer, diagnosis, or next step.
- `product_catalyst`: launch, benchmark, adoption, new round, useful feature.
- `community_momentum`: constructive miner/user activity and participation.
- `validator_issue`: rate limits, inference errors, unscored runs, scoring bugs.
- `fairness_concern`: suspicious score, contested leaderboard, unfair competition.
- `team_silence`: important questions with no visible team response.
- `hype_without_evidence`: excitement without concrete support.

Each event must include a short evidence excerpt or OCR-derived paraphrase so the
dashboard can explain the adjustment.

## Scoring

Keep both raw and adjusted scores:

```text
adjusted_swing_score = clamp(raw_swing_score + active_discord_adjustment, 0, 100)
```

Do not store `adjusted_swing_score` in `snapshots` in V1. Compute it when reading
latest dashboard/recommendation data so expired Discord events stop affecting the
decision score without rewriting historical snapshots.

`active_discord_adjustment` is the sum of active, non-expired events for that
netuid, with hard caps:

- positive boost cap: `+8`
- negative penalty cap: `-18`
- net absolute cap: `18`

Initial impact ranges:

- `team_responsive`: `+1` to `+4`
- `product_catalyst`: `+3` to `+8`
- `community_momentum`: `+1` to `+4`
- `validator_issue`: `-4` to `-12`
- `fairness_concern`: `-6` to `-15`
- `team_silence`: `-3` to `-8`
- `hype_without_evidence`: `-2` to `-6`

TTL by severity:

- `low`: 24 hours
- `medium`: 72 hours
- `high`: 7 days

Expired events should be ignored by scoring. They may remain in the table for
audit and future backtesting.

## Example

For a SOMA screenshot where miners report unscored runs and the team says the
provider is rate limiting and impacted runs are being reevaluated:

```text
validator_issue
severity: medium
confidence: 0.90
direction: negative
impact: -8
ttl: 72h
summary: miners report unscored runs and provider rate-limit issues
```

```text
team_responsive
severity: low
confidence: 0.80
direction: positive
impact: +3
ttl: 72h
summary: team explains the issue and says impacted runs are being reevaluated
```

Net adjustment: `-5`.

## Dashboard Changes

Keep V1 simple:

- Main dashboard ranking should use `adjusted_swing_score`.
- Show a compact `Discord ±X` badge when a subnet has active Discord intel.
- Subnet page should show:
  - raw swing score
  - Discord adjustment
  - adjusted swing score
  - active event summaries with severity, confidence, expiration, and source path

No `/discord-intel` admin page in V1.

## Error Handling

- Unknown subnet: move image to `needs_review/`; no scoring impact.
- Subnet confidence below `0.80`: move image to `needs_review/`; no scoring impact.
- Duplicate image hash: skip without inserting new events.
- OCR/vision failure: move image to `needs_review/`.
- Classification failure: move image to `needs_review/`.
- Missing evidence text: do not insert an event.

## Testing

Unit tests should cover:

- subnet header parsing
- duplicate hash handling
- event impact clamping
- TTL expiration behavior
- adjusted score calculation
- no scoring impact for low-confidence subnet detection

Integration tests should cover:

- inbox image produces events and moves to `processed/`
- ambiguous image moves to `needs_review/`
- dashboard data includes raw score, adjustment, and adjusted score
