"""InfluxDB adapter for the household p1meter readings.

Bucket layout (matches the ecopower-tariffs project):
  bucket:        metering
  measurement:   energy
  tag:           device = "p1meter"
  fields:
    PowerDelivered  — grid → home (kW consumption)
    PowerReturned   — home → grid (kW injection)
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from influxdb_client import InfluxDBClient


@dataclass(frozen=True)
class GridPoint:
    timestamp: datetime
    grid_power_w: float    # negative = exporting to grid (evcc convention)


class InfluxMeteringProvider:
    def __init__(self, client: InfluxDBClient, org: str, bucket: str = "metering"):
        self._client = client
        self._org = org
        self._bucket = bucket

    def current_grid_power_w(self) -> float | None:
        """Most recent p1meter grid power (W) — fallback when evcc has no grid meter.

        Positive = importing from grid, negative = exporting (evcc convention).
        """
        flux = f'''
delivered = from(bucket: "{self._bucket}")
  |> range(start: -5m)
  |> filter(fn: (r) => r["_measurement"] == "energy")
  |> filter(fn: (r) => r["device"] == "p1meter")
  |> filter(fn: (r) => r["_field"] == "PowerDelivered")
  |> last()

returned = from(bucket: "{self._bucket}")
  |> range(start: -5m)
  |> filter(fn: (r) => r["_measurement"] == "energy")
  |> filter(fn: (r) => r["device"] == "p1meter")
  |> filter(fn: (r) => r["_field"] == "PowerReturned")
  |> last()

union(tables: [delivered, returned])
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
'''
        result = self._client.query_api().query(org=self._org, query=flux)
        for table in result:
            for record in table.records:
                pd = (record.values.get("PowerDelivered") or 0.0) * 1000.0
                pr = (record.values.get("PowerReturned") or 0.0) * 1000.0
                return pd - pr
        return None

    def todays_curtail_samples(self) -> list[dict]:
        """All `sma_curtail` samples persisted since local midnight today.

        Used to seed the in-memory ring buffer at startup and to compute
        money_today across daemon restarts. Returns dicts shaped like Sample.
        """
        flux = f'''
from(bucket: "{self._bucket}")
  |> range(start: today())
  |> filter(fn: (r) => r["_measurement"] == "sma_curtail")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
        result = self._client.query_api().query(org=self._org, query=flux)
        out = []
        for table in result:
            for record in table.records:
                v = record.values
                curtail = bool(v.get("curtail", 0))
                target_percent = v.get("target_percent")
                if target_percent is None:    # legacy rows: binary 0/100
                    target_percent = 0 if curtail else 100
                out.append({
                    "timestamp": record.get_time().isoformat(timespec="seconds"),
                    "curtail":   curtail,
                    "target_percent": int(target_percent),
                    "injection_price_eur_per_kwh":   v.get("injection_eur_kwh"),
                    "consumption_price_eur_per_kwh": v.get("consumption_eur_kwh"),
                    "pv_power_w":   v.get("pv_w"),
                    "grid_power_w": v.get("grid_w"),
                    "home_power_w": v.get("home_w"),
                    "any_loadpoint_charging": bool(v.get("charging", 0)),
                })
        return out

    def grid_history(self, hours_ago: int = 24, every: str = "5m") -> list[GridPoint]:
        """Returns aggregated grid power (W) over the last `hours_ago` hours.

        Convention: positive = importing, negative = exporting (evcc-style).
        """
        flux = f'''
delivered = from(bucket: "{self._bucket}")
  |> range(start: -{hours_ago}h)
  |> filter(fn: (r) => r["_measurement"] == "energy")
  |> filter(fn: (r) => r["device"] == "p1meter")
  |> filter(fn: (r) => r["_field"] == "PowerDelivered")
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)

returned = from(bucket: "{self._bucket}")
  |> range(start: -{hours_ago}h)
  |> filter(fn: (r) => r["_measurement"] == "energy")
  |> filter(fn: (r) => r["device"] == "p1meter")
  |> filter(fn: (r) => r["_field"] == "PowerReturned")
  |> aggregateWindow(every: {every}, fn: mean, createEmpty: false)

union(tables: [delivered, returned])
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])
'''
        result = self._client.query_api().query(org=self._org, query=flux)
        out: list[GridPoint] = []
        for table in result:
            for record in table.records:
                values = record.values
                pd = values.get("PowerDelivered")  # kW import
                pr = values.get("PowerReturned")   # kW export
                if pd is None and pr is None:
                    continue
                pd_w = (pd or 0.0) * 1000.0
                pr_w = (pr or 0.0) * 1000.0
                grid_w = pd_w - pr_w   # positive = importing, negative = exporting
                out.append(GridPoint(timestamp=record.get_time(), grid_power_w=grid_w))
        return out
