from __future__ import annotations

from sma.registers import DataType, Register

# Active-power-limit setpoint register on SB4.0-1AV-40 firmware 1.3.36.R.
# This is "Normalized active power limitation by PV system ctrl", FIX0 (whole
# percent). Writes only take effect when the operating mode is set to
# "External setting active power" via Sunny Portal/installer; otherwise the
# inverter accepts the protocol-level write but silently ignores the value.
ACTIVE_POWER_PCT  = Register("active_power_pct",  40015, DataType.U32, unit="%")

# Heartbeat fallback registers (read-only confirmation of installer config):
#   41193 — operating mode for absent system control (2506 = "Values maintained")
#   41195 — fallback timeout in seconds (typically 600 = 10 min)
#   41197 — fallback active power % FIX2 (typically 10000 = 100.00%)
FALLBACK_TIMEOUT_S = Register("fallback_timeout_s", 41195, DataType.U32, unit="s")
FALLBACK_PCT       = Register("fallback_pct",       41197, DataType.U32, scale=0.01, unit="%")

CONTROL_REGISTERS = (ACTIVE_POWER_PCT, FALLBACK_TIMEOUT_S, FALLBACK_PCT)

# "Eingestellter Betriebsmodus der Wirkleistungsbegrenzung" enum values.
MODE_OFF       = 303    # No active power limit (default operation)
MODE_LIMIT_W   = 1077   # Limit set as fixed value in Watts
MODE_LIMIT_PCT = 1078   # Limit set as fixed value in percent
MODE_LIMIT_EXT = 1079   # Limit via external PV system control

MODE_NAMES = {
    MODE_OFF: "off (no limit)",
    MODE_LIMIT_W: "limit in W",
    MODE_LIMIT_PCT: "limit in %",
    MODE_LIMIT_EXT: "limit via external control",
}


def u32_to_words(value: int) -> list[int]:
    if value < 0 or value > 0xFFFFFFFF:
        raise ValueError(f"value out of u32 range: {value}")
    return [(value >> 16) & 0xFFFF, value & 0xFFFF]
