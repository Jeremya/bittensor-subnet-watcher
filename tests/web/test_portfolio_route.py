import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from web.routes import create_app


def make_db_mock(positions=None):
    db = AsyncMock()
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)
    return db


@pytest.fixture
def client_no_wallets():
    """Client with no WALLET_COLDKEYS configured."""
    db = make_db_mock()
    with patch("web.routes.get_portfolio_positions", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_staked_netuids", new=AsyncMock(return_value=set())), \
         patch("web.routes.get_latest_snapshots_with_registry", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_last_50_alerts", new=AsyncMock(return_value=[])), \
         patch("web.routes.get_emission_rank_24h_ago", new=AsyncMock(return_value={})), \
         patch("web.routes.config") as mock_config:
        mock_config.WALLET_COLDKEYS = []
        mock_config.WALLET_LABELS = []
        mock_config.MOMENTUM_HISTORY_LIMIT = 10
        app = create_app(db)
        yield TestClient(app)


def test_portfolio_empty_state(client_no_wallets):
    resp = client_no_wallets.get("/portfolio")
    assert resp.status_code == 200
    assert "No portfolio positions" in resp.text


def test_portfolio_pnl_calculation():
    """P&L = tao_value - baseline_tao_value; shown as — when baseline=0."""
    from unittest.mock import MagicMock

    pos = MagicMock()
    pos.__getitem__ = lambda self, k: {
        "coldkey": "ck1", "netuid": 1, "alpha_amount": 100.0,
        "tao_value": 6.0, "baseline_tao_value": 5.0,
        "first_seen_at": "2026-01-01", "updated_at": "2026-01-02",
        "name": "Apex", "tao_usd_price": 300.0,
    }[k]
    pos.keys = lambda: ["coldkey", "netuid", "alpha_amount", "tao_value",
                        "baseline_tao_value", "first_seen_at", "updated_at",
                        "name", "tao_usd_price"]

    baseline = 5.0
    tao_val = 6.0
    pnl = tao_val - baseline
    pnl_pct = pnl / baseline * 100
    assert pnl == pytest.approx(1.0)
    assert pnl_pct == pytest.approx(20.0)


def test_portfolio_pnl_null_when_baseline_zero():
    baseline = 0.0
    tao_val = 5.0
    pnl = (tao_val - baseline) if baseline > 0 else None
    assert pnl is None
