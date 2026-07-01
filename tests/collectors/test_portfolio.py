import pytest
from unittest.mock import AsyncMock, MagicMock
from collectors.portfolio import PortfolioCollector


def make_stake_info(netuid, hotkey, alpha_amount):
    s = MagicMock()
    s.netuid = netuid
    s.hotkey_ss58 = hotkey
    s.stake = MagicMock()
    s.stake.tao = alpha_amount
    return s


@pytest.mark.asyncio
async def test_collect_basic():
    subtensor = MagicMock()
    subtensor.get_stake_info_for_coldkey = AsyncMock(return_value=[
        make_stake_info(1, "hotkey_a", 100.0),
        make_stake_info(5, "hotkey_a", 50.0),
    ])
    price_by_netuid = {1: 0.01, 5: 0.05}
    result = await PortfolioCollector.collect(subtensor, ["coldkey1"], price_by_netuid)

    assert "coldkey1" in result
    assert result["coldkey1"][1]["alpha_amount"] == pytest.approx(100.0)
    assert result["coldkey1"][1]["tao_value"] == pytest.approx(1.0)
    assert result["coldkey1"][5]["alpha_amount"] == pytest.approx(50.0)
    assert result["coldkey1"][5]["tao_value"] == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_collect_aggregates_across_hotkeys():
    """Same subnet staked via two different hotkeys — should be summed."""
    subtensor = MagicMock()
    subtensor.get_stake_info_for_coldkey = AsyncMock(return_value=[
        make_stake_info(1, "hotkey_a", 100.0),
        make_stake_info(1, "hotkey_b", 200.0),
    ])
    price_by_netuid = {1: 0.02}
    result = await PortfolioCollector.collect(subtensor, ["coldkey1"], price_by_netuid)

    assert result["coldkey1"][1]["alpha_amount"] == pytest.approx(300.0)
    assert result["coldkey1"][1]["tao_value"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_collect_missing_price_leaves_tao_value_unknown():
    subtensor = MagicMock()
    subtensor.get_stake_info_for_coldkey = AsyncMock(return_value=[
        make_stake_info(99, "hotkey_a", 500.0),
    ])
    result = await PortfolioCollector.collect(subtensor, ["coldkey1"], price_by_netuid={})

    assert result["coldkey1"][99]["alpha_amount"] == pytest.approx(500.0)
    assert result["coldkey1"][99]["tao_value"] is None


@pytest.mark.asyncio
async def test_collect_skips_failed_coldkey(caplog):
    subtensor = MagicMock()
    subtensor.get_stake_info_for_coldkey = AsyncMock(side_effect=Exception("chain error"))

    result = await PortfolioCollector.collect(subtensor, ["bad_coldkey"], {})
    assert result == {}
    assert "coldkey_failed" in caplog.text


@pytest.mark.asyncio
async def test_collect_multiple_coldkeys():
    subtensor = MagicMock()
    subtensor.get_stake_info_for_coldkey = AsyncMock(side_effect=[
        [make_stake_info(1, "hk", 10.0)],
        [make_stake_info(2, "hk", 20.0)],
    ])
    result = await PortfolioCollector.collect(
        subtensor, ["ck1", "ck2"], {1: 0.1, 2: 0.2}
    )
    assert result["ck1"][1]["tao_value"] == pytest.approx(1.0)
    assert result["ck2"][2]["tao_value"] == pytest.approx(4.0)
