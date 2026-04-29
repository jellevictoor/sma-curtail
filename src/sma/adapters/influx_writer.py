"""Persist daemon samples back to InfluxDB so history survives restarts.

Writes to the same `metering` bucket (no admin permission needed) but under
a distinct measurement: `sma_curtail`. Fields mirror the Sample dataclass.
"""
from __future__ import annotations

from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS


class InfluxSampleWriter:
    def __init__(self, client: InfluxDBClient, org: str, bucket: str = "metering"):
        self._org = org
        self._bucket = bucket
        self._write_api = client.write_api(write_options=SYNCHRONOUS)

    def close(self) -> None:
        try: self._write_api.close()
        except Exception: pass  # noqa: BLE001, S110

    def write_sample(self, *, curtail: bool,
                     target_percent: int,
                     injection_price: float | None,
                     consumption_price: float | None,
                     pv_w: float | None,
                     grid_w: float | None,
                     home_w: float | None,
                     charging: bool) -> None:
        point = Point("sma_curtail").time(datetime.now(timezone.utc), WritePrecision.S)
        point = point.field("curtail", 1 if curtail else 0)
        point = point.field("target_percent", int(target_percent))
        if injection_price is not None: point = point.field("injection_eur_kwh", float(injection_price))
        if consumption_price is not None: point = point.field("consumption_eur_kwh", float(consumption_price))
        if pv_w is not None:   point = point.field("pv_w", float(pv_w))
        if grid_w is not None: point = point.field("grid_w", float(grid_w))
        if home_w is not None: point = point.field("home_w", float(home_w))
        point = point.field("charging", 1 if charging else 0)
        self._write_api.write(bucket=self._bucket, org=self._org, record=point)
