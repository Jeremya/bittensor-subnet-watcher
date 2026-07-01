import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from typing import Optional
import bittensor as bt
from bittensor.core.chain_data.dynamic_info import DynamicInfo
from models import SubnetSnapshot
from utils import aiohttp_session
import config

logger = logging.getLogger(__name__)

# Singleton — initialized by main.py at startup
_subtensor: Optional[bt.AsyncSubtensor] = None

_ALPHA_SQRT_PRICE_STORAGE = 'Swap.AlphaSqrtPrice'


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


def _is_missing_alpha_sqrt_price_storage(exc: Exception) -> bool:
    message = str(exc)
    return _ALPHA_SQRT_PRICE_STORAGE in message and "not found" in message


async def _all_subnets_without_bulk_prices() -> list[object]:
    """Fetch dynamic subnet info without Bittensor's Swap.AlphaSqrtPrice query_map."""
    if _subtensor is None:
        return []

    block_hash = await _subtensor.determine_block_hash(
        block=None,
        block_hash=None,
        reuse_block=False,
    )
    query = await _subtensor.substrate.runtime_call(
        api="SubnetInfoRuntimeApi",
        method="get_all_dynamic_info",
        block_hash=block_hash,
    )
    dynamic_list = DynamicInfo.list_from_dicts(query.decode())
    await _hydrate_dynamic_prices(dynamic_list)
    return dynamic_list


async def _hydrate_dynamic_prices(dynamic_list: list[object]) -> None:
    if _subtensor is None:
        return

    semaphore = asyncio.Semaphore(16)

    async def hydrate_one(dyn: object) -> None:
        netuid = getattr(dyn, "netuid", None)
        if netuid is None:
            return

        try:
            async with semaphore:
                dyn.price = await _subtensor.get_subnet_price(netuid)
        except Exception as exc:
            logger.debug(
                "[COLLECTOR] fallback_subnet_price_failed netuid=%s error=%s",
                netuid,
                exc,
            )

    await asyncio.gather(*(hydrate_one(dyn) for dyn in dynamic_list))


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

        dynamic_result, info_result, tao_usd_result = await asyncio.gather(
            _subtensor.all_subnets(),
            _subtensor.get_all_subnets_info(),
            fetch_tao_usd_price(),
            return_exceptions=True,
        )

        if isinstance(dynamic_result, Exception):
            if _is_missing_alpha_sqrt_price_storage(dynamic_result):
                logger.warning(
                    "[COLLECTOR] chain_bulk_price_storage_missing fallback=dynamic_runtime_api error=%s",
                    dynamic_result,
                )
                try:
                    dynamic_list = await _all_subnets_without_bulk_prices()
                except Exception as exc:
                    logger.error("[COLLECTOR] chain_dynamic_fallback_failed error=%s", exc)
                    return []
            else:
                logger.error("[COLLECTOR] chain_collect_failed error=%s", dynamic_result)
                return []
        else:
            dynamic_list = dynamic_result

        if isinstance(info_result, Exception):
            logger.warning("[COLLECTOR] chain_subnet_info_failed error=%s", info_result)
            info_list = []
        else:
            info_list = info_result

        if isinstance(tao_usd_result, Exception):
            logger.warning("[COLLECTOR] tao_usd_price_failed error=%s", tao_usd_result)
            tao_usd = None
        else:
            tao_usd = tao_usd_result

        # Build lookup by netuid
        info_by_netuid: dict[int, object] = {i.netuid: i for i in (info_list or [])}

        now = datetime.now(timezone.utc)
        snapshots: list[SubnetSnapshot] = []

        for dyn in (dynamic_list or []):
            try:
                info = info_by_netuid.get(dyn.netuid)
                daily_em = dyn.tao_in_emission.tao * config.BLOCKS_PER_DAY
                tao_in = dyn.tao_in.tao
                total_alpha = dyn.alpha_in.tao + dyn.alpha_out.tao
                price_tao = dyn.price.tao
                mcap_tao = total_alpha * price_tao
                mcap_usd = (mcap_tao * tao_usd) if tao_usd is not None else None
                reference_trade_tao = config.TRADABILITY_REFERENCE_TAO
                buy_slippage_pct = None
                sell_slippage_pct = None
                try:
                    buy_slippage_pct = dyn.tao_to_alpha_with_slippage(
                        reference_trade_tao,
                        percentage=True,
                    )
                    reference_alpha = dyn.tao_to_alpha(reference_trade_tao)
                    sell_slippage_pct = dyn.alpha_to_tao_with_slippage(
                        reference_alpha,
                        percentage=True,
                    )
                except Exception as exc:
                    logger.debug(
                        "[COLLECTOR] tradability_slippage_failed netuid=%s error=%s",
                        getattr(dyn, "netuid", "?"),
                        exc,
                    )

                snap = SubnetSnapshot(
                    netuid=dyn.netuid,
                    polled_at=now,
                    alpha_price_tao=price_tao,
                    alpha_mcap_tao=mcap_tao,
                    alpha_mcap_usd=mcap_usd,
                    tao_in_tao=tao_in,
                    volume_24h_alpha=dyn.subnet_volume.tao,
                    buy_slippage_pct=buy_slippage_pct,
                    sell_slippage_pct=sell_slippage_pct,
                    tao_usd_price=tao_usd,
                    daily_emission_tao=daily_em,
                    owner_coldkey=getattr(dyn, "owner_coldkey", None),
                    n_neurons=info.subnetwork_n if info else None,
                    max_allowed_uids=info.max_n if info else None,
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
