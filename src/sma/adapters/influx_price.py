from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from influxdb_client import InfluxDBClient

from sma.ecopower import (
    FluviusRegion,
    consumption_price_eur_kwh,
    injection_price_eur_kwh,
)


@dataclass(frozen=True)
class PricePoint:
    timestamp: datetime
    epex_eur_mwh: float
    injection_eur_kwh: float
    consumption_eur_kwh: float


class InfluxPriceProvider:
    """Pulls the most recent EPEX day-ahead BE price from InfluxDB and converts
    it to the Ecopower net injection price (€/kWh).

    Bucket layout assumed (matches the ecopower-tariffs project):
      measurement: electricity_price
      tag:         country = "BE"
      field:       price_eur_mwh   (preferred; falls back to price_eur_kwh × 1000)
    """

    def __init__(self, client: InfluxDBClient, org: str, bucket: str = "energy_prices"):
        self._client = client
        self._org = org
        self._bucket = bucket

    def current_injection_price_eur_kwh(self) -> float | None:
        epex = self._latest_epex_eur_mwh()
        if epex is None:
            return None
        return injection_price_eur_kwh(epex)

    def current_consumption_price_eur_kwh(self, region: FluviusRegion) -> float | None:
        epex = self._latest_epex_eur_mwh()
        if epex is None:
            return None
        return consumption_price_eur_kwh(epex, region)

    def time_series(
        self,
        hours_ago: int,
        hours_ahead: int,
        region: FluviusRegion,
    ) -> list[PricePoint]:
        """Pull EPEX day-ahead prices over a window centred on now.

        Day-ahead is published ~16:00 the day before, so by mid-afternoon you
        already have tomorrow's full schedule in Influx. `hours_ahead` should
        be generous (e.g. 30) — Flux returns whatever exists, no error if the
        future window is partially empty.
        """
        flux = f'''
from(bucket: "{self._bucket}")
  |> range(start: -{hours_ago}h, stop: {hours_ahead}h)
  |> filter(fn: (r) => r["_measurement"] == "electricity_price")
  |> filter(fn: (r) => r["country"] == "BE")
  |> filter(fn: (r) => r["_field"] == "price_eur_mwh")
  |> sort(columns: ["_time"])
'''
        result = self._client.query_api().query(org=self._org, query=flux)
        points: list[PricePoint] = []
        for table in result:
            for record in table.records:
                value = record.get_value()
                if value is None:
                    continue
                points.append(PricePoint(
                    timestamp=record.get_time(),
                    epex_eur_mwh=value,
                    injection_eur_kwh=injection_price_eur_kwh(value),
                    consumption_eur_kwh=consumption_price_eur_kwh(value, region),
                ))
        return points

    def _latest_epex_eur_mwh(self) -> float | None:
        # Look back 2 hours to be safe across the quarter-hour boundary.
        start = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Try price_eur_mwh first (canonical per ecopower-tariffs).
        for field, scale in (("price_eur_mwh", 1.0), ("price_eur_kwh", 1000.0)):
            flux = f'''
from(bucket: "{self._bucket}")
  |> range(start: {start})
  |> filter(fn: (r) => r["_measurement"] == "electricity_price")
  |> filter(fn: (r) => r["country"] == "BE")
  |> filter(fn: (r) => r["_field"] == "{field}")
  |> last()
'''
            result = self._client.query_api().query(org=self._org, query=flux)
            for table in result:
                for record in table.records:
                    value = record.get_value()
                    if value is not None:
                        return value * scale
        return None
