"""forecast.solar adapter — free tier (12 calls/hour, 24h horizon).

Endpoint:
  GET https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}

`az` convention: 0 = south, 90 = west, -90 = east, 180 = north (same as evcc).
Response JSON has `result.watts` keyed by timestamp string ("YYYY-MM-DD HH:MM:SS"
in local time), and metadata. We only need the watts series.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger("sma.solar_forecast")


@dataclass(frozen=True)
class SolarForecastPoint:
    timestamp: datetime          # tz-aware (UTC)
    pv_power_w: float


class ForecastSolarProvider:
    def __init__(self, lat: float, lon: float, dec: int, az: int, kwp: float,
                 tz: str = "Europe/Brussels", timeout: float = 10.0):
        self._url = (
            f"https://api.forecast.solar/estimate/"
            f"{lat:.5f}/{lon:.5f}/{dec}/{az}/{kwp}"
        )
        self._tz = ZoneInfo(tz)
        self._timeout = timeout

    def fetch(self) -> list[SolarForecastPoint]:
        req = urllib.request.Request(self._url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            data = json.loads(r.read())
        watts = (data.get("result") or {}).get("watts") or {}
        out: list[SolarForecastPoint] = []
        for ts_str, w in watts.items():
            # local naive → make tz-aware → convert to UTC
            local = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=self._tz)
            out.append(SolarForecastPoint(timestamp=local, pv_power_w=float(w)))
        out.sort(key=lambda p: p.timestamp)
        return out
