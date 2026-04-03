# web/routes.py
import aiosqlite
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from db.database import get_latest_snapshots, get_last_50_alerts

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(db: aiosqlite.Connection) -> FastAPI:
    app = FastAPI(title="TAO Monitor")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        snapshots = await get_latest_snapshots(db)
        alerts = await get_last_50_alerts(db)
        last_poll = snapshots[0]["polled_at"] if snapshots else None
        return templates.TemplateResponse(request, "index.html", {
            "snapshots": snapshots,
            "alerts": alerts,
            "last_poll": last_poll,
            "subnet_count": len(snapshots),
        })

    @app.get("/api/snapshots")
    async def api_snapshots():
        rows = await get_latest_snapshots(db)
        return [dict(row) for row in rows]

    @app.get("/api/alerts")
    async def api_alerts():
        rows = await get_last_50_alerts(db)
        return [dict(row) for row in rows]

    return app
