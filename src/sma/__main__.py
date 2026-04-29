import argparse
import sys
from dataclasses import asdict

from sma.client import DEFAULT_UNIT_ID, SMAModbusClient
from sma.registers import REGISTERS

_UNITS = {reg.name: reg.unit for reg in REGISTERS}


def _format(name: str, value: object, unit: str = "") -> str:
    if value is None:
        return f"{name:<22} —"
    if isinstance(value, float):
        return f"{name:<22} {value:>12.2f} {unit}".rstrip()
    return f"{name:<22} {value:>12} {unit}".rstrip()


def cmd_read(client: SMAModbusClient) -> int:
    reading = client.read_all()
    for name, value in asdict(reading).items():
        print(_format(name, value, _UNITS.get(name, "")))
    return 0


def cmd_probe_control(client: SMAModbusClient) -> int:
    control = client.read_control()
    print(_format("active_power_pct",   control.get("active_power_pct"), "%"))
    print(_format("fallback_timeout_s", control.get("fallback_timeout_s"), "s"))
    print(_format("fallback_pct",       control.get("fallback_pct"), "%"))
    return 0


def _confirm(prompt: str, force: bool) -> bool:
    if force:
        return True
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in ("y", "yes")


def cmd_set_pct(client: SMAModbusClient, percent: int, force: bool) -> int:
    if not _confirm(f"Set inverter active-power limit to {percent}%?", force):
        print("aborted")
        return 1
    client.set_active_power_percent(percent)
    print(f"wrote active_power_pct = {percent}%")
    print("note: takes effect only when operating mode = 'External setting active power'.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Read and control SMA Sunny Boy inverter via Modbus TCP")
    parser.add_argument("--host", default="192.168.1.3")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=DEFAULT_UNIT_ID, help="Modbus unit/slave ID (SMA default: 3)")

    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("read",          help="Read all inverter measurements")
    sub.add_parser("probe-control", help="Read power-control registers (read-only)")

    p_set = sub.add_parser("set-pct", help="Set active-power limit (0..100 %)")
    p_set.add_argument("percent", type=int)
    p_set.add_argument("-y", "--yes", action="store_true", help="skip confirmation")

    args = parser.parse_args()

    with SMAModbusClient(args.host, args.port, args.unit) as client:
        if args.cmd == "read":
            return cmd_read(client)
        if args.cmd == "probe-control":
            return cmd_probe_control(client)
        if args.cmd == "set-pct":
            return cmd_set_pct(client, args.percent, args.yes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
