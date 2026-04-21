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
