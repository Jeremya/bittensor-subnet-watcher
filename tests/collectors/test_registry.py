import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import aiosqlite
from db.database import SCHEMA_SQL, get_registry
from collectors.registry import RegistryCollector

MOCK_TAOSTAT_JSON = {
    "1": {"name": "Apex", "github": "https://github.com/macrocosm-os/apex",
          "owner": "5HCF", "bittensor_id": "alpha", "twitter": "@apexteam"},
    "64": {"name": "Chutes", "github": "https://github.com/rayonlabs/chutes",
           "owner": "5Xyz", "bittensor_id": "chutes"},
}


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


def make_dynamic_info(netuid: int, name: str, github: str = "") -> MagicMock:
    m = MagicMock()
    m.netuid = netuid
    m.subnet_name = name
    m.subnet_identity = MagicMock()
    m.subnet_identity.github_repo = github
    m.subnet_identity.subnet_url = f"https://sn{netuid}.example.com"
    return m


async def test_refresh_builds_registry_from_dynamic_info(db):
    dynamic_list = [make_dynamic_info(1, "Apex", "https://github.com/macrocosm-os/apex")]
    with patch("collectors.registry.fetch_taostat_json", AsyncMock(return_value={})):
        await RegistryCollector.refresh(db, dynamic_list)
    registry = await get_registry(db)
    assert 1 in registry
    assert registry[1]["github_url"] == "https://github.com/macrocosm-os/apex"


async def test_refresh_succeeds_when_taostat_fails(db):
    dynamic_list = [make_dynamic_info(1, "Apex")]
    with patch("collectors.registry.fetch_taostat_json",
               AsyncMock(side_effect=Exception("404"))):
        await RegistryCollector.refresh(db, dynamic_list)
    registry = await get_registry(db)
    assert registry[1]["name"] == "Apex"  # row still exists, refresh did not crash


async def test_refresh_extracts_x_handle_from_taostat(db):
    dynamic_list = [make_dynamic_info(1, "Apex")]
    with patch("collectors.registry.fetch_taostat_json",
               AsyncMock(return_value=MOCK_TAOSTAT_JSON)):
        await RegistryCollector.refresh(db, dynamic_list)
    registry = await get_registry(db)
    assert registry[1]["x_handle"] == "apexteam"  # leading @ stripped


async def test_refresh_handles_missing_subnet_identity(db):
    m = MagicMock()
    m.netuid = 99
    m.subnet_name = "Unknown"
    m.subnet_identity = None
    with patch("collectors.registry.fetch_taostat_json", AsyncMock(return_value={})):
        await RegistryCollector.refresh(db, [m])
    registry = await get_registry(db)
    assert 99 in registry
    assert registry[99]["github_url"] is None
