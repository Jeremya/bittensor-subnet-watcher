import aiohttp
import logging
from typing import Optional
import aiosqlite
from db.database import upsert_registry_entry

logger = logging.getLogger(__name__)

TAOSTAT_JSON_URL = (
    "https://raw.githubusercontent.com/taostat/subnets-infos/main/subnets.json"
)


async def fetch_taostat_json() -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(TAOSTAT_JSON_URL,
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


class RegistryCollector:
    @staticmethod
    async def refresh(db: aiosqlite.Connection, dynamic_list: list) -> None:
        """
        Rebuild subnet_registry from DynamicInfo subnet_identity.
        Supplements with taostat JSON for X handles where available.
        On taostat failure, keeps existing DB data — does not wipe.
        """
        # Try to fetch taostat JSON for supplemental data (X handles, team names)
        taostat: dict = {}
        try:
            taostat = await fetch_taostat_json()
        except Exception as exc:
            logger.warning("[COLLECTOR] registry: taostat_fetch_failed error=%s", exc)

        for dyn in dynamic_list:
            netuid = dyn.netuid
            name = dyn.subnet_name or f"SN{netuid}"
            identity = dyn.subnet_identity

            github_url: Optional[str] = None
            website: Optional[str] = None
            if identity:
                github_url = identity.github_repo or None
                website = identity.subnet_url or None

            # X handle from taostat (not in on-chain identity)
            x_handle: Optional[str] = None
            taostat_entry = taostat.get(str(netuid), {})
            if taostat_entry.get("twitter"):
                x_handle = taostat_entry["twitter"].lstrip("@")

            try:
                await upsert_registry_entry(
                    db, netuid, name, github_url, x_handle, website
                )
            except Exception as exc:
                logger.warning("[COLLECTOR] registry: upsert_failed netuid=%s error=%s",
                               netuid, exc)

        logger.info("[COLLECTOR] name=registry subnets=%d", len(dynamic_list))
