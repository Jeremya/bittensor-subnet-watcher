# Pump Radar Phase 3a — GitHub releases catalyst feed

**Date:** 2026-07-04
**Status:** Approved

## Problem

The pumps are news-driven but the catalyst inputs are near-empty (2 milestones
ever; X cut). GitHub releases are the most durable, machine-readable "the team
shipped something" signal, and 109/129 subnets have a repo in the registry.

## Design

A third source inside `collectors/milestone.py`, exactly following the
arXiv/HuggingFace pattern:

- `_query_github_releases(owner, repo, since_iso) -> list[dict]`
  - `GET https://api.github.com/repos/{owner}/{repo}/releases?per_page=5`,
    `Authorization: token GITHUB_TOKEN` when set (reuse `parse_github_url`
    from `collectors/github.py` for owner/repo).
  - Skip `draft` and `prerelease`; keep `published_at > since_iso`.
  - Entry: `title = "{tag_name} — {name}"` (name falls back to tag),
    `url = html_url`, `published_at` parsed ISO.
- `MilestoneCollector.collect`: for each registry row with a github_url,
  also query releases; insert as `milestone_type="github_release"` via the
  existing `insert_milestone` (AI summary/take via `interpret_milestone`,
  dedup via UNIQUE(netuid, url)). New collector-state key
  `milestone_last_github_check`.
- `fire_milestone_alerts` emoji map: `github_release` → 🚢.
- Health panel: extend the milestone collector's freshness keys with the new
  state key (list already max()s over available keys — add the third).

Downstream (already built, untouched): milestone Telegram alert, catalyst
`has_milestone` boost, convergence `milestone` signal, subnet-page milestones
block, harness grading of `catalyst_score`.

## Failure modes

| Case | Handling |
|---|---|
| github_url unparseable / repo 404 | skip subnet, count in errors log line |
| 403 (rate limit) | one warning, abort the releases source for this run |
| timeout / network | per-repo try/except → skip (arXiv pattern) |
| malformed JSON / missing fields | skip entry |
| first run (no since state) | only releases from the last 7 days are ingested — never a 109-repo backlog flood |

## Out of scope

Tags-only repos (follow-up), RSS/blog sources, exchange-listing feeds.

## Testing

Unit: release filtering (draft/prerelease/since/malformed), first-run 7-day cap.
Collector: mocked HTTP → one insert, idempotent re-run, 403 aborts source.
Alert emoji golden. Full suite green.
