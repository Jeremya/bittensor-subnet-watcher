import pytest
import aiosqlite
import config
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
from models import SubnetSnapshot, AlertRecord
from db.database import SCHEMA_SQL
from engine.alerts import (
    check_emission_divergence,
    check_dead_github,
    check_emission_drop,
    check_github_spike,
    check_social_silence,
    check_new_entry,
    check_ownership_transfer,
    check_tao_outflow,
    check_whale_inflow,
    check_flow_impulse,
    check_emission_near_zero,
    check_liquidity_floor,
    check_hyperparameter_change,
    evaluate_alerts,
)


def now(): return datetime.now(timezone.utc)


def make_snap(netuid: int, **kwargs) -> SubnetSnapshot:
    return SubnetSnapshot(netuid=netuid, polled_at=now(), **kwargs)


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


# ── Individual checks ─────────────────────────────────────────────────────────

def test_emission_divergence_fires():
    snap = make_snap(1, emission_rank=3, alpha_mcap_tao=32000.0)
    # mcap_rank=18 → ratio=6.0 > 1.5
    result = check_emission_divergence(snap, emission_rank=3, mcap_rank=18)
    assert result is not None
    assert result.alert_type == "emission_divergence"
    assert result.current_value == pytest.approx(6.0)


def test_emission_divergence_does_not_fire_below_threshold():
    result = check_emission_divergence(make_snap(1), emission_rank=5, mcap_rank=6)
    assert result is None  # mcap_rank/emission_rank = 6/5 = 1.2 < 1.5


def test_dead_github_fires():
    old_push = now() - timedelta(days=70)
    snap = make_snap(1, gh_last_push=old_push, alpha_mcap_usd=1_000_000)
    result = check_dead_github(snap)
    assert result is not None
    assert result.alert_type == "dead_github"


def test_dead_github_does_not_fire_below_mcap_threshold():
    old_push = now() - timedelta(days=70)
    snap = make_snap(1, gh_last_push=old_push, alpha_mcap_usd=100_000)  # < $500K
    assert check_dead_github(snap) is None


def test_emission_drop_fires():
    prev = make_snap(1, emission_rank=5)
    prev.polled_at = now() - timedelta(hours=23)
    curr = make_snap(1, emission_rank=8)  # dropped 3 ranks
    result = check_emission_drop(curr, prev)
    assert result is not None
    assert result.alert_type == "emission_drop"


def test_github_spike_fires():
    prev = make_snap(1, gh_stars=50, gh_forks=10)
    curr = make_snap(1, gh_stars=105, gh_forks=10)  # stars doubled
    result = check_github_spike(curr, prev)
    assert result is not None
    assert result.alert_type == "github_spike"


def test_social_silence_fires():
    old_tweet = now() - timedelta(days=20)
    snap = make_snap(1, x_last_tweet=old_tweet)
    result = check_social_silence(snap)
    assert result is not None
    assert result.alert_type == "social_silence"


def test_new_entry_fires_for_unknown_netuid():
    snap = make_snap(999)
    result = check_new_entry(snap, known_netuids={1, 2, 3})
    assert result is not None
    assert result.alert_type == "new_entry"


def test_new_entry_does_not_fire_for_known():
    snap = make_snap(1)
    assert check_new_entry(snap, known_netuids={1, 2, 3}) is None


# ── check_ownership_transfer ──────────────────────────────────────────────────

def test_ownership_transfer_fires_on_coldkey_change():
    prev = make_snap(1, owner_coldkey="5ABC123def456789abc")
    curr = make_snap(1, owner_coldkey="5XYZ987uvw654321xyz")
    result = check_ownership_transfer(curr, prev)
    assert result is not None
    assert result.alert_type == "ownership_transfer"
    assert "5ABC123d" in result.description
    assert "5XYZ987u" in result.description


def test_ownership_transfer_no_alert_when_same():
    coldkey = "5ABC123def456789abc"
    prev = make_snap(1, owner_coldkey=coldkey)
    curr = make_snap(1, owner_coldkey=coldkey)
    assert check_ownership_transfer(curr, prev) is None


def test_ownership_transfer_no_alert_when_prev_none():
    """Prevents false alerts on first snapshot after field was populated."""
    curr = make_snap(1, owner_coldkey="5ABC123def456789abc")
    prev = make_snap(1, owner_coldkey=None)
    assert check_ownership_transfer(curr, prev) is None


def test_ownership_transfer_no_alert_when_current_none():
    """Prevents false alerts when chain temporarily omits owner_coldkey."""
    prev = make_snap(1, owner_coldkey="5ABC123def456789abc")
    curr = make_snap(1, owner_coldkey=None)
    assert check_ownership_transfer(curr, prev) is None


# ── Capital-protection checks ─────────────────────────────────────────────────

def test_tao_outflow_fires_on_large_negative_flow():
    # 4% outflow > 3% threshold
    snap = make_snap(1, net_tao_flow_tao=-40.0, alpha_mcap_tao=1000.0)
    result = check_tao_outflow(snap)
    assert result is not None
    assert result.alert_type == "tao_outflow"
    assert result.current_value == pytest.approx(0.04)


def test_tao_outflow_does_not_fire_below_threshold():
    # 2% outflow < 3% threshold
    snap = make_snap(1, net_tao_flow_tao=-20.0, alpha_mcap_tao=1000.0)
    assert check_tao_outflow(snap) is None


def test_tao_outflow_does_not_fire_on_inflow():
    snap = make_snap(1, net_tao_flow_tao=50.0, alpha_mcap_tao=1000.0)
    assert check_tao_outflow(snap) is None


def test_tao_outflow_skips_when_data_missing():
    assert check_tao_outflow(make_snap(1)) is None
    assert check_tao_outflow(make_snap(1, net_tao_flow_tao=-50.0)) is None


def test_whale_inflow_fires_on_large_positive_flow():
    # 6% inflow > 5% threshold
    snap = make_snap(1, net_tao_flow_tao=60.0, alpha_mcap_tao=1000.0)
    result = check_whale_inflow(snap)
    assert result is not None
    assert result.alert_type == "whale_inflow"
    assert result.current_value == pytest.approx(0.06)


def test_whale_inflow_does_not_fire_below_threshold():
    snap = make_snap(1, net_tao_flow_tao=40.0, alpha_mcap_tao=1000.0)
    assert check_whale_inflow(snap) is None


def test_whale_inflow_does_not_fire_on_outflow():
    snap = make_snap(1, net_tao_flow_tao=-80.0, alpha_mcap_tao=1000.0)
    assert check_whale_inflow(snap) is None


def test_check_flow_impulse_builds_important_buy_alert():
    prev = make_snap(1, alpha_price_tao=1.0)
    curr = make_snap(
        1,
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=1.02,
        buy_slippage_pct=3.4,
    )

    result = check_flow_impulse(curr, prev)

    assert result is not None
    assert result.alert_type == "important_buy"
    assert result.current_value == pytest.approx(0.06)
    assert result.threshold == pytest.approx(0.05)
    assert "Important buy pressure" in result.description
    assert "not wallet-attributed" in result.description


def test_check_flow_impulse_builds_important_sell_alert():
    prev = make_snap(1, alpha_price_tao=1.0)
    curr = make_snap(
        1,
        net_tao_flow_tao=-40.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=0.985,
        sell_slippage_pct=5.2,
    )

    result = check_flow_impulse(curr, prev)

    assert result is not None
    assert result.alert_type == "important_sell"
    assert result.current_value == pytest.approx(0.04)
    assert result.threshold == pytest.approx(0.03)
    assert "Important sell pressure" in result.description
    assert "Impact score" in result.description


def test_emission_near_zero_fires():
    snap = make_snap(1, daily_emission_tao=3.0, alpha_mcap_usd=500_000.0)
    result = check_emission_near_zero(snap)
    assert result is not None
    assert result.alert_type == "emission_near_zero"
    assert result.current_value == pytest.approx(3.0)


def test_emission_near_zero_does_not_fire_above_threshold():
    snap = make_snap(1, daily_emission_tao=10.0, alpha_mcap_usd=500_000.0)
    assert check_emission_near_zero(snap) is None


def test_emission_near_zero_skips_micro_cap():
    # mcap < EMISSION_NEAR_ZERO_MIN_MCAP_USD ($100K)
    snap = make_snap(1, daily_emission_tao=1.0, alpha_mcap_usd=50_000.0)
    assert check_emission_near_zero(snap) is None


def test_liquidity_floor_fires_when_illiquid():
    # volume_tao = 0.5 * 0.1 = 0.05; ratio = 0.05/1000 = 0.005% < 0.1% floor
    snap = make_snap(1, volume_24h_alpha=0.5, alpha_price_tao=0.1,
                     alpha_mcap_tao=1000.0, alpha_mcap_usd=500_000.0)
    result = check_liquidity_floor(snap)
    assert result is not None
    assert result.alert_type == "liquidity_floor"


def test_liquidity_floor_does_not_fire_above_floor():
    # volume_tao = 2 * 0.1 = 0.2; ratio = 0.2/1000 = 0.02% -- still illiquid, use bigger volume
    # volume_tao = 20 * 0.1 = 2.0; ratio = 2/1000 = 0.2% > 0.1% floor
    snap = make_snap(1, volume_24h_alpha=20.0, alpha_price_tao=0.1,
                     alpha_mcap_tao=1000.0, alpha_mcap_usd=500_000.0)
    assert check_liquidity_floor(snap) is None


def test_liquidity_floor_skips_micro_cap():
    snap = make_snap(1, volume_24h_alpha=0.1, alpha_price_tao=0.1,
                     alpha_mcap_tao=1000.0, alpha_mcap_usd=50_000.0)
    assert check_liquidity_floor(snap) is None


def test_hyperparameter_change_fires_on_reg_cost_spike():
    prev = make_snap(1, reg_cost_tao=0.10)
    curr = make_snap(1, reg_cost_tao=0.20)  # +100% > 50% threshold
    result = check_hyperparameter_change(curr, prev)
    assert result is not None
    assert result.alert_type == "hyperparameter_change"
    assert "↑" in result.description


def test_hyperparameter_change_fires_on_reg_cost_drop():
    prev = make_snap(1, reg_cost_tao=0.10)
    curr = make_snap(1, reg_cost_tao=0.02)  # -80% > 50% threshold
    result = check_hyperparameter_change(curr, prev)
    assert result is not None
    assert "↓" in result.description


def test_hyperparameter_change_does_not_fire_within_tolerance():
    prev = make_snap(1, reg_cost_tao=0.10)
    curr = make_snap(1, reg_cost_tao=0.13)  # +30% < 50%
    assert check_hyperparameter_change(curr, prev) is None


def test_hyperparameter_change_fires_on_max_allowed_uids_change():
    prev = make_snap(1, max_allowed_uids=256)
    curr = make_snap(1, max_allowed_uids=512)
    result = check_hyperparameter_change(curr, prev)
    assert result is not None
    assert result.alert_type == "hyperparameter_change"
    assert "256" in result.description
    assert "512" in result.description


def test_hyperparameter_change_no_fire_when_unchanged():
    prev = make_snap(1, reg_cost_tao=0.10, max_allowed_uids=256)
    curr = make_snap(1, reg_cost_tao=0.11, max_allowed_uids=256)
    assert check_hyperparameter_change(curr, prev) is None


# ── evaluate_alerts integration ───────────────────────────────────────────────

async def test_evaluate_alerts_respects_cooldown(db):
    from db.database import insert_alert
    # Pre-fire an emission_divergence alert for netuid 1
    existing = AlertRecord(
        fired_at=now(), netuid=1, subnet_name="Apex",
        alert_type="emission_divergence", description="x", current_value=3.0, threshold=1.5
    )
    await insert_alert(db, existing)

    snap = make_snap(1, emission_rank=3, alpha_mcap_tao=32000.0, alpha_mcap_usd=5_000_000)
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}
    prev_by_netuid = {}
    known_netuids = {1}

    alerts = await evaluate_alerts(
        db, [snap], registry, prev_by_netuid, known_netuids
    )
    # Should not fire again (cooldown)
    em_div_alerts = [a for a in alerts if a.alert_type == "emission_divergence" and a.netuid == 1]
    assert len(em_div_alerts) == 0


async def test_evaluate_alerts_fires_important_buy_without_legacy_duplicate(db):
    snap = make_snap(
        1,
        net_tao_flow_tao=60.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=1.02,
        buy_slippage_pct=3.4,
    )
    prev = make_snap(1, alpha_price_tao=1.0)
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}

    alerts = await evaluate_alerts(db, [snap], registry, {1: prev}, known_netuids={1})

    alert_types = {alert.alert_type for alert in alerts}
    assert "important_buy" in alert_types
    assert "whale_inflow" not in alert_types


async def test_evaluate_alerts_fires_important_sell_without_legacy_duplicate(db):
    snap = make_snap(
        1,
        net_tao_flow_tao=-40.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
        alpha_price_tao=0.985,
        sell_slippage_pct=5.2,
    )
    prev = make_snap(1, alpha_price_tao=1.0)
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}

    alerts = await evaluate_alerts(db, [snap], registry, {1: prev}, known_netuids={1})

    alert_types = {alert.alert_type for alert in alerts}
    assert "important_sell" in alert_types
    assert "tao_outflow" not in alert_types


async def test_evaluate_alerts_suppresses_recent_flow_impulse_same_type(db):
    from db.database import insert_alert

    existing = AlertRecord(
        fired_at=now(),
        netuid=1,
        subnet_name="Apex",
        alert_type="important_buy",
        description="existing",
        current_value=0.06,
        threshold=0.05,
    )
    await insert_alert(db, existing)

    snap = make_snap(
        1,
        net_tao_flow_tao=70.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
    )
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}

    alerts = await evaluate_alerts(db, [snap], registry, {}, known_netuids={1})

    assert all(alert.alert_type != "important_buy" for alert in alerts)


async def test_evaluate_alerts_uses_flow_impulse_cooldown_instead_of_default(db):
    from db.database import insert_alert

    age_hours = config.FLOW_IMPULSE_COOLDOWN_HOURS + 0.5
    assert age_hours < config.ALERT_COOLDOWN_HOURS
    existing = AlertRecord(
        fired_at=now() - timedelta(hours=age_hours),
        netuid=1,
        subnet_name="Apex",
        alert_type="important_buy",
        description="existing",
        current_value=0.06,
        threshold=0.05,
    )
    await insert_alert(db, existing)

    snap = make_snap(
        1,
        net_tao_flow_tao=70.0,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
    )
    registry = {1: {"name": "Apex", "x_handle": None, "github_url": None}}

    alerts = await evaluate_alerts(db, [snap], registry, {}, known_netuids={1})

    important_buy_alerts = [
        alert for alert in alerts if alert.alert_type == "important_buy"
    ]
    assert len(important_buy_alerts) == 1


async def test_evaluate_alerts_fires_ownership_transfer(db):
    snap = make_snap(
        1,
        owner_coldkey="5XYZnewowner456789xyz",
        emission_rank=5,
        alpha_mcap_tao=1000.0,
        alpha_mcap_usd=100_000.0,
    )
    prev_snap = make_snap(1, owner_coldkey="5ABColdowner123abc")
    registry = {1: {"name": "TestNet", "x_handle": None, "github_url": None}}
    known_netuids = {1}

    alerts = await evaluate_alerts(
        db, [snap], registry, {1: prev_snap}, known_netuids
    )
    transfer_alerts = [a for a in alerts if a.alert_type == "ownership_transfer"]
    assert len(transfer_alerts) == 1
    assert transfer_alerts[0].netuid == 1


class RowLike:
    def __init__(self, values):
        self._values = values

    def __getitem__(self, key):
        return self._values[key]


async def test_evaluate_alerts_accepts_row_like_registry_values(db):
    snap = make_snap(1, emission_rank=3, alpha_mcap_tao=32000.0)
    registry = {1: RowLike({"name": "Apex"})}
    alerts = await evaluate_alerts(db, [snap], registry, prev_by_netuid={}, known_netuids=set())
    assert any(a.alert_type == "new_entry" for a in alerts)
    assert any(a.subnet_name == "Apex" for a in alerts)
