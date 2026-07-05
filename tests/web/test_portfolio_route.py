from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from engine import signals
from web.routes import create_app


def make_row(**overrides):
    row = {
        "coldkey": "ck1",
        "netuid": 3,
        "alpha_amount": 100.0,
        "tao_value": 6.0,
        "baseline_tao_value": 5.0,
        "name": "Templar",
        "category": "AI Training",
        "tao_usd_price": 300.0,
    }
    row.update(overrides)
    return row


@pytest.fixture
def client_with_portfolio():
    db = AsyncMock()
    with patch("web.routes.get_portfolio_positions", new=AsyncMock(return_value=[
        make_row(netuid=3, name="Templar", tao_value=6.0, baseline_tao_value=5.0),
        make_row(netuid=56, name="Gradients", tao_value=4.0, baseline_tao_value=3.0),
    ])), \
         patch("web.routes.get_latest_snapshots_with_registry", new=AsyncMock(return_value=[
             {
                 "netuid": 3,
                 "name": "Templar",
                 "category": "AI Training",
                 "composite_score": 81.0,
                 "spec421_score": 77.0,
                 "momentum_score": 68.0,
             },
             {
                 "netuid": 56,
                 "name": "Gradients",
                 "category": "Infrastructure",
                 "composite_score": 86.0,
                 "momentum_score": 76.0,
             },
             {
                 "netuid": 14,
                 "name": "Macro",
                 "category": "Data / Retrieval",
                 "composite_score": 82.0,
                 "momentum_score": 72.0,
             },
         ])), \
         patch("web.routes.get_scoring_alert_context", new=AsyncMock(return_value={})), \
         patch("web.routes.get_active_analyst_coverage_netuids", new=AsyncMock(return_value={14})), \
         patch("web.routes.get_recent_milestone_netuids", new=AsyncMock(return_value={56})), \
         patch("web.routes.build_portfolio_recommendations", return_value={
             "portfolio_actions": [
                 {
                     "netuid": 3,
                     "subnet_name": "Templar",
                     "action": "trim",
                     "confidence": "high",
                     "reasons": ["position is 60.0% of book"],
                     "score": 81.0,
                     "allocation_pct": 0.60,
                     "context": [
                         {"label": "Risk", "value": "tao_outflow", "tone": "danger"},
                         {"label": "Allocation", "value": "60.0% of portfolio", "tone": "warning"},
                     ],
                 }
             ],
             "new_candidates": [
                 {
                     "netuid": 14,
                     "subnet_name": "Macro",
                     "action": "new_buy",
                     "confidence": "medium",
                     "reasons": ["outranks weakest held name with a fresh catalyst"],
                     "score": 82.0,
                     "allocation_pct": None,
                     "context": [
                         {"label": "Context", "value": "analyst coverage active", "tone": "positive"},
                     ],
                 }
             ],
             "table_actions": {
                 3: {
                     "action": "trim",
                     "confidence": "high",
                     "reasons": ["position is 60.0% of book"],
                     "context": [
                         {"label": "Allocation", "value": "60.0% of portfolio", "tone": "warning"},
                     ],
                 },
                 56: {"action": "hold", "confidence": "low", "reasons": [], "context": []},
             },
         }), \
         patch("web.routes.get_staked_netuids", new=AsyncMock(return_value={3, 56})), \
         patch("web.routes.get_last_50_alerts", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_emission_rank_24h_ago", new=AsyncMock(return_value={})), \
         patch("web.routes.config") as mock_config:
        mock_config.WALLET_COLDKEYS = ["ck1"]
        mock_config.WALLET_LABELS = ["Main"]
        mock_config.MOMENTUM_HISTORY_LIMIT = 10
        mock_config.ANALYST_COVERAGE_DECAY_HOURS = 72
        mock_config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS = 168
        mock_config.PORTFOLIO_TRIM_MAX_ALLOC_PCT = 0.25
        mock_config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT = 0.45
        mock_config.PORTFOLIO_ADD_MIN_SCORE = 75.0
        mock_config.PORTFOLIO_NEW_BUY_MIN_SCORE = 78.0
        mock_config.PORTFOLIO_REPLACE_SCORE_MARGIN = 8.0
        mock_config.PORTFOLIO_HOLD_FLOOR_SCORE = 55.0
        app = create_app(db)
        yield TestClient(app)


def test_portfolio_empty_state():
    db = AsyncMock()
    with patch("web.routes.get_portfolio_positions", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_latest_snapshots_with_registry", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_scoring_alert_context", new=AsyncMock(return_value={})), \
         patch("web.routes.get_active_analyst_coverage_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_recent_milestone_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_staked_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_last_50_alerts", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_emission_rank_24h_ago", new=AsyncMock(return_value={})), \
         patch("web.routes.config") as mock_config:
        mock_config.WALLET_COLDKEYS = []
        mock_config.WALLET_LABELS = []
        mock_config.MOMENTUM_HISTORY_LIMIT = 10
        mock_config.ANALYST_COVERAGE_DECAY_HOURS = 72
        mock_config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS = 168
        app = create_app(db)
        client = TestClient(app)
        resp = client.get("/portfolio")
    assert resp.status_code == 200
    assert "No portfolio positions found" in resp.text


def test_portfolio_renders_action_sections(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert resp.status_code == 200
    assert "Portfolio Actions" in resp.text
    assert "New Candidates" in resp.text


def test_portfolio_renders_table_recommendations(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Recommendation" in resp.text
    assert "trim" in resp.text.lower()
    assert "hold" in resp.text.lower()


def test_portfolio_renders_spec421_score(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Spec 421" in resp.text
    assert "77.0" in resp.text


def test_portfolio_table_has_mobile_card_labels(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")

    assert 'data-label="Subnet"' in resp.text
    assert 'data-label="Spec 421"' in resp.text
    assert 'data-label="Recommendation"' in resp.text
    assert "td::before" in resp.text


def test_portfolio_renders_new_buy_candidate(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Macro" in resp.text
    assert "New Buy" in resp.text or "NEW BUY" in resp.text


def test_portfolio_renders_action_context(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Risk" in resp.text
    assert "tao_outflow" in resp.text
    assert "Allocation" in resp.text
    assert "60.0% of portfolio" in resp.text


def test_portfolio_table_recommendation_prefers_context(client_with_portfolio):
    resp = client_with_portfolio.get("/portfolio")
    assert "Allocation: 60.0% of portfolio" in resp.text


def test_portfolio_recent_alert_query_includes_flow_aliases_and_legacy_types():
    db = AsyncMock()
    recent_alerts = AsyncMock(return_value={})
    with patch("web.routes.get_portfolio_positions", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_latest_snapshots_with_registry", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_scoring_alert_context", new=recent_alerts), \
         patch("web.routes.get_active_analyst_coverage_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_recent_milestone_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_staked_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_last_50_alerts", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_emission_rank_24h_ago", new=AsyncMock(return_value={})), \
         patch("web.routes.config") as mock_config:
        mock_config.WALLET_COLDKEYS = []
        mock_config.WALLET_LABELS = []
        mock_config.MOMENTUM_HISTORY_LIMIT = 10
        mock_config.ANALYST_COVERAGE_DECAY_HOURS = 72
        mock_config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS = 168
        app = create_app(db)
        client = TestClient(app)
        resp = client.get("/portfolio")

    assert resp.status_code == 200
    alert_types = recent_alerts.await_args.args[1]
    assert alert_types == signals.SCORING_ALERT_TYPES
    for alert_type in (
        "hyperparameter_change",
        "tao_outflow",
        "important_sell",
        "whale_inflow",
        "important_buy",
        "github_spike",
    ):
        assert alert_type in alert_types
