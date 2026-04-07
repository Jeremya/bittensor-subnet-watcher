# collectors/portfolio.py
import logging
from typing import Optional
import bittensor as bt

logger = logging.getLogger(__name__)


class PortfolioCollector:
    @staticmethod
    async def collect(
        subtensor: bt.AsyncSubtensor,
        coldkeys: list[str],
        price_by_netuid: dict[int, float],
    ) -> dict[str, dict[int, dict]]:
        """
        Query stake positions for each coldkey.

        Returns {coldkey: {netuid: {"alpha_amount": float, "tao_value": float}}}
        Stakes are aggregated across all hotkeys per (coldkey, netuid).
        Coldkeys that fail are skipped with a warning log.
        """
        result: dict[str, dict[int, dict]] = {}

        for coldkey in coldkeys:
            try:
                stakes = await subtensor.get_stake_info_for_coldkey(coldkey)
            except Exception as exc:
                logger.warning("[PORTFOLIO] coldkey_failed coldkey=%.12s... error=%s", coldkey, exc)
                continue

            positions: dict[int, dict] = {}
            for s in (stakes or []):
                netuid = s.netuid
                alpha = s.stake.tao  # alpha token amount as decimal
                price = price_by_netuid.get(netuid, 0.0)
                tao_val = alpha * price

                if netuid in positions:
                    positions[netuid]["alpha_amount"] += alpha
                    positions[netuid]["tao_value"] += tao_val
                else:
                    positions[netuid] = {"alpha_amount": alpha, "tao_value": tao_val}

            result[coldkey] = positions
            logger.info("[PORTFOLIO] coldkey=%.12s... subnets=%d", coldkey, len(positions))

        return result
