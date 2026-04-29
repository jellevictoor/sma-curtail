"""Look back through EPEX history and classify each hour into:
    - profitable    : injection >= 0  → run 100%
    - grey zone     : injection < 0 AND consumption >= 0  → match-load
    - paid import   : consumption < 0 → full curtail (the new rail)

Also overlays p1meter grid power so we can see what was actually happening
during grey-zone windows (were we exporting? how much?).

Run: uv run python scripts/backtest_grey_zone.py [--since 2025-09-01]
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from influxdb_client import InfluxDBClient

from sma.ecopower import (
    FluviusRegion,
    consumption_price_eur_kwh,
    injection_price_eur_kwh,
)


def regime(injection: float, consumption: float) -> str:
    if consumption < 0:
        return "paid-import"
    if injection < 0:
        return "grey-zone"
    return "profitable"


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--since", default="2025-09-01T00:00:00Z")
    args = p.parse_args()

    region = FluviusRegion[os.environ["FLUVIUS_REGION"].upper()]

    client = InfluxDBClient(
        url=os.environ["INFLUX_URL"],
        token=os.environ["INFLUX_TOKEN"],
        org=os.environ["INFLUX_ORG"],
    )

    # 1) all EPEX prices since cutoff
    flux_prices = f'''
from(bucket: "energy_prices")
  |> range(start: {args.since})
  |> filter(fn: (r) => r["_measurement"] == "electricity_price")
  |> filter(fn: (r) => r["country"] == "BE")
  |> filter(fn: (r) => r["_field"] == "price_eur_mwh")
  |> sort(columns: ["_time"])
'''
    rows = []
    for table in client.query_api().query(org=os.environ["INFLUX_ORG"], query=flux_prices):
        for rec in table.records:
            epex = rec.get_value()
            if epex is None:
                continue
            inj = injection_price_eur_kwh(epex)
            con = consumption_price_eur_kwh(epex, region)
            rows.append((rec.get_time(), epex, inj, con, regime(inj, con)))

    if not rows:
        print("no price data in window")
        return

    # 2) regime totals (each row is a quarter-hour or hourly slot)
    counts: dict[str, int] = defaultdict(int)
    for _, _, _, _, r in rows:
        counts[r] += 1
    total = sum(counts.values())

    # Infer slot duration from first two timestamps to convert counts -> hours.
    if len(rows) >= 2:
        delta_s = (rows[1][0] - rows[0][0]).total_seconds()
    else:
        delta_s = 3600
    hours_per_slot = delta_s / 3600.0

    print(f"Window: {rows[0][0].isoformat()}  →  {rows[-1][0].isoformat()}")
    print(f"Slot length: {delta_s:.0f}s  ({len(rows)} slots, {total*hours_per_slot:.0f}h total)")
    print()
    print("Regime counts:")
    for r in ("profitable", "grey-zone", "paid-import"):
        c = counts.get(r, 0)
        h = c * hours_per_slot
        pct = 100 * c / total if total else 0
        print(f"  {r:12s}  {c:6d} slots  ({h:6.1f} h, {pct:5.2f}%)")
    print()

    # 3) grey-zone deep dive — distribution by hour-of-day, worst windows
    grey = [(t, e, i, c) for (t, e, i, c, r) in rows if r == "grey-zone"]
    if not grey:
        print("no grey-zone hours in window")
    else:
        print(f"Grey zone: {len(grey)} slots = {len(grey)*hours_per_slot:.1f}h")
        print(f"  EPEX range:        {min(g[1] for g in grey):+.1f} → {max(g[1] for g in grey):+.1f} €/MWh")
        print(f"  injection range:   {min(g[2] for g in grey):+.4f} → {max(g[2] for g in grey):+.4f} €/kWh")
        print(f"  consumption range: {min(g[3] for g in grey):+.4f} → {max(g[3] for g in grey):+.4f} €/kWh")
        print()

        by_hod: dict[int, int] = defaultdict(int)
        for t, *_ in grey:
            by_hod[t.astimezone(timezone.utc).hour] += 1
        print("  Distribution by hour-of-day (UTC):")
        for h in range(24):
            n = by_hod.get(h, 0)
            bar = "#" * min(50, n)
            print(f"    {h:02d}h  {n:4d}  {bar}")
        print()

        by_month: dict[str, int] = defaultdict(int)
        for t, *_ in grey:
            by_month[t.strftime("%Y-%m")] += 1
        print("  Distribution by month:")
        for m in sorted(by_month):
            n = by_month[m]
            bar = "#" * min(50, n // 2)
            print(f"    {m}  {n:4d}  {bar}")
        print()

    # 4) paid-import deep dive
    paid = [(t, e, i, c) for (t, e, i, c, r) in rows if r == "paid-import"]
    if paid:
        print(f"Paid-import: {len(paid)} slots = {len(paid)*hours_per_slot:.1f}h")
        print(f"  EPEX range:        {min(p[1] for p in paid):+.1f} → {max(p[1] for p in paid):+.1f} €/MWh")
        print(f"  consumption range: {min(p[3] for p in paid):+.4f} → {max(p[3] for p in paid):+.4f} €/kWh")
        print()
        by_day: dict[str, list] = defaultdict(list)
        for t, e, i, c in paid:
            by_day[t.strftime("%Y-%m-%d")].append((t, e, c))
        print(f"  Days with paid-import:")
        for day in sorted(by_day):
            entries = by_day[day]
            min_epex = min(e for _, e, _ in entries)
            min_cons = min(c for _, _, c in entries)
            print(f"    {day}  {len(entries):3d} slots  worst EPEX {min_epex:+7.1f} €/MWh  consumption {min_cons:+.4f} €/kWh")
        print()

    # 5) overlay grid power during grey-zone windows: were we exporting?
    print("Querying p1meter for grey-zone overlap (this may take a moment)...")
    flux_grid = f'''
delivered = from(bucket: "metering")
  |> range(start: {args.since})
  |> filter(fn: (r) => r["_measurement"] == "energy")
  |> filter(fn: (r) => r["device"] == "p1meter")
  |> filter(fn: (r) => r["_field"] == "PowerDelivered")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)

returned = from(bucket: "metering")
  |> range(start: {args.since})
  |> filter(fn: (r) => r["_measurement"] == "energy")
  |> filter(fn: (r) => r["device"] == "p1meter")
  |> filter(fn: (r) => r["_field"] == "PowerReturned")
  |> aggregateWindow(every: 1h, fn: mean, createEmpty: false)

union(tables: [delivered, returned])
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
    grid_by_hour: dict[str, float] = {}
    for table in client.query_api().query(org=os.environ["INFLUX_ORG"], query=flux_grid):
        for rec in table.records:
            v = rec.values
            pd = (v.get("PowerDelivered") or 0.0) * 1000.0
            pr = (v.get("PowerReturned") or 0.0) * 1000.0
            grid_by_hour[rec.get_time().strftime("%Y-%m-%dT%H")] = pd - pr  # +import / -export

    if grey and grid_by_hour:
        # Bucket grey slots into hourly keys, average grid_w per slot.
        export_kwh_grey = 0.0
        loss_eur_grey  = 0.0  # had we kept exporting at injection price
        save_eur_grey  = 0.0  # what match-load saved by NOT exporting
        slots_with_data = 0
        for t, _, inj, _ in grey:
            key = t.strftime("%Y-%m-%dT%H")
            grid_w = grid_by_hour.get(key)
            if grid_w is None:
                continue
            slots_with_data += 1
            if grid_w < 0:  # exporting
                exp_kwh = (-grid_w / 1000.0) * hours_per_slot
                export_kwh_grey += exp_kwh
                loss_eur_grey += exp_kwh * inj  # inj is negative → loss is negative
        print(f"Grey-zone slots with grid data: {slots_with_data}/{len(grey)}")
        print(f"  Exported during grey zone: {export_kwh_grey:.1f} kWh")
        print(f"  Cost of that export at injection prices: {loss_eur_grey:.2f} €")
        print(f"  → that's the money you would have saved with match-load instead.")
        print()

    if paid and grid_by_hour:
        export_kwh_paid = 0.0
        loss_eur_paid   = 0.0  # exporting at very negative injection
        miss_eur_paid   = 0.0  # imports we'd have been paid for if we'd curtailed
        slots_with_data = 0
        for t, _, inj, cons in paid:
            key = t.strftime("%Y-%m-%dT%H")
            grid_w = grid_by_hour.get(key)
            if grid_w is None:
                continue
            slots_with_data += 1
            if grid_w < 0:  # exporting at -EPEX moments
                exp_kwh = (-grid_w / 1000.0) * hours_per_slot
                export_kwh_paid += exp_kwh
                loss_eur_paid += exp_kwh * inj
                # If we'd curtailed instead, that exp_kwh would have been imported (paid).
                # Plus home_load also imported. We can't separate without home data here;
                # underestimate by counting exported kWh as paid imports.
                miss_eur_paid += exp_kwh * (-cons)  # cons negative → +€
        print(f"Paid-import slots with grid data: {slots_with_data}/{len(paid)}")
        print(f"  Exported during paid-import: {export_kwh_paid:.1f} kWh")
        print(f"  Cost of that export at injection prices: {loss_eur_paid:.2f} €")
        print(f"  Plus missed paid-import (lower bound): {miss_eur_paid:.2f} €")
        print(f"  → total recoverable with new 0% rail (lower bound): {loss_eur_paid + miss_eur_paid:.2f} €")


if __name__ == "__main__":
    main()
