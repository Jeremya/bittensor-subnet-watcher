import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from collectors.chain import ChainCollector, fetch_tao_usd_price


def make_dynamic_info(netuid: int, price_tao: float = 0.013,
                       tao_in: float = 32000.0, emission_tao: float = 0.006,
                       volume: float = 700000.0) -> MagicMock:
    m = MagicMock()
    m.netuid = netuid
    m.subnet_name = f"Subnet{netuid}"
    m.price = MagicMock(); m.price.tao = price_tao
    m.tao_in = MagicMock(); m.tao_in.tao = tao_in
    m.tao_in_emission = MagicMock(); m.tao_in_emission.tao = emission_tao
    m.subnet_volume = MagicMock(); m.subnet_volume.tao = volume
    m.owner_coldkey = "5FakeKey"
    m.is_dynamic = True
    m.subnet_identity = MagicMock()
    m.subnet_identity.github_repo = "https://github.com/example/sn"
    m.subnet_identity.subnet_url = "https://example.com"
    return m


def make_subnet_info(netuid: int, n: int = 256, burn_tao: float = 0.001) -> MagicMock:
    m = MagicMock()
    m.netuid = netuid
    m.subnetwork_n = n
    m.burn = MagicMock(); m.burn.tao = burn_tao
    m.owner_ss58 = "5FakeOwner"
    return m


def make_balance(tao: float) -> MagicMock:
    m = MagicMock()
    m.tao = tao
    return m


@pytest.fixture
def mock_subtensor():
    with patch("collectors.chain._subtensor") as mock_sub:
        mock_sub.all_subnets = AsyncMock(return_value=[
            make_dynamic_info(1), make_dynamic_info(64, price_tao=0.086, tao_in=216000.0)
        ])
        mock_sub.get_all_subnets_info = AsyncMock(return_value=[
            make_subnet_info(1), make_subnet_info(64)
        ])
        yield mock_sub


async def test_collect_returns_snapshots(mock_subtensor):
    with patch("collectors.chain.fetch_tao_usd_price", AsyncMock(return_value=300.0)):
        snapshots = await ChainCollector.collect()
    assert len(snapshots) == 2
    sn1 = next(s for s in snapshots if s.netuid == 1)
    assert sn1.alpha_price_tao == pytest.approx(0.013)
    assert sn1.tao_usd_price == 300.0
    assert sn1.daily_emission_tao == pytest.approx(0.006 * 7200, rel=0.01)
    assert sn1.n_neurons == 256


async def test_collect_assigns_emission_rank(mock_subtensor):
    with patch("collectors.chain.fetch_tao_usd_price", AsyncMock(return_value=300.0)):
        snapshots = await ChainCollector.collect()
    # SN64 has same emission as SN1 in mock — both get rank assigned
    ranks = {s.netuid: s.emission_rank for s in snapshots}
    assert set(ranks.values()) == {1, 2}  # ranks 1 and 2 assigned


async def test_collect_handles_subtensor_exception():
    with patch("collectors.chain._subtensor") as mock_sub:
        mock_sub.all_subnets = AsyncMock(side_effect=Exception("gRPC error"))
        mock_sub.get_all_subnets_info = AsyncMock(return_value=[])
        with patch("collectors.chain.fetch_tao_usd_price", AsyncMock(return_value=300.0)):
            snapshots = await ChainCollector.collect()
    assert snapshots == []


async def test_collect_recovers_from_missing_alpha_sqrt_price_storage():
    fallback_dynamic = [
        make_dynamic_info(1),
        make_dynamic_info(64, price_tao=0.086, tao_in=216000.0),
    ]
    query = MagicMock()
    query.decode.return_value = [{"netuid": 1}, {"netuid": 64}]

    with patch("collectors.chain._subtensor") as mock_sub:
        mock_sub.all_subnets = AsyncMock(
            side_effect=ValueError('Storage function "Swap.AlphaSqrtPrice" not found')
        )
        mock_sub.get_all_subnets_info = AsyncMock(return_value=[
            make_subnet_info(1), make_subnet_info(64)
        ])
        mock_sub.determine_block_hash = AsyncMock(return_value="0xabc")
        mock_sub.substrate.runtime_call = AsyncMock(return_value=query)
        mock_sub.get_subnet_price = AsyncMock(side_effect=lambda netuid: make_balance({
            1: 0.0125,
            64: 0.075,
        }[netuid]))

        with (
            patch("collectors.chain.DynamicInfo", create=True) as mock_dynamic_cls,
            patch("collectors.chain.fetch_tao_usd_price", AsyncMock(return_value=300.0)),
        ):
            mock_dynamic_cls.list_from_dicts.return_value = fallback_dynamic
            snapshots = await ChainCollector.collect()

    assert len(snapshots) == 2
    assert {s.netuid for s in snapshots} == {1, 64}
    assert next(s for s in snapshots if s.netuid == 1).alpha_price_tao == pytest.approx(0.0125)
    assert next(s for s in snapshots if s.netuid == 64).alpha_price_tao == pytest.approx(0.075)
    mock_sub.get_subnet_price.assert_any_await(1)
    mock_sub.get_subnet_price.assert_any_await(64)
    mock_sub.substrate.runtime_call.assert_awaited_once_with(
        api="SubnetInfoRuntimeApi",
        method="get_all_dynamic_info",
        block_hash="0xabc",
    )


async def test_fetch_tao_usd_price_happy_path():
    mock_response = MagicMock()
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)
    mock_response.json = AsyncMock(return_value={"bittensor": {"usd": 299.67}})
    mock_response.raise_for_status = MagicMock()

    mock_session = MagicMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.get = MagicMock(return_value=mock_response)

    with patch("collectors.chain.aiohttp.ClientSession", return_value=mock_session):
        price = await fetch_tao_usd_price()
    assert price == pytest.approx(299.67)


async def test_fetch_tao_usd_price_returns_none_on_error():
    with patch("collectors.chain.aiohttp.ClientSession") as mock_cls:
        mock_cls.return_value.__aenter__ = AsyncMock(side_effect=Exception("timeout"))
        price = await fetch_tao_usd_price()
    assert price is None
