"""Replay detect_ignition over history against recorded pump events.

Grid-searches (price impulse, flow pct) and reports events-caught vs
false-fires/day so config defaults are chosen from evidence, not vibes.

Usage: .venv/bin/python -m scripts.tune_ignition [--db PATH]
"""
import argparse
import asyncio
from datetime import datetime, timedelta

import config
from db.database import init_db
from engine.ignition import detect_ignition
from engine.pump_events import _row_to_snapshot


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()
    db = await init_db(args.db)
    try:
        cursor = await db.execute(
            """SELECT netuid, polled_at, alpha_price_tao, alpha_mcap_usd,
                      owner_coldkey, volume_24h_alpha, net_tao_flow_tao,
                      alpha_mcap_tao, buy_slippage_pct
               FROM snapshots ORDER BY netuid, polled_at""")
        rows = await cursor.fetchall()
        by_netuid: dict[int, list] = {}
        for r in rows:
            s = _row_to_snapshot(r)
            s.volume_24h_alpha = r["volume_24h_alpha"]
            s.net_tao_flow_tao = r["net_tao_flow_tao"]
            s.alpha_mcap_tao = r["alpha_mcap_tao"]
            s.buy_slippage_pct = r["buy_slippage_pct"]
            by_netuid.setdefault(r["netuid"], []).append(s)

        cursor = await db.execute("SELECT * FROM pump_events")
        events = await cursor.fetchall()
        starts = [(e["netuid"], datetime.fromisoformat(e["start_at"])) for e in events]
        total_days = max(1.0, len(rows) / 129 / 96)

        print(f"{'impulse%':>8} {'flow%':>6} {'caught':>7} {'of':>3} {'false/day':>9}")
        for impulse in (4.0, 6.0, 8.0, 10.0):
            for flow in (0.01, 0.02, 0.03):
                config.IGNITION_PRICE_IMPULSE_PCT = impulse
                config.IGNITION_FLOW_PCT = flow
                fires = []
                for netuid, series in by_netuid.items():
                    for i in range(1, len(series)):
                        sig = detect_ignition(series[i], list(reversed(series[:i])))
                        if sig:
                            fires.append((netuid, series[i].polled_at))
                caught = sum(
                    1 for en, es in starts
                    if any(fn == en and es <= ft <= es + timedelta(hours=6)
                           for fn, ft in fires))
                false = sum(
                    1 for fn, ft in fires
                    if not any(fn == en and es - timedelta(hours=1) <= ft <= es + timedelta(hours=72)
                               for en, es in starts))
                print(f"{impulse:>8.1f} {flow*100:>6.1f} {caught:>7} {len(starts):>3} "
                      f"{false / total_days:>9.2f}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
