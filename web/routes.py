# web/routes.py
import aiosqlite
from pathlib import Path
from datetime import datetime, timezone
import config
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from db.database import (
    add_analyst_handle,
    get_analyst_mentions_for_netuid,
    get_analyst_watchlist,
    get_milestones_for_netuid,
    get_latest_snapshots, get_last_50_alerts,
    get_latest_snapshots_with_registry, get_emission_rank_24h_ago,
    has_active_analyst_coverage,
    get_subnet_detail, get_alerts_for_netuid, get_snapshots_for_netuid,
    get_owner_change_counts, get_reg_cost_7d_ago,
    get_portfolio_positions, get_staked_netuids,
    remove_analyst_handle,
    update_registry_category,
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
        staked_netuids = await get_staked_netuids(db)
        coverage_netuids: set[int] = set()
        for snap in snapshots:
            if await has_active_analyst_coverage(
                db,
                snap["netuid"],
                config.ANALYST_COVERAGE_DECAY_HOURS,
            ):
                coverage_netuids.add(snap["netuid"])

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

        return templates.TemplateResponse(request, "index.html", {
            "snapshots": enriched,
            "alerts": alerts,
            "last_poll": last_poll,
            "subnet_count": len(snapshots),
            "all_categories": all_categories,
        })

    @app.get("/subnet/{netuid}", response_class=HTMLResponse)
    async def subnet_detail(request: Request, netuid: int):
        snap = await get_subnet_detail(db, netuid)
        if snap is None:
            return HTMLResponse("Subnet not found", status_code=404)

        alerts = await get_alerts_for_netuid(db, netuid, limit=10)
        analyst_mentions = await get_analyst_mentions_for_netuid(db, netuid, limit=10)
        milestones = await get_milestones_for_netuid(db, netuid, limit=10)
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
        # Single synthesised call for the investor: entry / caution / exit / risk
        if owner_n >= 3:
            verdict = f"Governance risk — {owner_n} ownership changes in 30 days"
        elif momentum_state == "fragile":
            verdict = "Fragile — capital exiting despite rising emission rank"
        elif momentum_state == "distributing" and yield_state in ("overpriced", "rich"):
            verdict = "Exit candidate — overpriced and capital leaving"
        elif momentum_state == "distributing":
            verdict = "Caution — capital outflow, monitor emission rank"
        elif yield_state == "underpriced" and momentum_state == "accumulating":
            verdict = "Entry signal — underpriced yield with capital accumulating"
        elif yield_state in ("underpriced", "discount") and momentum_state == "early_inflow":
            verdict = "Potential entry — discount yield, inflow building"
        elif yield_state in ("overpriced", "rich") and momentum_state != "accumulating":
            verdict = "Avoid — priced above emission contribution"
        elif health_risks:
            verdict = f"Risk flag — {health_risks[0]}"
        else:
            verdict = "Monitor — no strong entry or exit signal"

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

    @app.get("/analysts", response_class=HTMLResponse)
    async def analysts_page(request: Request):
        db_rows = await get_analyst_watchlist(db)
        db_handles = [row["handle"] for row in db_rows]
        config_handles = config.ANALYST_HANDLES
        return templates.TemplateResponse(request, "analysts.html", {
            "db_handles": db_handles,
            "config_handles": config_handles,
        })

    @app.post("/analysts/add")
    async def analysts_add(handle: str = Form(...)):
        clean = handle.lstrip("@").strip()
        if clean:
            await add_analyst_handle(db, clean, source="dashboard")
        return RedirectResponse("/analysts", status_code=303)

    @app.post("/analysts/remove/{handle}")
    async def analysts_remove(handle: str):
        await remove_analyst_handle(db, handle)
        return RedirectResponse("/analysts", status_code=303)

    @app.get("/portfolio", response_class=HTMLResponse)
    async def portfolio(request: Request):
        rows = await get_portfolio_positions(db)

        # Build label map
        label_map = {}
        for i, ck in enumerate(config.WALLET_COLDKEYS):
            label_map[ck] = (config.WALLET_LABELS[i]
                             if i < len(config.WALLET_LABELS) else f"Wallet {i + 1}")

        # Group by coldkey
        from collections import defaultdict
        by_coldkey: dict[str, list[dict]] = defaultdict(list)
        tao_usd_price: float | None = None

        for row in rows:
            r = dict(row)
            if tao_usd_price is None and r.get("tao_usd_price"):
                tao_usd_price = r["tao_usd_price"]
            baseline = r["baseline_tao_value"]
            tao_val = r["tao_value"]
            if baseline and baseline > 0:
                r["pnl_tao"] = tao_val - baseline
                r["pnl_pct"] = (tao_val - baseline) / baseline * 100
            else:
                r["pnl_tao"] = None
                r["pnl_pct"] = None
            r["usd_value"] = (tao_val * tao_usd_price) if tao_usd_price else None
            r["subnet_label"] = r.get("name") or f"SN{r['netuid']}"
            by_coldkey[r["coldkey"]].append(r)

        wallets = []
        grand_tao = grand_usd = grand_pnl_tao = 0.0
        grand_baseline = 0.0

        for ck, positions in by_coldkey.items():
            total_tao = sum(p["tao_value"] for p in positions)
            total_usd = sum(p["usd_value"] or 0 for p in positions)
            total_baseline = sum(p["baseline_tao_value"] for p in positions)
            total_pnl_tao = total_tao - total_baseline if total_baseline > 0 else None
            total_pnl_pct = (total_pnl_tao / total_baseline * 100
                             if total_baseline > 0 else None)
            wallets.append({
                "label": label_map.get(ck, ck[:12] + "..."),
                "coldkey": ck,
                "positions": positions,
                "total_tao": total_tao,
                "total_usd": total_usd if tao_usd_price else None,
                "total_pnl_tao": total_pnl_tao,
                "total_pnl_pct": total_pnl_pct,
            })
            grand_tao += total_tao
            grand_usd += total_usd
            grand_baseline += total_baseline

        grand_pnl_tao = grand_tao - grand_baseline if grand_baseline > 0 else None
        grand_pnl_pct = (grand_pnl_tao / grand_baseline * 100
                         if grand_baseline and grand_baseline > 0 else None)

        return templates.TemplateResponse(request, "portfolio.html", {
            "wallets": wallets,
            "grand_total_tao": grand_tao,
            "grand_total_usd": grand_usd if tao_usd_price else None,
            "grand_pnl_tao": grand_pnl_tao,
            "grand_pnl_pct": grand_pnl_pct,
            "tao_usd_price": tao_usd_price,
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
