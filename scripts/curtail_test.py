#!/usr/bin/env python3
"""Quick curtailment test: drop AC to 0 for N seconds, then release.

Usage:
    python scripts/curtail_test.py [DURATION_S]   # default 60

Reads register 30775 (AC power) every second so you can correlate
with your monitoring graphs.
"""
from __future__ import annotations

import sys
import time

from pymodbus.client import ModbusTcpClient

HOST = "192.168.1.3"
PORT = 502
UNIT = 3


def read_ac_w(client: ModbusTcpClient) -> int | None:
    r = client.read_holding_registers(address=30775, count=2, slave=UNIT)
    if r.isError():
        return None
    w = r.registers
    v = (w[0] << 16) | w[1]
    if v >= (1 << 31):
        v -= 1 << 32
    return v


def write_pct(client: ModbusTcpClient, pct: int) -> bool:
    hi, lo = (pct >> 16) & 0xFFFF, pct & 0xFFFF
    resp = client.write_registers(address=40015, values=[hi, lo], slave=UNIT)
    return not resp.isError()


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def hold(client: ModbusTcpClient, label: str, seconds: int) -> None:
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(1)
        elapsed = int(time.time() - t0)
        print(f"{stamp()}  {label}  +{elapsed:>3}s  AC = {read_ac_w(client)} W")


def main() -> int:
    duration = int(sys.argv[1]) if len(sys.argv) > 1 else 60

    client = ModbusTcpClient(host=HOST, port=PORT, timeout=5)
    if not client.connect():
        print(f"could not connect to {HOST}:{PORT}", file=sys.stderr)
        return 1

    try:
        print(f"{stamp()}  baseline AC = {read_ac_w(client)} W")

        print(f"\n{stamp()}  >>> CURTAIL 0% for {duration}s <<<")
        if not write_pct(client, 0):
            print("write failed", file=sys.stderr)
            return 1
        hold(client, "curtailed", duration)

        print(f"\n{stamp()}  >>> RELEASE 100% <<<")
        if not write_pct(client, 100):
            print("release failed", file=sys.stderr)
            return 1
        hold(client, "released ", 20)

        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
