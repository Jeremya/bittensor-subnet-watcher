from collections import defaultdict
from typing import Any

import config

SEVERE_SELL_ALERTS = {"emission_near_zero", "liquidity_floor"}
MODERATE_SELL_ALERTS = {
    "ownership_transfer",
    "hyperparameter_change",
    "tao_outflow",
    "dead_github",
}
ACTION_PRIORITY = {
    "sell": 0,
    "trim": 1,
    "add": 2,
    "new_buy": 3,
    "hold": 4,
}


def _sum_or_none(values: list[float | None]) -> float | None:
    materialized = [value for value in values if value is not None]
    if not materialized:
        return None
    return sum(materialized)


def build_portfolio_ledger(rows: list[dict[str, Any]],
                           wallet_coldkeys: list[str],
                           wallet_labels: list[str]) -> dict[str, Any]:
    label_map = {}
    for i, coldkey in enumerate(wallet_coldkeys):
        label_map[coldkey] = (
            wallet_labels[i] if i < len(wallet_labels) else f"Wallet {i + 1}"
        )

    tao_usd_price = next(
        (row.get("tao_usd_price") for row in rows if row.get("tao_usd_price") is not None),
        None,
    )
    wallets_by_coldkey: dict[str, list[dict[str, Any]]] = defaultdict(list)
    positions_by_netuid: dict[int, dict[str, Any]] = {}

    for raw in rows:
        row = dict(raw)
        row["usd_value"] = (
            row["tao_value"] * tao_usd_price if tao_usd_price is not None else None
        )
        row["subnet_label"] = row.get("name") or f"SN{row['netuid']}"
        if row["baseline_tao_value"] > 0:
            row["pnl_tao"] = row["tao_value"] - row["baseline_tao_value"]
            row["pnl_pct"] = row["pnl_tao"] / row["baseline_tao_value"] * 100
        else:
            row["pnl_tao"] = None
            row["pnl_pct"] = None
        wallets_by_coldkey[row["coldkey"]].append(row)

        aggregate = positions_by_netuid.setdefault(row["netuid"], {
            "netuid": row["netuid"],
            "subnet_name": row["subnet_label"],
            "category": row.get("category") or "Other",
            "tao_value": 0.0,
            "baseline_tao_value": 0.0,
        })
        aggregate["tao_value"] += row["tao_value"]
        if row["baseline_tao_value"] > 0:
            aggregate["baseline_tao_value"] += row["baseline_tao_value"]

    wallets = []
    grand_total_tao = 0.0
    grand_total_usd = 0.0
    priced_baseline_total = 0.0
    priced_pnl_total = 0.0

    for coldkey, positions in wallets_by_coldkey.items():
        positions.sort(key=lambda pos: pos["tao_value"], reverse=True)
        total_tao = sum(pos["tao_value"] for pos in positions)
        total_usd = sum(pos["usd_value"] or 0.0 for pos in positions)
        wallet_priced_baseline = sum(
            pos["baseline_tao_value"]
            for pos in positions
            if pos["baseline_tao_value"] > 0
        )
        wallet_pnl_tao = _sum_or_none([pos["pnl_tao"] for pos in positions])
        wallet_pnl_pct = (
            wallet_pnl_tao / wallet_priced_baseline * 100
            if wallet_pnl_tao is not None and wallet_priced_baseline > 0
            else None
        )

        wallets.append({
            "label": label_map.get(coldkey, coldkey[:12] + "..."),
            "coldkey": coldkey,
            "positions": positions,
            "total_tao": total_tao,
            "total_usd": total_usd if tao_usd_price is not None else None,
            "total_pnl_tao": wallet_pnl_tao,
            "total_pnl_pct": wallet_pnl_pct,
        })

        grand_total_tao += total_tao
        grand_total_usd += total_usd
        priced_baseline_total += wallet_priced_baseline
        if wallet_pnl_tao is not None:
            priced_pnl_total += wallet_pnl_tao

    for position in positions_by_netuid.values():
        position["allocation_pct"] = (
            position["tao_value"] / grand_total_tao if grand_total_tao > 0 else 0.0
        )

    grand_pnl_tao = priced_pnl_total if priced_baseline_total > 0 else None
    grand_pnl_pct = (
        grand_pnl_tao / priced_baseline_total * 100
        if grand_pnl_tao is not None and priced_baseline_total > 0
        else None
    )

    return {
        "wallets": wallets,
        "positions_by_netuid": positions_by_netuid,
        "grand_total_tao": grand_total_tao,
        "grand_total_usd": grand_total_usd if tao_usd_price is not None else None,
        "grand_pnl_tao": grand_pnl_tao,
        "grand_pnl_pct": grand_pnl_pct,
        "tao_usd_price": tao_usd_price,
    }


def _has_positive_catalyst(snapshot: dict[str, Any],
                           alert_types: set[str],
                           covered: bool,
                           has_milestone: bool) -> bool:
    return (
        "convergence" in alert_types
        or "milestone" in alert_types
        or covered
        or has_milestone
        or (snapshot.get("momentum_score") or 0.0) >= 70.0
    )


def _has_thesis_break(snapshot: dict[str, Any], alert_types: set[str]) -> bool:
    if SEVERE_SELL_ALERTS & alert_types:
        return True
    moderate_count = len(MODERATE_SELL_ALERTS & alert_types)
    return (
        moderate_count >= 2
        and (snapshot.get("composite_score") or 0.0) < config.PORTFOLIO_HOLD_FLOOR_SCORE
    )


def _is_illiquid(snapshot: dict[str, Any]) -> bool:
    """Real-time liquidity check — don't wait for the alert to have fired."""
    volume = snapshot.get("volume_24h_alpha")
    price = snapshot.get("alpha_price_tao")
    mcap = snapshot.get("alpha_mcap_tao")
    if not volume or not price or not mcap:
        return False  # no data → can't determine, don't block
    return (volume * price) / mcap < config.LIQUIDITY_FLOOR_RATIO


def _card(snapshot: dict[str, Any],
          action: str,
          confidence: str,
          reasons: list[str],
          allocation_pct: float | None) -> dict[str, Any]:
    return {
        "netuid": snapshot["netuid"],
        "subnet_name": snapshot.get("name") or f"SN{snapshot['netuid']}",
        "action": action,
        "confidence": confidence,
        "reasons": reasons,
        "score": snapshot.get("composite_score"),
        "category": snapshot.get("category") or "Other",
        "allocation_pct": allocation_pct,
    }


def _sort_action_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        cards,
        key=lambda card: (
            ACTION_PRIORITY.get(card["action"], 99),
            -(card.get("allocation_pct") or 0.0),
            -(card.get("score") or 0.0),
            card["netuid"],
        ),
    )


def build_portfolio_recommendations(
    positions_by_netuid: dict[int, dict[str, Any]],
    snapshots: list[dict[str, Any]],
    alert_types_by_netuid: dict[int, set[str]],
    coverage_netuids: set[int],
    milestone_netuids: set[int],
) -> dict[str, Any]:
    snapshots_by_netuid = {snap["netuid"]: dict(snap) for snap in snapshots}
    category_allocations: dict[str, float] = defaultdict(float)
    for position in positions_by_netuid.values():
        category_allocations[position["category"]] += position["allocation_pct"]

    weakest_held_score = (
        min(
            (snapshots_by_netuid.get(netuid, {}).get("composite_score") or 0.0)
            for netuid in positions_by_netuid
        )
        if positions_by_netuid
        else 0.0
    )

    portfolio_actions: list[dict[str, Any]] = []
    table_actions: dict[int, dict[str, Any]] = {}

    for netuid, position in positions_by_netuid.items():
        snapshot = snapshots_by_netuid.get(netuid, {
            "netuid": netuid,
            "name": position["subnet_name"],
            "category": position["category"],
            "composite_score": None,
            "momentum_score": None,
        })
        alert_types = alert_types_by_netuid.get(netuid, set())
        catalyst = _has_positive_catalyst(
            snapshot,
            alert_types,
            netuid in coverage_netuids,
            netuid in milestone_netuids,
        )
        score = snapshot.get("composite_score") or 0.0

        if _has_thesis_break(snapshot, alert_types):
            card = _card(
                snapshot,
                "sell",
                "high",
                ["thesis break: severe or repeated risk alerts"],
                position["allocation_pct"],
            )
            table_actions[netuid] = card
            portfolio_actions.append(card)
            continue

        if position["allocation_pct"] >= config.PORTFOLIO_TRIM_MAX_ALLOC_PCT:
            reasons = [f"position is {position['allocation_pct'] * 100:.1f}% of book"]
            if not catalyst:
                reasons.append("no fresh positive catalyst")
            card = _card(
                snapshot,
                "trim",
                "high",
                reasons,
                position["allocation_pct"],
            )
            table_actions[netuid] = card
            portfolio_actions.append(card)
            continue

        if (
            score >= config.PORTFOLIO_ADD_MIN_SCORE
            and catalyst
            and category_allocations[position["category"]] < config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT
        ):
            card = _card(
                snapshot,
                "add",
                "medium",
                ["strong held winner with room to add"],
                position["allocation_pct"],
            )
            table_actions[netuid] = card
            portfolio_actions.append(card)
            continue

        table_actions[netuid] = _card(
            snapshot,
            "hold",
            "low",
            [],
            position["allocation_pct"],
        )

    new_candidates: list[dict[str, Any]] = []
    for snapshot in sorted(
        snapshots,
        key=lambda snap: snap.get("composite_score") or 0.0,
        reverse=True,
    ):
        netuid = snapshot["netuid"]
        if netuid in positions_by_netuid:
            continue
        score = snapshot.get("composite_score") or 0.0
        if score < config.PORTFOLIO_NEW_BUY_MIN_SCORE:
            continue
        if score < weakest_held_score + config.PORTFOLIO_REPLACE_SCORE_MARGIN:
            continue

        alert_types = alert_types_by_netuid.get(netuid, set())
        if _has_thesis_break(snapshot, alert_types):
            continue

        if _is_illiquid(snapshot):
            continue

        category = snapshot.get("category") or "Other"
        if category_allocations[category] >= config.PORTFOLIO_CATEGORY_MAX_ALLOC_PCT:
            continue

        catalyst = _has_positive_catalyst(
            snapshot,
            alert_types,
            netuid in coverage_netuids,
            netuid in milestone_netuids,
        )
        if not catalyst:
            continue

        new_candidates.append(
            _card(
                snapshot,
                "new_buy",
                "medium",
                ["outranks weakest held name with a fresh catalyst"],
                None,
            )
        )

    return {
        "portfolio_actions": _sort_action_cards(portfolio_actions),
        "new_candidates": _sort_action_cards(new_candidates)[:3],
        "table_actions": table_actions,
    }
