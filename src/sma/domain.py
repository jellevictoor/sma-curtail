from dataclasses import dataclass


@dataclass(frozen=True)
class InverterReading:
    serial_number: int | None
    operating_status: int | None
    total_yield_wh: int | None
    daily_yield_wh: int | None
    operating_time_s: int | None
    feed_in_time_s: int | None
    dc_current_a: float | None
    dc_voltage_v: float | None
    dc_power_w: int | None
    ac_power_w: int | None
    ac_voltage_v: float | None
    grid_frequency_hz: float | None
    device_temperature_c: float | None
