from __future__ import annotations

from sma.client import SMAModbusClient


class ModbusActuator:
    """Wraps SMAModbusClient to expose a simple `set_percent(0..100)` API.

    The daemon calls this on every tick (curtailed or not) — when state is
    unchanged the call still serves as a heartbeat that keeps the inverter's
    External-setpoint mode alive. The inverter's internal watchdog reverts
    to 100 % if no write arrives within ~600 s.
    """

    def __init__(self, client: SMAModbusClient):
        self._client = client

    def set_percent(self, percent: int) -> None:
        self._client.set_active_power_percent(percent)
