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


# AnalystCollector was removed 2026-07-01 with the X scraping cut; mentions are
# now hand-curated (see engine/mentions.py). match_subnets tests above remain.
