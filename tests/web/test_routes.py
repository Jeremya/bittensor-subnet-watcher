# tests/web/test_routes.py
import pytest
import aiosqlite
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from db.database import (
    SCHEMA_SQL,
    add_analyst_handle,
    insert_alert,
    insert_analyst_mention,
    insert_milestone,
    insert_snapshot,
    update_registry_category,
    upsert_registry_entry,
)
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
    assert "Fresh subnets" in resp.text
    assert "Signal coverage" in resp.text


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


async def test_dashboard_shows_registry_name(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              composite_score=80.0,
                                              alpha_mcap_tao=5000.0))
    await upsert_registry_entry(db, 1, "Apex", None, None, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "Apex" in resp.text


async def test_dashboard_row_links_to_subnet_page(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=7, polled_at=now,
                                              composite_score=60.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert "/subnet/7" in resp.text


async def test_dashboard_shows_mcap_usd(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=1, polled_at=now,
                                              composite_score=75.0,
                                              alpha_mcap_usd=2_100_000.0))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")
    assert "$2.1M" in resp.text


async def test_subnet_detail_returns_200(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(db, SubnetSnapshot(netuid=5, polled_at=now,
                                              composite_score=72.0,
                                              emission_rank=3,
                                              alpha_mcap_tao=1000.0))
    await upsert_registry_entry(db, 5, "Templar", None, None, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/5")
    assert resp.status_code == 200
    assert "Templar" in resp.text
    assert "SN5" in resp.text
    assert "Chain Stats" in resp.text


async def test_subnet_detail_returns_404_for_unknown(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/9999")
    assert resp.status_code == 404


async def test_analysts_page_lists_config_and_db_handles(app, db):
    await add_analyst_handle(db, "db_added")
    import web.routes as routes

    original_handles = routes.config.ANALYST_HANDLES
    routes.config.ANALYST_HANDLES = ["config_added"]
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/analysts")
    finally:
        routes.config.ANALYST_HANDLES = original_handles

    assert resp.status_code == 200
    assert "@config_added" in resp.text
    assert "@db_added" in resp.text


async def test_analysts_add_and_remove_round_trip(app, db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        add_resp = await client.post("/analysts/add", data={"handle": "@new_handle"})
        remove_resp = await client.post("/analysts/remove/new_handle")
        page = await client.get("/analysts")

    assert add_resp.status_code == 303
    assert remove_resp.status_code == 303
    assert "@new_handle" not in page.text


async def test_dashboard_shows_category_and_coverage_badges(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(
        db,
        SubnetSnapshot(
            netuid=3,
            polled_at=now,
            composite_score=85.0,
            alpha_mcap_tao=5_000.0,
        ),
    )
    await upsert_registry_entry(db, 3, "Templar", None, None, None)
    await update_registry_category(db, 3, "AI Training", confirmed=True)
    await insert_analyst_mention(
        db,
        "0xai_dev",
        3,
        "https://x.com/0xai_dev/status/1",
        "SN3 is moving",
        now,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "AI Training" in resp.text
    assert "📡" in resp.text
    assert "/analysts" in resp.text


async def test_dashboard_shows_freshness_panel(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(
        db,
        SubnetSnapshot(
            netuid=9,
            polled_at=now,
            composite_score=70.0,
            swing_score=70.0,
            alpha_mcap_tao=2_000.0,
        ),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/")

    assert resp.status_code == 200
    assert "Fresh subnets" in resp.text
    assert "Signal coverage" in resp.text
    assert "Analyst coverage" in resp.text


async def test_subnet_detail_shows_milestones_mentions_and_category_form(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(
        db,
        SubnetSnapshot(
            netuid=5,
            polled_at=now,
            composite_score=72.0,
            emission_rank=3,
            alpha_mcap_tao=1_000.0,
        ),
    )
    await upsert_registry_entry(db, 5, "Templar", None, None, None)
    await insert_milestone(
        db,
        5,
        "arxiv",
        "SparseLoCo",
        "https://arxiv.org/abs/2603.08163",
        now,
        ai_summary="summary",
        ai_take="take",
    )
    await insert_analyst_mention(
        db,
        "0xai_dev",
        5,
        "https://x.com/0xai_dev/status/2",
        "Templar shipped a model",
        now,
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/subnet/5")

    assert resp.status_code == 200
    assert "Milestones" in resp.text
    assert "Analyst Mentions" in resp.text
    assert "SparseLoCo" in resp.text
    assert "summary" in resp.text
    assert "Templar shipped a model" in resp.text
    assert "/subnet/5/category" in resp.text


async def test_subnet_category_post_updates_registry(app, db):
    now = datetime.now(timezone.utc)
    await insert_snapshot(
        db,
        SubnetSnapshot(
            netuid=5,
            polled_at=now,
            composite_score=72.0,
            emission_rank=3,
            alpha_mcap_tao=1_000.0,
        ),
    )
    await upsert_registry_entry(db, 5, "Templar", None, None, None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/subnet/5/category", data={"category": "AI Training"})

    cursor = await db.execute(
        "SELECT category, category_confirmed FROM subnet_registry WHERE netuid=5"
    )
    row = await cursor.fetchone()
    assert resp.status_code == 303
    assert row["category"] == "AI Training"
    assert row["category_confirmed"] == 1


async def test_analysts_page_loads(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/analysts")
    assert resp.status_code == 200
    assert "Analyst Watchlist" in resp.text


async def test_analysts_add_handle(app, db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/analysts/add", data={"handle": "@testuser"}, follow_redirects=True
        )
    assert resp.status_code == 200
    assert "testuser" in resp.text
    cursor = await db.execute(
        "SELECT handle FROM analyst_watchlist WHERE handle='testuser'"
    )
    assert await cursor.fetchone() is not None


async def test_analysts_remove_dashboard_handle(app, db):
    await add_analyst_handle(db, "toremove", source="dashboard")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/analysts/remove/toremove", follow_redirects=True)
    assert resp.status_code == 200
    cursor = await db.execute(
        "SELECT handle FROM analyst_watchlist WHERE handle='toremove'"
    )
    assert await cursor.fetchone() is None
