from dataclasses import dataclass
from enum import Enum


class DataType(Enum):
    U32 = "u32"
    S32 = "s32"
    U64 = "u64"
    ENUM = "enum"


REGISTER_COUNT = {
    DataType.U32: 2,
    DataType.S32: 2,
    DataType.ENUM: 2,
    DataType.U64: 4,
}

_NAN_U32 = {0xFFFFFFFF, 0xFFFFFFFE}
_NAN_S32 = {-(1 << 31)}
_NAN_U64 = {0xFFFFFFFFFFFFFFFF}


@dataclass(frozen=True)
class Register:
    name: str
    address: int
    data_type: DataType
    scale: float = 1.0
    unit: str = ""


REGISTERS: tuple[Register, ...] = (
    Register("serial_number",        30057, DataType.U32),
    Register("operating_status",     30201, DataType.ENUM),
    Register("total_yield_wh",       30513, DataType.U64, unit="Wh"),
    Register("daily_yield_wh",       30517, DataType.U64, unit="Wh"),
    Register("operating_time_s",     30521, DataType.U64, unit="s"),
    Register("feed_in_time_s",       30525, DataType.U64, unit="s"),
    Register("dc_current_a",         30769, DataType.S32, scale=0.001, unit="A"),
    Register("dc_voltage_v",         30771, DataType.S32, scale=0.01,  unit="V"),
    Register("dc_power_w",           30773, DataType.S32, unit="W"),
    Register("ac_power_w",           30775, DataType.S32, unit="W"),
    Register("ac_voltage_v",         30783, DataType.U32, scale=0.01,  unit="V"),
    Register("grid_frequency_hz",    30803, DataType.U32, scale=0.01,  unit="Hz"),
    Register("device_temperature_c", 30953, DataType.S32, scale=0.1,   unit="°C"),
)


def decode(register: Register, raw: list[int]) -> float | int | None:
    """Decode raw 16-bit register words into a scaled value, or None if NaN."""
    expected = REGISTER_COUNT[register.data_type]
    if len(raw) != expected:
        raise ValueError(f"{register.name}: expected {expected} words, got {len(raw)}")

    if register.data_type in (DataType.U32, DataType.ENUM):
        value = (raw[0] << 16) | raw[1]
        if value in _NAN_U32:
            return None
    elif register.data_type == DataType.S32:
        value = (raw[0] << 16) | raw[1]
        if value >= (1 << 31):
            value -= 1 << 32
        if value in _NAN_S32:
            return None
    elif register.data_type == DataType.U64:
        value = (raw[0] << 48) | (raw[1] << 32) | (raw[2] << 16) | raw[3]
        if value in _NAN_U64:
            return None
    else:
        raise ValueError(f"unsupported data type: {register.data_type}")

    if register.scale != 1.0:
        return value * register.scale
    return value
