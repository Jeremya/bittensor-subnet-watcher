# GitHub Releases Catalyst Feed Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GitHub releases become a third milestone source, feeding the existing catalyst/convergence/alert pipeline.

**Architecture:** Extend `collectors/milestone.py` with a pure release-filter function + one HTTP query, following the arXiv/HF pattern exactly. New collector-state key, new alert emoji, health-panel key added. No schema changes. Spec: `docs/superpowers/specs/2026-07-04-github-releases-catalyst-design.md`.

**Tech Stack:** Python 3.13, aiohttp, pytest. Branch `github-releases-catalyst`. Suite currently 426 passing.

---

## Task 1: Release parsing + query + collect integration

**Files:**
- Modify: `collectors/milestone.py`
- Test: `tests/test_milestone_collector.py` (read its existing mocking style first; add the tests below adapted to it)

- [ ] **1.1 Failing tests** — append to `tests/test_milestone_collector.py`:

```python
from datetime import datetime, timedelta, timezone

from collectors.milestone import parse_release_entries


def _release(tag="v1.2.0", name="Big release", days_ago=1, draft=False,
             prerelease=False):
    published = (datetime.now(timezone.utc) - timedelta(days=days_ago))
    return {
        "tag_name": tag, "name": name, "draft": draft,
        "prerelease": prerelease,
        "published_at": published.isoformat().replace("+00:00", "Z"),
        "html_url": f"https://github.com/o/r/releases/tag/{tag}",
    }


def test_parse_release_entries_filters_drafts_and_prereleases():
    payload = [_release(), _release(tag="v2.0-rc", prerelease=True),
               _release(tag="wip", draft=True)]
    entries = parse_release_entries(payload, since_iso=None)
    assert len(entries) == 1
    assert entries[0]["title"] == "v1.2.0 — Big release"
    assert entries[0]["url"].endswith("/v1.2.0")


def test_parse_release_entries_respects_since():
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    payload = [_release(days_ago=1), _release(tag="v9", days_ago=0)]
    entries = parse_release_entries(payload, since_iso=old_iso)
    assert [e["title"].split(" — ")[0] for e in entries] == ["v9"]


def test_parse_release_entries_first_run_caps_at_seven_days():
    payload = [_release(days_ago=30), _release(tag="v9", days_ago=2)]
    entries = parse_release_entries(payload, since_iso=None)
    assert len(entries) == 1                      # 30-day-old release not flooded in


def test_parse_release_entries_skips_malformed():
    payload = [{"tag_name": "v1"}, _release()]    # missing published_at/html_url
    assert len(parse_release_entries(payload, since_iso=None)) == 1


def test_parse_release_entries_name_falls_back_to_tag():
    payload = [_release(name=None)]
    entries = parse_release_entries(payload, since_iso=None)
    assert entries[0]["title"] == "v1.2.0 — v1.2.0"
```

Run: `python -m pytest tests/test_milestone_collector.py -q` → ImportError.

- [ ] **1.2 Implement pure filter** in `collectors/milestone.py` (below `parse_arxiv_feed`):

```python
GITHUB_RELEASES_API = "https://api.github.com/repos/{owner}/{repo}/releases"
_FIRST_RUN_LOOKBACK_DAYS = 7


class _RateLimited(Exception):
    """GitHub returned 403: abort the releases source for this run."""


def parse_release_entries(payload: list[dict],
                          since_iso: Optional[str]) -> list[dict]:
    """Filter raw GitHub /releases JSON into milestone entries.

    Drafts and prereleases are noise. On the first run (no since state) only
    the last 7 days are ingested — never a 109-repo backlog flood.
    """
    if since_iso is None:
        since_iso = (datetime.now(timezone.utc)
                     - timedelta(days=_FIRST_RUN_LOOKBACK_DAYS)).isoformat()

    entries: list[dict] = []
    for release in payload:
        if not isinstance(release, dict):
            continue
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        url = release.get("html_url")
        published_raw = release.get("published_at")
        if not tag or not url or not published_raw:
            continue
        try:
            published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published_at.isoformat() <= since_iso:
            continue
        name = release.get("name") or tag
        entries.append({
            "title": f"{tag} — {name}",
            "url": url,
            "published_at": published_at,
        })
    return entries
```

Add `from datetime import datetime, timedelta, timezone` (timedelta is new) to the imports.

- [ ] **1.3 Query + collect integration.** Append to `MilestoneCollector`:

```python
    @staticmethod
    async def _query_github_releases(owner: str, repo: str,
                                     since_iso: Optional[str]) -> list[dict]:
        headers = {"Accept": "application/vnd.github+json"}
        if config.GITHUB_TOKEN:
            headers["Authorization"] = f"token {config.GITHUB_TOKEN}"
        url = GITHUB_RELEASES_API.format(owner=owner, repo=repo)
        try:
            async with aiohttp_session() as session:
                async with session.get(
                    url,
                    headers=headers,
                    params={"per_page": "5"},
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status == 403:
                        raise _RateLimited(url)
                    if resp.status != 200:
                        return []
                    payload = await resp.json()
        except _RateLimited:
            raise
        except Exception as exc:
            logger.warning("[COLLECTOR] milestone: releases failed repo=%s/%s error=%s",
                           owner, repo, exc)
            return []
        if not isinstance(payload, list):
            return []
        return parse_release_entries(payload, since_iso)
```

In `collect()`: read the state key next to the others —

```python
        github_since = await get_collector_state(db, "milestone_last_github_check")
```

Import `parse_github_url` at the top: `from collectors.github import parse_github_url`.
Inside the per-subnet loop, after the HuggingFace block (before `asyncio.sleep`),
add — with a `releases_enabled = True` flag initialised before the loop:

```python
            repo_parts = parse_github_url(
                row["github_url"] if not isinstance(row, dict) else row.get("github_url"))
            if releases_enabled and repo_parts:
                try:
                    release_entries = await MilestoneCollector._query_github_releases(
                        repo_parts[0], repo_parts[1], github_since)
                except _RateLimited:
                    logger.warning("[COLLECTOR] milestone: github rate-limited — "
                                   "skipping releases for the rest of this run")
                    releases_enabled = False
                    release_entries = []
                for entry in release_entries:
                    summary, take = await interpret_milestone(
                        name, netuid, "github_release", entry["title"], entry["url"])
                    if await insert_milestone(db, netuid, "github_release",
                                              entry["title"], entry["url"],
                                              entry["published_at"],
                                              ai_summary=summary, ai_take=take):
                        new_count += 1
```

and at the end, next to the other two: `await set_collector_state(db, "milestone_last_github_check", now_iso)`.

- [ ] **1.4 Collect-integration tests** — append (adapting to the file's existing fixture style):

```python
@pytest.mark.asyncio
async def test_collect_inserts_github_release(db, registry):
    entry = {"title": "v1.0 — Launch", "url": "https://github.com/o/r/releases/tag/v1.0",
             "published_at": datetime.now(timezone.utc)}
    with patch.object(MilestoneCollector, "_query_arxiv", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_huggingface", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_github_releases",
                         AsyncMock(return_value=[entry])), \
            patch("collectors.milestone.asyncio.sleep", AsyncMock()):
        new = await MilestoneCollector.collect(db, registry)
    assert new >= 1
    cur = await db.execute(
        "SELECT milestone_type, title FROM subnet_milestones WHERE milestone_type='github_release'")
    row = await cur.fetchone()
    assert row is not None and row["title"] == "v1.0 — Launch"


@pytest.mark.asyncio
async def test_collect_rate_limit_aborts_releases_source(db, registry):
    calls = AsyncMock(side_effect=_RateLimited("x"))
    with patch.object(MilestoneCollector, "_query_arxiv", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_huggingface", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_github_releases", calls), \
            patch("collectors.milestone.asyncio.sleep", AsyncMock()):
        await MilestoneCollector.collect(db, registry)
    assert calls.await_count == 1        # aborted after the first 403
```

(`registry` fixture must contain ≥2 subnets with github_urls; reuse/extend the file's existing registry fixture.)

- [ ] **1.5** Full suite green; commit: `feat: github releases as third milestone source`

---

## Task 2: Emoji, health key, verification

**Files:** Modify `engine/alerts.py` (`fire_milestone_alerts`), `engine/health.py`; Test: `tests/engine/test_alerts.py` or existing milestone alert tests

- [ ] **2.1** `engine/alerts.py` `fire_milestone_alerts` — replace the binary emoji pick:

```python
        type_emoji = {"arxiv": "🔬", "huggingface": "🤗",
                      "github_release": "🚢"}.get(row["milestone_type"], "📌")
```

- [ ] **2.2** `engine/health.py` `compute_collector_health` — extend the milestone keys tuple:

```python
    for key in ("milestone_last_arxiv_check", "milestone_last_hf_check",
                "milestone_last_github_check"):
```

- [ ] **2.3** Quick test for the emoji (append near existing milestone alert tests, matching their fixture style): insert a `github_release` milestone with `notified=0`, run `fire_milestone_alerts`, assert the fired alert's description starts with `🚢`.

- [ ] **2.4** Full suite green; live smoke test against a real repo (read-only):
`.venv/bin/python -c "import asyncio; from collectors.milestone import MilestoneCollector; print(asyncio.run(MilestoneCollector._query_github_releases('opentensor', 'bittensor', None))[:2])"` — expect recent bittensor releases (published within 7 days) or `[]`, no exception.

- [ ] **2.5** Update TODOS.md (Phase 3a done; tags-only repos + RSS/listings remain queued). Commit: `feat: release emoji, health key; record phase 3a` → finishing-a-development-branch (merge, push, remind restart).
