from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from db.database import SCHEMA_SQL, add_analyst_handle, get_analyst_mentions_for_netuid


REGISTRY = {
    3: {"name": "Templar"},
    56: {"name": "Gradients"},
    13: {"name": "Macrocosmos"},
}


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


def test_matches_sn_pattern():
    from collectors.analyst import match_subnets

    result = match_subnets("SN3 is going to pump hard", REGISTRY)
    assert result == {3}


def test_matches_sn_pattern_case_insensitive():
    from collectors.analyst import match_subnets

    result = match_subnets("watching sn56 closely", REGISTRY)
    assert result == {56}


def test_matches_subnet_name():
    from collectors.analyst import match_subnets

    result = match_subnets("Templar shipped a new model today", REGISTRY)
    assert result == {3}


def test_matches_subnet_name_case_insensitive():
    from collectors.analyst import match_subnets

    result = match_subnets("templar is doing great things", REGISTRY)
    assert result == {3}


def test_matches_multiple_subnets():
    from collectors.analyst import match_subnets

    result = match_subnets("SN3 and Gradients are my top picks", REGISTRY)
    assert result == {3, 56}


def test_no_match_returns_empty():
    from collectors.analyst import match_subnets

    result = match_subnets("Bitcoin is going to 100k", REGISTRY)
    assert result == set()


def test_sn_pattern_not_in_registry_ignored():
    from collectors.analyst import match_subnets

    result = match_subnets("SN999 is unknown", REGISTRY)
    assert result == set()


def test_partial_name_not_matched():
    from collectors.analyst import match_subnets

    result = match_subnets("Grad is a word but not a subnet", REGISTRY)
    assert result == set()


@pytest.mark.asyncio
async def test_collect_inserts_one_row_per_matched_subnet(db):
    from collectors.analyst import AnalystCollector

    posted_at = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)
    await add_analyst_handle(db, "db_added")

    with patch("collectors.analyst.config.ANALYST_HANDLES", ["config_added"]), \
            patch(
                "collectors.analyst._scrape_tweets",
                AsyncMock(side_effect=[
                    [{"url": "https://x.com/a/status/1",
                      "text": "SN3 and Gradients are moving",
                      "posted_at": posted_at}],
                    [],
                ]),
            ), \
            patch("collectors.analyst.asyncio.sleep", AsyncMock()):
        inserted = await AnalystCollector.collect(db, REGISTRY)

    assert inserted == 2
    mentions_sn3 = await get_analyst_mentions_for_netuid(db, 3)
    mentions_sn56 = await get_analyst_mentions_for_netuid(db, 56)
    assert len(mentions_sn3) == 1
    assert len(mentions_sn56) == 1
    assert mentions_sn3[0]["tweet_url"] == "https://x.com/a/status/1"
    assert mentions_sn56[0]["tweet_url"] == "https://x.com/a/status/1"
