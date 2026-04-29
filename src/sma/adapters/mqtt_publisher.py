"""Publish daemon state to MQTT with Home-Assistant auto-discovery.

On connect:
  - sends one retained discovery message per sensor under
    `homeassistant/<component>/<node>/<object>/config`
On every tick:
  - publishes current values to `sma_curtail/state/<field>` (retained)

State topics:
  sma_curtail/state/active            "1" / "0"
  sma_curtail/state/summary           "released — exporting profitably"
  sma_curtail/state/injection_price   €/kWh
  sma_curtail/state/consumption_price €/kWh
  sma_curtail/state/epex              €/kWh
  sma_curtail/state/pv_power_w        W
  sma_curtail/state/grid_power_w      W (negative = exporting)
  sma_curtail/state/home_power_w      W
  sma_curtail/state/curtail_hours_ahead h
  sma_curtail/state/money_today_net_eur €
"""
from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger("sma.mqtt")

NODE_ID = "sma_curtail"
STATE_PREFIX = f"{NODE_ID}/state"
DEVICE = {
    "identifiers": [NODE_ID],
    "name": "SMA Curtail",
    "manufacturer": "victoor.io",
    "model": "Sunny Boy 4.0 curtailment daemon",
}

# (component, object_id, payload). Payloads omit the discovery topic; built lazily.
SENSORS: list[tuple[str, str, dict]] = [
    ("binary_sensor", "active", {
        "name": "Solar Curtail Active",
        "device_class": "running",
        "state_topic": f"{STATE_PREFIX}/active",
        "payload_on": "1", "payload_off": "0",
    }),
    ("sensor", "summary", {
        "name": "Solar Curtail Summary",
        "state_topic": f"{STATE_PREFIX}/summary",
    }),
    ("sensor", "injection_price", {
        "name": "Solar Injection Price",
        "state_topic": f"{STATE_PREFIX}/injection_price",
        "unit_of_measurement": "€/kWh", "state_class": "measurement",
        "suggested_display_precision": 4,
    }),
    ("sensor", "consumption_price", {
        "name": "Solar Consumption Price",
        "state_topic": f"{STATE_PREFIX}/consumption_price",
        "unit_of_measurement": "€/kWh", "state_class": "measurement",
        "suggested_display_precision": 4,
    }),
    ("sensor", "epex", {
        "name": "EPEX Day-Ahead",
        "state_topic": f"{STATE_PREFIX}/epex",
        "unit_of_measurement": "€/kWh", "state_class": "measurement",
        "suggested_display_precision": 4,
    }),
    ("sensor", "pv_power_w", {
        "name": "Solar PV Power",
        "state_topic": f"{STATE_PREFIX}/pv_power_w",
        "device_class": "power", "unit_of_measurement": "W", "state_class": "measurement",
    }),
    ("sensor", "grid_power_w", {
        "name": "Grid Power (− exporting)",
        "state_topic": f"{STATE_PREFIX}/grid_power_w",
        "device_class": "power", "unit_of_measurement": "W", "state_class": "measurement",
    }),
    ("sensor", "home_power_w", {
        "name": "Home Consumption",
        "state_topic": f"{STATE_PREFIX}/home_power_w",
        "device_class": "power", "unit_of_measurement": "W", "state_class": "measurement",
    }),
    ("sensor", "curtail_hours_ahead", {
        "name": "Forecast Curtail Hours Ahead",
        "state_topic": f"{STATE_PREFIX}/curtail_hours_ahead",
        "unit_of_measurement": "h", "state_class": "measurement",
        "suggested_display_precision": 2,
    }),
    ("sensor", "money_today_net_eur", {
        "name": "Solar Curtail Net Today",
        "state_topic": f"{STATE_PREFIX}/money_today_net_eur",
        "unit_of_measurement": "€", "state_class": "total",
        "suggested_display_precision": 2,
    }),
]


class MQTTPublisher:
    """Connects on construction, publishes on demand, disconnects on close()."""

    def __init__(self, host: str, port: int = 1883,
                 username: str | None = None, password: str | None = None,
                 discovery_prefix: str = "homeassistant"):
        self._host = host
        self._port = port
        self._discovery_prefix = discovery_prefix
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=NODE_ID,
        )
        if username:
            self._client.username_pw_set(username, password)
        self._client.connect(host, port, keepalive=60)
        self._client.loop_start()
        self._publish_discovery()

    def close(self) -> None:
        try:
            self._client.publish(f"{STATE_PREFIX}/online", "0", retain=True).wait_for_publish(timeout=2)
        except Exception: pass  # noqa: BLE001, S110
        try: self._client.loop_stop()
        except Exception: pass  # noqa: BLE001, S110
        try: self._client.disconnect()
        except Exception: pass  # noqa: BLE001, S110

    def _publish_discovery(self) -> None:
        for component, object_id, payload in SENSORS:
            full = {
                **payload,
                "unique_id": f"{NODE_ID}_{object_id}",
                "object_id": f"{NODE_ID}_{object_id}",
                "device": DEVICE,
                "availability_topic": f"{STATE_PREFIX}/online",
                "payload_available": "1", "payload_not_available": "0",
            }
            topic = f"{self._discovery_prefix}/{component}/{NODE_ID}/{object_id}/config"
            self._client.publish(topic, json.dumps(full), retain=True)
        # Online sentinel — flips off in close() so HA marks the device unavailable on graceful exit.
        self._client.publish(f"{STATE_PREFIX}/online", "1", retain=True)

    def publish_state(self, *,
                      curtail: bool,
                      summary: str,
                      injection_price: float | None,
                      consumption_price: float | None,
                      epex_eur_kwh: float | None,
                      pv_w: float | None,
                      grid_w: float | None,
                      home_w: float | None,
                      curtail_hours_ahead: float | None,
                      money_today_net_eur: float | None) -> None:
        def pub(field: str, value) -> None:
            payload = "" if value is None else (
                "1" if value is True else "0" if value is False else f"{value}"
            )
            self._client.publish(f"{STATE_PREFIX}/{field}", payload, retain=True)

        pub("active",            curtail)
        pub("summary",           summary)
        pub("injection_price",   injection_price)
        pub("consumption_price", consumption_price)
        pub("epex",              epex_eur_kwh)
        pub("pv_power_w",        pv_w)
        pub("grid_power_w",      grid_w)
        pub("home_power_w",      home_w)
        pub("curtail_hours_ahead", curtail_hours_ahead)
        pub("money_today_net_eur", money_today_net_eur)
