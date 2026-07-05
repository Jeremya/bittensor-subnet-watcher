from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from db.database import (
    SCHEMA_SQL,
    get_collector_state,
    get_milestones_for_netuid,
)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


def test_parse_arxiv_feed_extracts_entries():
    from collectors.milestone import parse_arxiv_feed

    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>SparseLoCo: Gradient Compression</title>
        <id>http://arxiv.org/abs/2603.08163v1</id>
        <published>2026-03-10T12:00:00Z</published>
      </entry>
    </feed>"""
    entries = parse_arxiv_feed(xml)
    assert len(entries) == 1
    assert entries[0]["title"] == "SparseLoCo: Gradient Compression"
    assert entries[0]["url"] == "https://arxiv.org/abs/2603.08163"
    assert entries[0]["published_at"].year == 2026


def test_parse_arxiv_feed_strips_version_suffix():
    from collectors.milestone import parse_arxiv_feed

    xml = """<feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Test Paper</title>
        <id>http://arxiv.org/abs/1234.56789v3</id>
        <published>2026-01-01T00:00:00Z</published>
      </entry>
    </feed>"""
    entries = parse_arxiv_feed(xml)
    assert entries[0]["url"] == "https://arxiv.org/abs/1234.56789"


def test_parse_arxiv_feed_returns_empty_on_bad_xml():
    from collectors.milestone import parse_arxiv_feed

    entries = parse_arxiv_feed("this is not xml")
    assert entries == []


@pytest.mark.asyncio
async def test_interpret_milestone_returns_none_when_no_client():
    from collectors.milestone import interpret_milestone

    # Patch the module-level client to None (simulates missing API key)
    with patch("collectors.milestone._anthropic_client", None):
        result = await interpret_milestone(
            "Templar", 3, "arxiv", "Test Paper", "http://example.com"
        )
    assert result == (None, None)


@pytest.mark.asyncio
async def test_interpret_milestone_returns_summary_and_take():
    from unittest.mock import MagicMock
    from collectors.milestone import interpret_milestone

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"summary": "S", "take": "T"}')]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("collectors.milestone._anthropic_client", mock_client):
        summary, take = await interpret_milestone(
            "Templar", 3, "arxiv", "Test Paper", "http://example.com"
        )
    assert summary == "S"
    assert take == "T"


@pytest.mark.asyncio
async def test_interpret_milestone_returns_none_on_malformed_json():
    from unittest.mock import MagicMock
    from collectors.milestone import interpret_milestone

    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="not json at all")]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("collectors.milestone._anthropic_client", mock_client):
        summary, take = await interpret_milestone(
            "Templar", 3, "arxiv", "Test Paper", "http://example.com"
        )
    assert summary is None
    assert take is None


@pytest.mark.asyncio
async def test_collect_inserts_milestone_and_updates_state(db):
    from collectors.milestone import MilestoneCollector

    published_at = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    registry = {
        3: {"name": "Templar", "github_url": "https://github.com/org/repo"},
    }

    with patch.object(
        MilestoneCollector,
        "_query_arxiv",
        AsyncMock(return_value=[{
            "title": "SparseLoCo",
            "url": "https://arxiv.org/abs/2603.08163",
            "published_at": published_at,
        }]),
    ), patch.object(
        MilestoneCollector,
        "_query_huggingface",
        AsyncMock(return_value=[]),
    ), patch(
        "collectors.milestone.interpret_milestone",
        AsyncMock(return_value=("summary", "take")),
    ), patch("collectors.milestone.asyncio.sleep", AsyncMock()):
        inserted = await MilestoneCollector.collect(db, registry)

    assert inserted == 1
    rows = await get_milestones_for_netuid(db, 3)
    assert len(rows) == 1
    assert rows[0]["title"] == "SparseLoCo"
    assert rows[0]["ai_summary"] == "summary"
    assert rows[0]["ai_take"] == "take"
    assert await get_collector_state(db, "milestone_last_arxiv_check") is not None
    assert await get_collector_state(db, "milestone_last_hf_check") is not None


# ── GitHub releases source ────────────────────────────────────────────────────

from datetime import timedelta

from collectors.milestone import MilestoneCollector, _RateLimited, parse_release_entries


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


@pytest.mark.asyncio
async def test_collect_inserts_github_release(db):
    registry = {3: {"name": "Templar", "github_url": "https://github.com/org/repo"}}
    entry = {"title": "v1.0 — Launch",
             "url": "https://github.com/o/r/releases/tag/v1.0",
             "published_at": datetime.now(timezone.utc)}
    with patch.object(MilestoneCollector, "_query_arxiv", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_huggingface", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_github_releases",
                         AsyncMock(return_value=[entry])), \
            patch("collectors.milestone.interpret_milestone",
                  AsyncMock(return_value=(None, None))), \
            patch("collectors.milestone.asyncio.sleep", AsyncMock()):
        new = await MilestoneCollector.collect(db, registry)
    assert new == 1
    cur = await db.execute(
        "SELECT milestone_type, title FROM subnet_milestones WHERE milestone_type='github_release'")
    row = await cur.fetchone()
    assert row is not None and row["title"] == "v1.0 — Launch"
    assert await get_collector_state(db, "milestone_last_github_check") is not None


@pytest.mark.asyncio
async def test_collect_rate_limit_aborts_releases_source(db):
    registry = {
        3: {"name": "A", "github_url": "https://github.com/org/repo1"},
        4: {"name": "B", "github_url": "https://github.com/org/repo2"},
    }
    calls = AsyncMock(side_effect=_RateLimited("x"))
    with patch.object(MilestoneCollector, "_query_arxiv", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_huggingface", AsyncMock(return_value=[])), \
            patch.object(MilestoneCollector, "_query_github_releases", calls), \
            patch("collectors.milestone.asyncio.sleep", AsyncMock()):
        await MilestoneCollector.collect(db, registry)
    assert calls.await_count == 1        # aborted after the first 403
