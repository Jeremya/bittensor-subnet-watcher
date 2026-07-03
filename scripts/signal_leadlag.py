"""Grade every persisted signal against recorded pump events.

For each CLOSED pump event, sample each signal column at T-24h, T-12h, T-6h,
T-1h and T0 relative to start_at (nearest snapshot at or before the offset,
within 2h — otherwise 'no data'; NULLs counted separately, never as 0).
A signal 'hit' an event if any pre-T0 sample >= threshold. Also grades
pump_ignition alerts: hit = fired within [start, start+6h]; late = (start+6h,
peak]; false = no event started within 72h after the alert.

Usage: .venv/bin/python -m scripts.signal_leadlag [--db PATH] [--threshold 70] [--output x.json]
"""
import argparse
import asyncio
import json
import statistics
from datetime import datetime, timedelta, timezone

import aiosqlite

import config
from db.database import init_db

SIGNAL_COLUMNS = [
    "swing_score", "spec421_score", "flow_score", "emergence_score",
    "reg_demand_score", "slot_fill_score", "flow_accel_score",
    "catalyst_score", "tradability_score",
]
OFFSETS_HOURS = [24, 12, 6, 1, 0]
SAMPLE_TOLERANCE_HOURS = 2


async def _sample(db, netuid: int, at: datetime, column: str):
    cursor = await db.execute(
        f"""
        SELECT {column}, polled_at FROM snapshots
        WHERE netuid=? AND datetime(polled_at) <= datetime(?)
        ORDER BY polled_at DESC LIMIT 1
        """,
        (netuid, at.isoformat()),
    )
    row = await cursor.fetchone()
    if row is None:
        return "no_data"
    age = at - datetime.fromisoformat(row["polled_at"])
    if age > timedelta(hours=SAMPLE_TOLERANCE_HOURS):
        return "no_data"
    return row[column]           # may be None (NULL) — a null sample, not 0


async def run_leadlag(db: aiosqlite.Connection, threshold: float = 70.0) -> dict:
    cursor = await db.execute(
        "SELECT * FROM pump_events WHERE status='closed' ORDER BY start_at"
    )
    events = await cursor.fetchall()

    signals: dict[str, dict] = {}
    for col in SIGNAL_COLUMNS:
        hits, misses, unmeasurable = 0, 0, 0
        values_by_offset: dict[int, list[float]] = {h: [] for h in OFFSETS_HOURS}
        for ev in events:
            start = datetime.fromisoformat(ev["start_at"])
            pre_samples = []
            for h in OFFSETS_HOURS:
                s = await _sample(db, ev["netuid"], start - timedelta(hours=h), col)
                if isinstance(s, (int, float)):
                    values_by_offset[h].append(s)
                if h > 0:
                    pre_samples.append(s)
            numeric = [s for s in pre_samples if isinstance(s, (int, float))]
            if not numeric:
                unmeasurable += 1
            elif any(v >= threshold for v in numeric):
                hits += 1
            else:
                misses += 1
        measured = hits + misses
        signals[col] = {
            "hits": hits, "misses": misses, "unmeasurable": unmeasurable,
            "hit_rate": (hits / measured) if measured else None,
            "median_by_offset": {
                f"T-{h}h" if h else "T0": (round(statistics.median(v), 1) if v else None)
                for h, v in values_by_offset.items()
            },
        }

    # Grade pump_ignition alerts (netuid -1 = market-wide summary rows, skipped)
    cursor = await db.execute(
        "SELECT netuid, fired_at FROM alerts "
        "WHERE alert_type='pump_ignition' AND netuid != -1"
    )
    alerts = await cursor.fetchall()
    grades = {"hit": 0, "late": 0, "false": 0}
    for alert in alerts:
        fired = datetime.fromisoformat(alert["fired_at"])
        grade = "false"
        for ev in events:
            if ev["netuid"] != alert["netuid"]:
                continue
            start = datetime.fromisoformat(ev["start_at"])
            peak = datetime.fromisoformat(ev["peak_at"]) if ev["peak_at"] else start
            if start <= fired <= start + timedelta(hours=6):
                grade = "hit"
                break
            if start + timedelta(hours=6) < fired <= peak:
                grade = "late"
        grades[grade] += 1

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "event_count": len(events),
        "threshold": threshold,
        "signals": signals,
        "ignition_grades": grades,
    }


def _print_report(report: dict) -> None:
    print(f"Lead/lag over {report['event_count']} closed pump events "
          f"(threshold {report['threshold']})\n")
    print(f"{'signal':<22} {'hit-rate':>8} {'hits':>5} {'miss':>5} {'unmeas':>6}  medians T-24/12/6/1/0")
    for col, s in report["signals"].items():
        rate = f"{s['hit_rate']:.0%}" if s["hit_rate"] is not None else "—"
        meds = "/".join(str(v) if v is not None else "·"
                        for v in s["median_by_offset"].values())
        print(f"{col:<22} {rate:>8} {s['hits']:>5} {s['misses']:>5} "
              f"{s['unmeasurable']:>6}  {meds}")
    g = report["ignition_grades"]
    print(f"\nignition alerts: hit={g['hit']} late={g['late']} false={g['false']}")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=config.DB_PATH)
    parser.add_argument("--threshold", type=float, default=70.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    db = await init_db(args.db)
    try:
        report = await run_leadlag(db, threshold=args.threshold)
        _print_report(report)
        if args.output:
            with open(args.output, "w") as fh:
                json.dump(report, fh, indent=2)
            print(f"\nwrote {args.output}")
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
