# web/routes.py
import aiosqlite
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from db.database import (
    get_latest_snapshots, get_last_50_alerts,
    get_latest_snapshots_with_registry, get_emission_rank_24h_ago,
    get_subnet_detail, get_alerts_for_netuid, get_snapshots_for_netuid,
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(db: aiosqlite.Connection) -> FastAPI:
    app = FastAPI(title="TAO Monitor")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        snapshots = await get_latest_snapshots_with_registry(db)
        alerts = await get_last_50_alerts(db)
        trend_raw = await get_emission_rank_24h_ago(db)
        last_poll = snapshots[0]["polled_at"] if snapshots else None

        sorted_by_mcap = sorted(
            [s for s in snapshots if s["alpha_mcap_tao"] is not None],
            key=lambda s: s["alpha_mcap_tao"], reverse=True,
        )
        mcap_rank_map = {s["netuid"]: i + 1 for i, s in enumerate(sorted_by_mcap)}

        def trend_arrow(netuid, current_rank):
            prev = trend_raw.get(netuid)
            if prev is None or current_rank is None:
                return "—"
            if current_rank < prev:
                return "▲"
            if current_rank > prev:
                return "▼"
            return "→"

        enriched = [
            {**dict(s),
             "mcap_rank": mcap_rank_map.get(s["netuid"]),
             "trend": trend_arrow(s["netuid"], s["emission_rank"])}
            for s in snapshots
        ]

        return templates.TemplateResponse(request, "index.html", {
            "snapshots": enriched,
            "alerts": alerts,
            "last_poll": last_poll,
            "subnet_count": len(snapshots),
        })

    @app.get("/subnet/{netuid}", response_class=HTMLResponse)
    async def subnet_detail(request: Request, netuid: int):
        snap = await get_subnet_detail(db, netuid)
        if snap is None:
            return HTMLResponse("Subnet not found", status_code=404)

        alerts = await get_alerts_for_netuid(db, netuid, limit=10)
        all_snaps = await get_latest_snapshots_with_registry(db)
        total = len(all_snaps)

        sorted_by_mcap = sorted(
            [s for s in all_snaps if s["alpha_mcap_tao"] is not None],
            key=lambda s: s["alpha_mcap_tao"], reverse=True,
        )
        mcap_rank = next(
            (i + 1 for i, s in enumerate(sorted_by_mcap) if s["netuid"] == netuid),
            None,
        )
        score_rank = next(
            (i + 1 for i, s in enumerate(all_snaps) if s["netuid"] == netuid),
            None,
        )

        em_rank = snap["emission_rank"]
        yield_why = "no emission data"
        if em_rank is not None and mcap_rank is not None:
            ratio = mcap_rank / em_rank
            yield_why = f"em #{em_rank} vs mc #{mcap_rank} → {ratio:.1f}× gap"

        quality_why = "no GitHub data"
        if snap["gh_last_push"]:
            # SQLite stores timestamps as 'YYYY-MM-DD HH:MM:SS' (no tz); treat as UTC
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(snap["gh_last_push"]).replace(tzinfo=timezone.utc)).days
            stars = snap["gh_stars"] or 0
            quality_why = f"pushed {age}d ago · {stars:,} ⭐"

        momentum_why = "no history"
        history = await get_snapshots_for_netuid(db, netuid, limit=8)
        if len(history) >= 2:
            oldest = history[-1]
            polls = len(history) - 1
            parts = []
            # Primary: sum of emission-adjusted net TAO flows (direct staking signal)
            net_flows = [h["net_tao_flow_tao"] for h in history[:-1]
                         if h["net_tao_flow_tao"] is not None]
            if net_flows:
                total = sum(net_flows)
                sign = "+" if total >= 0 else ""
                parts.append(f"{sign}{total:.1f} τ net flow ({polls} polls)")
            else:
                # Fallback: crude tao_in delta when flow field not yet populated
                tao_now = snap["alpha_mcap_tao"]
                tao_old = oldest["alpha_mcap_tao"]
                if tao_now is not None and tao_old and tao_old > 0:
                    flow_pct = (tao_now - tao_old) / tao_old * 100
                    if flow_pct > 1:
                        parts.append(f"+{flow_pct:.0f}% tao in {polls} polls")
                    elif flow_pct < -1:
                        parts.append(f"{flow_pct:.0f}% tao in {polls} polls")
                    else:
                        parts.append("tao stable")
            # Secondary: emission rank change (lagged confirmation)
            oldest_rank = oldest["emission_rank"]
            if oldest_rank is not None and em_rank is not None:
                delta = oldest_rank - em_rank
                if delta > 0:
                    parts.append(f"em +{delta} ranks")
                elif delta < 0:
                    parts.append(f"em {delta} ranks")
            momentum_why = " · ".join(parts) if parts else "stable"

        hype_why = "no social data"
        hype_parts = []
        if snap["x_followers"] is not None:
            hype_parts.append(f"{snap['x_followers']:,} followers")
        if snap["x_last_tweet"]:
            tweet_age = (datetime.now(timezone.utc) -
                         datetime.fromisoformat(snap["x_last_tweet"]).replace(tzinfo=timezone.utc)).days
            hype_parts.append(f"tweeted {tweet_age}d ago")
        if hype_parts:
            hype_why = " · ".join(hype_parts)

        now_utc = datetime.now(timezone.utc)
        gh_push_age_days = None
        if snap["gh_last_push"]:
            gh_push_age_days = (now_utc -
                                datetime.fromisoformat(snap["gh_last_push"]).replace(tzinfo=timezone.utc)).days
        x_tweet_age_days = None
        if snap["x_last_tweet"]:
            x_tweet_age_days = (now_utc -
                                datetime.fromisoformat(snap["x_last_tweet"]).replace(tzinfo=timezone.utc)).days

        return templates.TemplateResponse(request, "subnet.html", {
            "snap": dict(snap),
            "alerts": alerts,
            "mcap_rank": mcap_rank,
            "score_rank": score_rank,
            "total_subnets": total,
            "yield_why": yield_why,
            "quality_why": quality_why,
            "momentum_why": momentum_why,
            "hype_why": hype_why,
            "gh_push_age_days": gh_push_age_days,
            "x_tweet_age_days": x_tweet_age_days,
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
