# tests/web/test_routes.py
import pytest
import aiosqlite
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from db.database import SCHEMA_SQL, insert_snapshot, insert_alert
from models import SubnetSnapshot, AlertRecord


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()
        yield conn


@pytest.fixture
def app(db):
    from web.routes import create_app
    return create_app(db)


async def test_dashboard_returns_200(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "TAO Monitor" in resp.text


async def test_dashboard_empty_state(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert "Waiting for first poll" in resp.text or "No alerts yet" in resp.text


async def test_api_snapshots_returns_json(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              composite_score=75.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["netuid"] == 1


async def test_api_alerts_returns_json(app, db):
    now = datetime.now(timezone.utc)
    await insert_alert(db, AlertRecord(
        fired_at=now, netuid=1, subnet_name="Apex",
        alert_type="new_entry", description="new", notified=True
    ))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/alerts")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
