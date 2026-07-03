"""One-time backfill of pump_events over full snapshot history.

Usage: .venv/bin/python -m scripts.backfill_pump_events [--db PATH]
"""
import argparse
import asyncio

import config
from db.database import init_db
from engine.pump_events import scan_and_store, get_recent_pump_events


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    args = parser.parse_args()

    db = await init_db(args.db)
    try:
        count = await scan_and_store(db, since_days=3650)
        rows = await get_recent_pump_events(db, limit=500)
        print(f"upserted {count} events; registry now holds {len(rows)}")
        print(f"{'SN':>4} {'start':>16} {'ratio':>6} {'status':>7} {'retrace':>8}")
        for r in rows:
            retrace = f"{r['retrace_pct']:.0%}" if r["retrace_pct"] is not None else "-"
            print(f"{r['netuid']:>4} {r['start_at'][:16]:>16} "
                  f"{r['ratio']:>6.2f} {r['status']:>7} {retrace:>8}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
