import pytest
import sys
import importlib
from datetime import datetime, timezone
from unittest.mock import ANY, patch, AsyncMock, MagicMock

from models import SubnetSnapshot


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
