import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from typing import Optional
import bittensor as bt
from models import SubnetSnapshot
from utils import aiohttp_session
import config

logger = logging.getLogger(__name__)

# Singleton — initialized by main.py at startup
_subtensor: Optional[bt.AsyncSubtensor] = None


async def init_subtensor() -> None:
    global _subtensor
    _subtensor = bt.AsyncSubtensor(network=config.BITTENSOR_NETWORK)
    await _subtensor.initialize()
    logger.info("[STARTUP] bt.AsyncSubtensor initialized (network=%s)", config.BITTENSOR_NETWORK)


async def close_subtensor() -> None:
    global _subtensor
    if _subtensor is not None:
        await _subtensor.close()
        _subtensor = None


async def fetch_tao_usd_price() -> Optional[float]:
    """Fetch TAO/USD price from CoinGecko."""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bittensor&vs_currencies=usd"
    try:
        async with aiohttp_session() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json()
                return float(data["bittensor"]["usd"])
    except Exception as exc:
        logger.warning("[COLLECTOR] coingecko_price_failed error=%s", exc)
        return None


class ChainCollector:
    @staticmethod
    async def collect() -> list[SubnetSnapshot]:
        """
        Fetch all subnet data from the Bittensor chain.
        Returns one SubnetSnapshot per active subnet with chain + price data.
        """
        if _subtensor is None:
            logger.error("[COLLECTOR] chain: subtensor not initialized")
            return []

        try:
            dynamic_list, info_list, tao_usd = await asyncio.gather(
                _subtensor.all_subnets(),
                _subtensor.get_all_subnets_info(),
                fetch_tao_usd_price(),
            )
        except Exception as exc:
            logger.error("[COLLECTOR] chain_collect_failed error=%s", exc)
            return []

        # Build lookup by netuid
        info_by_netuid: dict[int, object] = {i.netuid: i for i in (info_list or [])}

        now = datetime.now(timezone.utc)
        snapshots: list[SubnetSnapshot] = []

        for dyn in (dynamic_list or []):
            try:
                info = info_by_netuid.get(dyn.netuid)
                daily_em = dyn.tao_in_emission.tao * config.BLOCKS_PER_DAY
                tao_in = dyn.tao_in.tao
                mcap_usd = (tao_in * tao_usd) if tao_usd is not None else None

                snap = SubnetSnapshot(
                    netuid=dyn.netuid,
                    polled_at=now,
                    alpha_price_tao=dyn.price.tao,
                    alpha_mcap_tao=tao_in,
                    alpha_mcap_usd=mcap_usd,
                    volume_24h_alpha=dyn.subnet_volume.tao,
                    tao_usd_price=tao_usd,
                    daily_emission_tao=daily_em,
                    owner_coldkey=getattr(dyn, "owner_coldkey", None),
                    n_neurons=info.subnetwork_n if info else None,
                    reg_cost_tao=info.burn.tao if info else None,
                )
                snapshots.append(snap)
            except Exception as exc:
                logger.warning("[COLLECTOR] chain_subnet_failed netuid=%s error=%s",
                               getattr(dyn, "netuid", "?"), exc)

        # Assign emission ranks (1 = highest daily_emission_tao)
        valid = [(i, s) for i, s in enumerate(snapshots)
                 if s.daily_emission_tao is not None]
        valid.sort(key=lambda x: x[1].daily_emission_tao, reverse=True)
        for rank, (idx, _) in enumerate(valid, start=1):
            snapshots[idx].emission_rank = rank

        ok = sum(1 for s in snapshots if s.alpha_price_tao is not None)
        logger.info("[COLLECTOR] name=chain ok=%d errors=%d",
                    ok, len(snapshots) - ok)
        return snapshots
