from __future__ import annotations

from dataclasses import fields
from types import TracebackType
from typing import Literal

from pymodbus.client import ModbusTcpClient, ModbusUdpClient

from sma.control import (
    ACTIVE_POWER_PCT,
    CONTROL_REGISTERS,
    u32_to_words,
)
from sma.domain import InverterReading
from sma.registers import REGISTER_COUNT, REGISTERS, Register, decode


# SMA Sunny Boy default Modbus unit (slave) ID. Configurable in Sunny Explorer.
DEFAULT_UNIT_ID = 3

Transport = Literal["tcp", "udp"]


class SMAModbusClient:
    """Modbus client for SMA inverters. Supports both TCP and UDP transports.

    UDP is useful when the inverter's TCP server hangs (a known issue on older
    SMA firmware) — UDP is stateless so it bypasses the connection-state issues
    that can wedge the TCP server on the comms processor.
    """

    def __init__(self, host: str, port: int = 502, unit_id: int = DEFAULT_UNIT_ID,
                 timeout: float = 5.0, transport: Transport = "tcp"):
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._transport = transport
        if transport == "udp":
            self._client = ModbusUdpClient(host=host, port=port, timeout=timeout)
        else:
            self._client = ModbusTcpClient(host=host, port=port, timeout=timeout)

    def __enter__(self) -> SMAModbusClient:
        if not self._client.connect():
            raise ConnectionError(
                f"could not connect to SMA inverter at {self._host}:{self._port} ({self._transport})"
            )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._client.close()

    def read_all(self) -> InverterReading:
        values = {reg.name: self._read(reg) for reg in REGISTERS}
        return InverterReading(**{f.name: values[f.name] for f in fields(InverterReading)})

    def read_control(self) -> dict[str, float | int | None]:
        return {reg.name: self._read(reg) for reg in CONTROL_REGISTERS}

    def set_active_power_percent(self, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError(f"percent must be 0..100, got {percent}")
        self._write_u32(ACTIVE_POWER_PCT, percent)

    def _read(self, register: Register) -> float | int | None:
        count = REGISTER_COUNT[register.data_type]
        response = self._client.read_holding_registers(
            address=register.address,
            count=count,
            slave=self._unit_id,
        )
        if response.isError():
            raise RuntimeError(f"modbus error reading {register.name} @ {register.address}: {response}")
        return decode(register, response.registers)

    def _write_u32(self, register: Register, value: int) -> None:
        response = self._client.write_registers(
            address=register.address,
            values=u32_to_words(value),
            slave=self._unit_id,
        )
        if response.isError():
            raise RuntimeError(
                f"modbus write failed for {register.name} @ {register.address}: {response}"
            )
