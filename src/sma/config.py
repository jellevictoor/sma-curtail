"""Daemon configuration loaded from environment variables (.env supported)."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    inverter_host: str
    inverter_port: int
    inverter_unit_id: int
    inverter_max_power_w: int
    inverter_transport: str        # "tcp" or "udp"

    influx_url: str
    influx_token: str
    influx_org: str
    influx_bucket: str
    influx_metering_bucket: str

    evcc_mcp_url: str

    tick_seconds: int
    enter_below_eur_kwh: float
    exit_above_eur_kwh: float

    log_level: str
    web_port: int
    fluvius_region: str
    modbus_heartbeat_seconds: int
    home_power_ema_alpha: float
    target_deadband_percent: int

    mqtt_host: str | None
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_discovery_prefix: str

    solar_lat: float | None
    solar_lon: float | None
    solar_dec: int
    solar_az: int
    solar_kwp: float

    @staticmethod
    def from_env() -> Config:
        return Config(
            inverter_host=_get("INVERTER_HOST", "192.168.1.3"),
            inverter_port=int(_get("INVERTER_PORT", "502")),
            inverter_unit_id=int(_get("INVERTER_UNIT_ID", "3")),
            inverter_max_power_w=int(_get("INVERTER_MAX_POWER_W", "4000")),
            inverter_transport=_get("INVERTER_TRANSPORT", "tcp").lower(),
            influx_url=_get("INFLUX_URL", "http://192.168.1.5:8086"),
            influx_token=_required("INFLUX_TOKEN"),
            influx_org=_get("INFLUX_ORG", "victoor.io"),
            influx_bucket=_get("INFLUX_BUCKET", "energy_prices"),
            influx_metering_bucket=_get("INFLUX_METERING_BUCKET", "metering"),
            evcc_mcp_url=_get("EVCC_MCP_URL", "https://evcc.victoor.io/mcp"),
            tick_seconds=int(_get("TICK_SECONDS", "15")),
            enter_below_eur_kwh=float(_get("ENTER_BELOW_EUR_KWH", "-0.001")),
            exit_above_eur_kwh=float(_get("EXIT_ABOVE_EUR_KWH", "0.005")),
            log_level=_get("LOG_LEVEL", "INFO"),
            web_port=int(_get("WEB_PORT", "8080")),
            fluvius_region=_get("FLUVIUS_REGION", "WEST"),
            # Inverter watchdog is 600 s; 300 s gives 2× margin and minimises writes.
            modbus_heartbeat_seconds=int(_get("MODBUS_HEARTBEAT_SECONDS", "300")),
            # EMA smoothing factor for home_power readings before they reach the
            # match-load decision. 1.0 = raw (no smoothing), 0.3 = ~3-sample window
            # (≈ 45 s at 15 s tick), 0.1 = ~10-sample window (≈ 150 s). Smooths out
            # transients (washing machine startup, EV inrush) so the target percent
            # doesn't oscillate.
            home_power_ema_alpha=float(_get("HOME_POWER_EMA_ALPHA", "0.15")),
            # Deadband on the match-load percent: ignore changes smaller than this.
            # Keeps the inverter setpoint stable through small home-power jitter.
            target_deadband_percent=int(_get("TARGET_DEADBAND_PERCENT", "3")),
            mqtt_host=_optional("MQTT_HOST"),
            mqtt_port=int(_get("MQTT_PORT", "1883")),
            mqtt_username=_optional("MQTT_USERNAME"),
            mqtt_password=_optional("MQTT_PASSWORD"),
            mqtt_discovery_prefix=_get("MQTT_DISCOVERY_PREFIX", "homeassistant"),
            solar_lat=_optional_float("SOLAR_LAT"),
            solar_lon=_optional_float("SOLAR_LON"),
            solar_dec=int(_get("SOLAR_DEC", "35")),
            solar_az=int(_get("SOLAR_AZ", "0")),
            solar_kwp=float(_get("SOLAR_KWP", "4.0")),
        )


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required env var not set: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _optional_float(name: str) -> float | None:
    value = _optional(name)
    return float(value) if value is not None else None
