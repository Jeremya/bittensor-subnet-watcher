# web/routes.py
import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta, timezone
import config
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from db.database import (
    add_analyst_handle,
    get_active_analyst_coverage_netuids,
    get_analyst_mentions_for_netuid,
    get_analyst_watchlist,
    get_recent_analyst_mentions,
    get_registry,
    get_covered_netuids,
    get_milestones_for_netuid,
    get_latest_snapshots, get_last_50_alerts,
    get_latest_snapshots_with_registry, get_emission_rank_24h_ago,
    get_collector_state,
    get_subnet_detail, get_alerts_for_netuid, get_snapshots_for_netuid,
    get_owner_change_counts, get_reg_cost_7d_ago,
    get_portfolio_positions, get_staked_netuids,
    get_recent_alert_types_per_netuid,
    get_recent_milestone_netuids,
    remove_analyst_handle,
    update_registry_category,
)
from engine.recommendations import (
    build_portfolio_ledger,
    build_portfolio_recommendations,
)
from engine.health import compute_collector_health
from engine.mentions import add_manual_mention
from engine.pump_events import get_pump_events_for_netuid, get_recent_pump_events
from engine.policy import build_signal_from_snapshot, verdict_for_subnet
from engine.signals import SCORING_ALERT_TYPES

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def create_app(db: aiosqlite.Connection) -> FastAPI:
    app = FastAPI(title="TAO Monitor")

    def _parse_utc(value: str | None) -> datetime | None:
        if value is None:
            return None
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        snapshots = await get_latest_snapshots_with_registry(db)
        alerts = await get_last_50_alerts(db)
        trend_raw = await get_emission_rank_24h_ago(db)
        now_utc = datetime.now(timezone.utc)
        parsed_polls = [dt for dt in (_parse_utc(s["polled_at"]) for s in snapshots) if dt is not None]
        last_poll = max(parsed_polls).isoformat() if parsed_polls else None
        freshness_cutoff = now_utc - timedelta(minutes=config.POLL_INTERVAL_MINUTES * 2)
        stale_subnets = 0
        signal_ready = 0
        newest_age_minutes = None
        if parsed_polls:
            newest_age_minutes = round((now_utc - max(parsed_polls)).total_seconds() / 60, 1)
        for snap in snapshots:
            polled_at = _parse_utc(snap["polled_at"])
            if polled_at is None:
                continue
            if polled_at < freshness_cutoff:
                stale_subnets += 1
            if snap["swing_score"] is not None:
                signal_ready += 1
        staked_netuids = await get_staked_netuids(db)
        collector_health = await compute_collector_health(db)
        coverage_netuids = await get_covered_netuids(db, config.ANALYST_COVERAGE_DECAY_HOURS)
        milestone_arxiv_check = await get_collector_state(db, "milestone_last_arxiv_check")
        milestone_hf_check = await get_collector_state(db, "milestone_last_hf_check")

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
             "trend": trend_arrow(s["netuid"], s["emission_rank"]),
             "staked": s["netuid"] in staked_netuids,
             "covered": s["netuid"] in coverage_netuids,
             "category": s["category"] if "category" in s.keys() else None}
            for s in snapshots
        ]

        all_categories = sorted({
            snap["category"]
            for snap in enriched
            if snap.get("category") and snap["category"] != "Other"
        })
        data_health = {
            "fresh_subnets": len(snapshots) - stale_subnets,
            "stale_subnets": stale_subnets,
            "signal_coverage_pct": round(signal_ready / len(snapshots) * 100, 1) if snapshots else None,
            "newest_age_minutes": newest_age_minutes,
            "analyst_coverage_count": len(coverage_netuids),
            "milestone_arxiv_check": milestone_arxiv_check,
            "milestone_hf_check": milestone_hf_check,
        }

        return templates.TemplateResponse(request, "index.html", {
            "snapshots": enriched,
            "alerts": alerts,
            "last_poll": last_poll,
            "subnet_count": len(snapshots),
            "all_categories": all_categories,
            "data_health": data_health,
            "collector_health": collector_health,
        })

    @app.get("/subnet/{netuid}", response_class=HTMLResponse)
    async def subnet_detail(request: Request, netuid: int):
        snap = await get_subnet_detail(db, netuid)
        if snap is None:
            return HTMLResponse("Subnet not found", status_code=404)

        alerts = await get_alerts_for_netuid(db, netuid, limit=10)
        pump_events = await get_pump_events_for_netuid(db, netuid, limit=5)
        analyst_mentions = await get_analyst_mentions_for_netuid(db, netuid, limit=10)
        milestones = await get_milestones_for_netuid(db, netuid, limit=10)
        all_snaps = await get_latest_snapshots_with_registry(db)
        total = len(all_snaps)
        alert_types_by_netuid = await get_recent_alert_types_per_netuid(
            db,
            SCORING_ALERT_TYPES,
            config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS,
        )
        covered_netuids = await get_active_analyst_coverage_netuids(
            db,
            config.ANALYST_COVERAGE_DECAY_HOURS,
        )
        milestone_netuids = await get_recent_milestone_netuids(
            db,
            config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS,
        )

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

        # ── Yield signal ─────────────────────────────────────────────────────
        # Investor question: is this underpriced or overpriced relative to
        # its actual emission contribution?
        yield_state = "unknown"
        yield_why = "no emission data"
        if em_rank is not None and mcap_rank is not None:
            ratio = mcap_rank / em_rank
            if ratio >= 2.0:
                yield_state = "underpriced"
                yield_why = f"entry candidate — earns em #{em_rank}, priced mc #{mcap_rank}"
            elif ratio >= 1.2:
                yield_state = "discount"
                yield_why = f"mild discount — em #{em_rank} vs mc #{mcap_rank}"
            elif ratio >= 0.8:
                yield_state = "fair"
                yield_why = f"fair value — em #{em_rank} ≈ mc #{mcap_rank}"
            elif ratio >= 0.5:
                yield_state = "overpriced"
                yield_why = f"overpriced — mc #{mcap_rank} but earns em #{em_rank}"
            else:
                yield_state = "rich"
                yield_why = f"exit risk — priced mc #{mcap_rank}, earns em #{em_rank}"

        # ── Health signal ─────────────────────────────────────────────────────
        # Investor question: are there governance, execution, or liquidity risks?
        owner_counts = await get_owner_change_counts(db, days=30)
        reg_cost_7d = await get_reg_cost_7d_ago(db)
        owner_n = owner_counts.get(netuid, 1)
        prev_reg = reg_cost_7d.get(netuid)

        health_risks = []
        if owner_n >= 3:
            health_risks.append(f"governance risk: {owner_n} owners in 30d")
        elif owner_n == 2:
            health_risks.append("ownership change: 2 owners in 30d")

        if snap["reg_cost_tao"] is not None and prev_reg is not None and prev_reg > 0:
            reg_pct = (snap["reg_cost_tao"] - prev_reg) / prev_reg
            if reg_pct < -0.20:
                health_risks.append(f"demand risk: reg cost ↓{abs(reg_pct)*100:.0f}%")
            elif reg_pct > 0.20:
                health_risks.append(f"access tightening: reg cost ↑{reg_pct*100:.0f}%")

        now_utc = datetime.now(timezone.utc)
        gh_age = None
        if snap["gh_last_push"]:
            gh_age = (now_utc -
                      datetime.fromisoformat(snap["gh_last_push"]).replace(tzinfo=timezone.utc)).days
            if gh_age > 60:
                health_risks.append(f"execution risk: no commits {gh_age}d")

        vol = snap["volume_24h_alpha"]
        mcap_tao = snap["alpha_mcap_tao"]
        price = snap["alpha_price_tao"]
        if (vol is not None and price is not None
                and mcap_tao and mcap_tao > 0
                and (vol * price) / mcap_tao < 0.001):
            health_risks.append("exit liquidity risk: <0.1% daily turnover")

        health_why = " · ".join(health_risks) if health_risks else "no significant risks"

        # ── Momentum signal ───────────────────────────────────────────────────
        # Investor question: is capital entering or leaving right now?
        momentum_state = "unknown"
        momentum_why = "insufficient history"
        history = await get_snapshots_for_netuid(db, netuid, limit=config.MOMENTUM_HISTORY_LIMIT)
        if len(history) >= 2:
            oldest = history[-1]
            polls = len(history) - 1

            net_flows = [h["net_tao_flow_tao"] for h in history[:-1]
                         if h["net_tao_flow_tao"] is not None]
            total_flow: float | None = None
            if net_flows:
                total_flow = sum(net_flows)
            else:
                # Fallback: crude tao_in delta (mixes emission + staking)
                tao_now = snap["alpha_mcap_tao"]
                tao_old = oldest["alpha_mcap_tao"]
                if tao_now is not None and tao_old and tao_old > 0:
                    total_flow = tao_now - tao_old

            oldest_rank = oldest["emission_rank"]
            rank_delta = None
            if oldest_rank is not None and em_rank is not None:
                rank_delta = oldest_rank - em_rank  # positive = rank improved

            if total_flow is not None:
                sign = "+" if total_flow >= 0 else ""
                flow_str = f"{sign}{total_flow:.0f} τ ({polls} polls)"
                rising = rank_delta is not None and rank_delta > 0
                falling = rank_delta is not None and rank_delta < 0

                if total_flow > 0 and not falling:
                    momentum_state = "accumulating"
                    momentum_why = f"accumulating — {flow_str}, emission rising"
                elif total_flow > 0 and falling:
                    momentum_state = "early_inflow"
                    momentum_why = f"early inflow — {flow_str}, emission lagged"
                elif total_flow < 0 and rising:
                    momentum_state = "fragile"
                    momentum_why = f"fragile rally — {flow_str} outflow despite rising rank"
                elif total_flow < 0:
                    momentum_state = "distributing"
                    momentum_why = f"distributing — {flow_str} outflow, emission declining"
                else:
                    momentum_state = "neutral"
                    momentum_why = f"neutral — {flow_str}"
            else:
                momentum_why = "no flow data"

        # ── Verdict ───────────────────────────────────────────────────────────
        # Single 1-2 week swing call for the investor: entry / caution / exit / risk
        signal = build_signal_from_snapshot(
            dict(snap),
            alert_types_by_netuid.get(netuid, set()),
            netuid in covered_netuids,
            netuid in milestone_netuids,
        )
        verdict = verdict_for_subnet(signal)

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

        # gh_age already computed in health block; reuse for template colouring
        gh_push_age_days = gh_age
        x_tweet_age_days = None
        if snap["x_last_tweet"]:
            x_tweet_age_days = (now_utc -
                                datetime.fromisoformat(snap["x_last_tweet"]).replace(tzinfo=timezone.utc)).days

        return templates.TemplateResponse(request, "subnet.html", {
            "snap": dict(snap),
            "alerts": alerts,
            "pump_events": pump_events,
            "analyst_mentions": analyst_mentions,
            "milestones": milestones,
            "mcap_rank": mcap_rank,
            "score_rank": score_rank,
            "total_subnets": total,
            "verdict": verdict,
            "yield_why": yield_why,
            "health_why": health_why,
            "momentum_why": momentum_why,
            "hype_why": hype_why,
            "gh_push_age_days": gh_push_age_days,
            "x_tweet_age_days": x_tweet_age_days,
        })

    @app.post("/subnet/{netuid}/category")
    async def subnet_set_category(netuid: int, category: str = Form(...)):
        await update_registry_category(db, netuid, category, confirmed=True)
        return RedirectResponse(f"/subnet/{netuid}", status_code=303)

    @app.post("/subnet/{netuid}/mention")
    async def subnet_add_mention(netuid: int,
                                 tweet_url: str = Form(...),
                                 tweet_text: str = Form("")):
        registry = await get_registry(db)
        await add_manual_mention(db, registry, netuid, tweet_url, tweet_text)
        return RedirectResponse(f"/subnet/{netuid}", status_code=303)

    @app.get("/analysts", response_class=HTMLResponse)
    async def analysts_page(request: Request):
        db_rows = await get_analyst_watchlist(db)
        db_handles = [row["handle"] for row in db_rows]
        config_handles = config.ANALYST_HANDLES
        recent_mentions = await get_recent_analyst_mentions(db, limit=30)
        return templates.TemplateResponse(request, "analysts.html", {
            "db_handles": db_handles,
            "config_handles": config_handles,
            "recent_mentions": recent_mentions,
        })

    @app.post("/analysts/mention")
    async def analysts_add_mention(netuid: int = Form(...),
                                   tweet_url: str = Form(...),
                                   tweet_text: str = Form("")):
        registry = await get_registry(db)
        await add_manual_mention(db, registry, netuid, tweet_url, tweet_text)
        return RedirectResponse("/analysts", status_code=303)

    @app.post("/analysts/add")
    async def analysts_add(handle: str = Form(...)):
        clean = handle.lstrip("@").strip()
        if clean:
            db_rows = await get_analyst_watchlist(db)
            total = len(db_rows) + len(config.ANALYST_HANDLES)
            if total < config.MAX_ANALYST_HANDLES:
                await add_analyst_handle(db, clean, source="dashboard")
        return RedirectResponse("/analysts", status_code=303)

    @app.post("/analysts/remove/{handle}")
    async def analysts_remove(handle: str):
        await remove_analyst_handle(db, handle)
        return RedirectResponse("/analysts", status_code=303)

    @app.get("/pumps", response_class=HTMLResponse)
    async def pumps_page(request: Request):
        events = await get_recent_pump_events(db, limit=100)
        registry = await get_registry(db)
        enriched = []
        for ev in events:
            row = dict(ev)
            reg = registry.get(ev["netuid"])
            row["name"] = (reg["name"] if reg else None) or f"SN{ev['netuid']}"
            # signals at T-6h before start (the "did anything lead it?" column)
            cursor = await db.execute(
                """
                SELECT swing_score, emergence_score, catalyst_score FROM snapshots
                WHERE netuid=? AND datetime(polled_at) <= datetime(?, '-6 hours')
                ORDER BY polled_at DESC LIMIT 1
                """,
                (ev["netuid"], ev["start_at"]),
            )
            lead = await cursor.fetchone()
            row["lead_swing"] = lead["swing_score"] if lead else None
            row["lead_emergence"] = lead["emergence_score"] if lead else None
            row["lead_catalyst"] = lead["catalyst_score"] if lead else None
            enriched.append(row)
        return templates.TemplateResponse(request, "pumps.html", {"events": enriched})

    @app.get("/emerging", response_class=HTMLResponse)
    async def emerging(request: Request):
        rows = [dict(row) for row in await get_latest_snapshots_with_registry(db)]
        candidates = [
            row for row in rows
            if row.get("emergence_score") is not None
            and row.get("emergence_stage") not in (None, "established")
        ]
        candidates.sort(key=lambda row: row["emergence_score"], reverse=True)
        return templates.TemplateResponse(request, "emerging.html", {
            "request": request,
            "subnets": candidates,
        })

    @app.get("/portfolio", response_class=HTMLResponse)
    async def portfolio(request: Request):
        rows = [dict(row) for row in await get_portfolio_positions(db)]
        ledger = build_portfolio_ledger(
            rows,
            config.WALLET_COLDKEYS,
            config.WALLET_LABELS,
        )
        latest_snaps = [dict(row) for row in await get_latest_snapshots_with_registry(db)]
        snapshots_by_netuid = {snap["netuid"]: snap for snap in latest_snaps}
        alert_types = await get_recent_alert_types_per_netuid(
            db,
            SCORING_ALERT_TYPES,
            config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS,
        )
        coverage_netuids = await get_active_analyst_coverage_netuids(
            db,
            config.ANALYST_COVERAGE_DECAY_HOURS,
        )
        milestone_netuids = await get_recent_milestone_netuids(
            db,
            config.PORTFOLIO_RECOMMENDATION_WINDOW_HOURS,
        )
        recs = build_portfolio_recommendations(
            positions_by_netuid=ledger["positions_by_netuid"],
            snapshots=latest_snaps,
            alert_types_by_netuid=alert_types,
            coverage_netuids=coverage_netuids,
            milestone_netuids=milestone_netuids,
        )

        for wallet in ledger["wallets"]:
            for pos in wallet["positions"]:
                aggregate = ledger["positions_by_netuid"][pos["netuid"]]
                pos["allocation_pct"] = aggregate["allocation_pct"] * 100
                snap = snapshots_by_netuid.get(pos["netuid"])
                pos["score"] = snap.get("composite_score") if snap else None
                pos["spec421_score"] = snap.get("spec421_score") if snap else None
                pos["recommendation"] = recs["table_actions"].get(
                    pos["netuid"],
                    {
                        "action": "hold",
                        "confidence": "low",
                        "reasons": [],
                    },
                )

        return templates.TemplateResponse(request, "portfolio.html", {
            **ledger,
            **recs,
        })

    @app.get("/api/snapshots")
    async def api_snapshots():
        rows = await get_latest_snapshots(db)
        return [dict(row) for row in rows]

    @app.get("/api/alerts")
    async def api_alerts():
        rows = await get_last_50_alerts(db)
        return [dict(row) for row in rows]

    @app.get("/api/health")
    async def api_health():
        from dataclasses import asdict
        return [asdict(h) for h in await compute_collector_health(db)]

    return app
