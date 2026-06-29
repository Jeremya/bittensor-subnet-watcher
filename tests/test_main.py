import pytest
import sys
import importlib
from datetime import datetime, timezone
from unittest.mock import ANY, patch, AsyncMock, MagicMock

from models import SubnetSnapshot


class ItemOnlyRow:
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]


def test_validate_config_called_at_startup(monkeypatch):
    """main.py must call validate_config() before anything else."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")
    with pytest.raises(SystemExit) as exc_info:
        import main
        importlib.reload(main)
    # sys.exit(1) called by validate_config
    assert exc_info.value.code == 1


def load_main_with_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    import main

    return importlib.reload(main)


@pytest.mark.asyncio
async def test_github_collect_updates_snapshot_and_category(monkeypatch):
    main = load_main_with_env(monkeypatch)
    now = datetime(2026, 4, 21, tzinfo=timezone.utc)
    main._db = AsyncMock()
    main._db.execute = AsyncMock()
    main._db.commit = AsyncMock()

    with patch.object(main, "get_registry", AsyncMock(return_value={3: {"github_url": "https://github.com/org/repo"}})), \
            patch.object(main.GitHubCollector, "collect", AsyncMock(return_value={
                3: {
                    "gh_last_push": now,
                    "gh_stars": 10,
                    "gh_forks": 2,
                    "gh_open_issues": 1,
                    "category": "AI Training",
                }
            })), \
            patch.object(main, "update_registry_category", AsyncMock()) as update_category:
        await main.github_collect()

    main._db.execute.assert_awaited()
    update_category.assert_awaited_once_with(main._db, 3, "AI Training", confirmed=False)


@pytest.mark.asyncio
async def test_analyst_collect_runs_collector_and_alerts(monkeypatch):
    main = load_main_with_env(monkeypatch)
    main._db = AsyncMock()
    main._telegram = None

    with patch.object(main, "get_registry", AsyncMock(return_value={3: {"name": "Templar"}})), \
            patch.object(main.AnalystCollector, "collect", AsyncMock(return_value=1)) as collect_mock, \
            patch.object(main, "fire_analyst_alerts", AsyncMock(return_value=[])) as fire_mock:
        await main.analyst_collect()

    collect_mock.assert_awaited_once_with(main._db, {3: {"name": "Templar"}})
    fire_mock.assert_awaited_once_with(main._db, {3: {"name": "Templar"}})


@pytest.mark.asyncio
async def test_poll_cycle_calls_evaluate_convergence(monkeypatch):
    main = load_main_with_env(monkeypatch)
    main._db = AsyncMock()
    main._telegram = None
    main._cycle_count = 0
    main.config.WALLET_COLDKEYS = []

    with patch.object(main.ChainCollector, "collect", AsyncMock(return_value=[])), \
            patch.object(main, "get_registry", AsyncMock(return_value={})), \
            patch.object(main.XCollector, "collect", AsyncMock(return_value={})), \
            patch.object(main, "get_latest_snapshots", AsyncMock(return_value=[])), \
            patch.object(main, "get_snapshots_for_netuid", AsyncMock(return_value=[])), \
            patch.object(main, "score_snapshots", MagicMock()), \
            patch.object(main, "evaluate_alerts", AsyncMock(return_value=[])), \
            patch.object(main, "evaluate_convergence", AsyncMock(return_value=[])) as convergence_mock, \
            patch.object(main, "get_unsent_alerts", AsyncMock(return_value=[])):
        await main.poll_cycle()

    convergence_mock.assert_awaited_once_with(main._db, {})


@pytest.mark.asyncio
async def test_poll_cycle_scores_emergence_with_richer_history(monkeypatch):
    main = load_main_with_env(monkeypatch)
    from engine import signals

    main._db = AsyncMock()
    main._telegram = None
    main._cycle_count = 0
    main.config.WALLET_COLDKEYS = []
    recent_alert_types_mock = AsyncMock(return_value={})
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snap = SubnetSnapshot(
        netuid=42,
        polled_at=now,
        reg_cost_tao=8.0,
        n_neurons=240,
        max_allowed_uids=256,
        alpha_mcap_tao=1000.0,
        net_tao_flow_tao=6.0,
    )
    hist_row = {
        "netuid": 42,
        "polled_at": now.isoformat(),
        "alpha_price_tao": 1.0,
        "alpha_mcap_tao": 1000.0,
        "emission_rank": 10,
        "net_tao_flow_tao": 1.0,
        "reg_cost_tao": 2.0,
        "n_neurons": 100,
        "max_allowed_uids": 256,
    }

    with patch.object(main.ChainCollector, "collect", AsyncMock(return_value=[snap])), \
            patch.object(main, "get_registry", AsyncMock(return_value={})), \
            patch.object(main.XCollector, "collect", AsyncMock(return_value={})), \
            patch.object(main, "get_latest_snapshots", AsyncMock(return_value=[])), \
            patch.object(main, "get_snapshots_for_netuid", AsyncMock(return_value=[hist_row])), \
            patch.object(main, "get_recent_alert_types_per_netuid", recent_alert_types_mock), \
            patch.object(main, "get_active_analyst_coverage_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_recent_milestone_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_emergence_age_context", AsyncMock(return_value={42: now})), \
            patch.object(main, "score_snapshots", MagicMock()), \
            patch.object(main, "score_emergence", MagicMock()) as score_emergence_mock, \
            patch.object(main, "insert_snapshot", AsyncMock()), \
            patch.object(main, "evaluate_alerts", AsyncMock(return_value=[])), \
            patch.object(main, "evaluate_convergence", AsyncMock(return_value=[])), \
            patch.object(main, "get_unsent_alerts", AsyncMock(return_value=[])):
        await main.poll_cycle()

    score_emergence_mock.assert_called_once_with([snap], ANY, {42: now}, now=ANY)
    history = score_emergence_mock.call_args.args[1][42][0]
    assert history.reg_cost_tao == 2.0
    assert history.n_neurons == 100
    assert history.max_allowed_uids == 256
    assert recent_alert_types_mock.await_args.args[1] == signals.SCORING_ALERT_TYPES


@pytest.mark.asyncio
async def test_poll_cycle_passes_historical_alpha_price_to_scoring(monkeypatch):
    main = load_main_with_env(monkeypatch)

    main._db = AsyncMock()
    main._telegram = None
    main._cycle_count = 0
    main.config.WALLET_COLDKEYS = []
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snap = SubnetSnapshot(netuid=42, polled_at=now, alpha_price_tao=1.2)
    hist_row = ItemOnlyRow(
        {
            "netuid": 42,
            "polled_at": now.isoformat(),
            "alpha_price_tao": 1.0,
            "alpha_mcap_tao": 1000.0,
            "emission_rank": 10,
            "net_tao_flow_tao": 1.0,
            "reg_cost_tao": 2.0,
            "n_neurons": 100,
            "max_allowed_uids": 256,
        }
    )

    with patch.object(main.ChainCollector, "collect", AsyncMock(return_value=[snap])), \
            patch.object(main, "get_registry", AsyncMock(return_value={})), \
            patch.object(main.XCollector, "collect", AsyncMock(return_value={})), \
            patch.object(main, "get_latest_snapshots", AsyncMock(return_value=[])), \
            patch.object(main, "get_snapshots_for_netuid", AsyncMock(return_value=[hist_row])), \
            patch.object(main, "get_recent_alert_types_per_netuid", AsyncMock(return_value={})), \
            patch.object(main, "get_active_analyst_coverage_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_recent_milestone_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_emergence_age_context", AsyncMock(return_value={})), \
            patch.object(main, "score_snapshots", MagicMock()) as score_snapshots_mock, \
            patch.object(main, "score_emergence", MagicMock()), \
            patch.object(main, "insert_snapshot", AsyncMock()), \
            patch.object(main, "evaluate_alerts", AsyncMock(return_value=[])), \
            patch.object(main, "evaluate_convergence", AsyncMock(return_value=[])), \
            patch.object(main, "get_unsent_alerts", AsyncMock(return_value=[])):
        await main.poll_cycle()

    history = score_snapshots_mock.call_args.args[1][42][0]
    assert history.alpha_price_tao == 1.0


@pytest.mark.asyncio
async def test_poll_cycle_scores_emergence_before_swing_scoring(monkeypatch):
    main = load_main_with_env(monkeypatch)

    main._db = AsyncMock()
    main._telegram = None
    main._cycle_count = 0
    main.config.WALLET_COLDKEYS = []
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snap = SubnetSnapshot(netuid=42, polled_at=now, alpha_price_tao=1.2)

    def mark_emergence(snapshots, history_by_netuid, age_ctx, now):
        snapshots[0].emergence_stage = "nascent"
        snapshots[0].emergence_score = 76.0

    def assert_emergence_present(snapshots, *args, **kwargs):
        assert snapshots[0].emergence_stage == "nascent"
        assert snapshots[0].emergence_score == 76.0

    with patch.object(main.ChainCollector, "collect", AsyncMock(return_value=[snap])), \
            patch.object(main, "get_registry", AsyncMock(return_value={})), \
            patch.object(main.XCollector, "collect", AsyncMock(return_value={})), \
            patch.object(main, "get_latest_snapshots", AsyncMock(return_value=[])), \
            patch.object(main, "get_snapshots_for_netuid", AsyncMock(return_value=[])), \
            patch.object(main, "get_recent_alert_types_per_netuid", AsyncMock(return_value={})), \
            patch.object(main, "get_active_analyst_coverage_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_recent_milestone_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_emergence_age_context", AsyncMock(return_value={42: now})), \
            patch.object(main, "score_emergence", MagicMock(side_effect=mark_emergence)), \
            patch.object(main, "score_snapshots", MagicMock(side_effect=assert_emergence_present)), \
            patch.object(main, "insert_snapshot", AsyncMock()), \
            patch.object(main, "evaluate_alerts", AsyncMock(return_value=[])), \
            patch.object(main, "evaluate_convergence", AsyncMock(return_value=[])), \
            patch.object(main, "get_unsent_alerts", AsyncMock(return_value=[])):
        await main.poll_cycle()


@pytest.mark.asyncio
async def test_poll_cycle_preserves_previous_price_for_alert_context(monkeypatch):
    main = load_main_with_env(monkeypatch)

    main._db = AsyncMock()
    main._telegram = None
    main._cycle_count = 0
    main.config.WALLET_COLDKEYS = []
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    snap = SubnetSnapshot(
        netuid=42,
        polled_at=now,
        alpha_price_tao=1.2,
        tao_in_tao=1060.0,
        daily_emission_tao=0.0,
    )
    previous_row = {
        "netuid": 42,
        "polled_at": now.isoformat(),
        "alpha_price_tao": 1.0,
        "alpha_mcap_tao": 1000.0,
        "tao_in_tao": 1000.0,
        "emission_rank": 10,
        "gh_last_push": None,
        "gh_stars": 0,
        "gh_forks": 0,
        "gh_open_issues": 0,
        "owner_coldkey": "coldkey",
        "reg_cost_tao": 2.0,
        "max_allowed_uids": 256,
    }

    with patch.object(main.ChainCollector, "collect", AsyncMock(return_value=[snap])), \
            patch.object(main, "get_registry", AsyncMock(return_value={})), \
            patch.object(main.XCollector, "collect", AsyncMock(return_value={})), \
            patch.object(main, "get_latest_snapshots", AsyncMock(return_value=[previous_row])), \
            patch.object(main, "get_snapshots_for_netuid", AsyncMock(return_value=[])), \
            patch.object(main, "get_recent_alert_types_per_netuid", AsyncMock(return_value={})), \
            patch.object(main, "get_active_analyst_coverage_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_recent_milestone_netuids", AsyncMock(return_value=set())), \
            patch.object(main, "get_emergence_age_context", AsyncMock(return_value={})), \
            patch.object(main, "score_snapshots", MagicMock()), \
            patch.object(main, "score_emergence", MagicMock()), \
            patch.object(main, "insert_snapshot", AsyncMock()), \
            patch.object(main, "evaluate_alerts", AsyncMock(return_value=[])) as alerts_mock, \
            patch.object(main, "evaluate_convergence", AsyncMock(return_value=[])), \
            patch.object(main, "get_unsent_alerts", AsyncMock(return_value=[])):
        await main.poll_cycle()

    prev_snaps_obj = alerts_mock.await_args.args[3]
    assert prev_snaps_obj[42].alpha_price_tao == 1.0
