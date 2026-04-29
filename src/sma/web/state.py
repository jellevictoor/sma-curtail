"""Shared in-memory state between the tick loop and the FastAPI endpoints."""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Iterable

from sma.curtailment import Decision


@dataclass(frozen=True)
class LogEntry:
    timestamp: str   # ISO8601 UTC
    level: str       # "INFO" / "WARNING" / "ERROR"
    logger: str      # "sma.web", "sma.mqtt", etc.
    message: str


class LogBuffer:
    """Thread-safe ring buffer of LogEntry. Populated by AppLogHandler."""

    def __init__(self, max_entries: int = 200):
        self._buf: deque[LogEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def append(self, entry: LogEntry) -> None:
        with self._lock:
            self._buf.append(entry)

    def snapshot(self) -> list[LogEntry]:
        with self._lock:
            return list(self._buf)


class AppLogHandler(logging.Handler):
    """Captures log records into a LogBuffer for surfacing in the UI."""

    def __init__(self, buffer: LogBuffer):
        super().__init__()
        self._buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="seconds")
            self._buffer.append(LogEntry(
                timestamp=ts,
                level=record.levelname,
                logger=record.name,
                message=record.getMessage(),
            ))
        except Exception:  # noqa: BLE001
            pass


@dataclass(frozen=True)
class Sample:
    timestamp: str               # ISO8601, UTC
    curtail: bool
    target_percent: int          # 0..100, what's been written to register 40015
    target_watts: int            # = target_percent × inverter_max / 100
    injection_price_eur_per_kwh: float | None
    consumption_price_eur_per_kwh: float | None
    pv_power_w: float | None
    grid_power_w: float | None
    home_power_w: float | None
    any_loadpoint_charging: bool
    summary: str

    @staticmethod
    def now(decision: Decision,
            injection_price: float | None,
            consumption_price: float | None,
            pv_w: float | None,
            grid_w: float | None,
            home_w: float | None,
            charging: bool) -> Sample:
        return Sample(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            curtail=decision.curtail,
            target_percent=decision.target_percent,
            target_watts=decision.target_watts,
            injection_price_eur_per_kwh=injection_price,
            consumption_price_eur_per_kwh=consumption_price,
            pv_power_w=pv_w,
            grid_power_w=grid_w,
            home_power_w=home_w,
            any_loadpoint_charging=charging,
            summary=decision.summary,
        )


class History:
    """Thread-safe ring buffer of Sample. Max ~24h at 60s tick = 1440 entries."""

    def __init__(self, max_samples: int = 1440):
        self._buf: deque[Sample] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def append(self, sample: Sample) -> None:
        with self._lock:
            self._buf.append(sample)

    def snapshot(self) -> list[Sample]:
        with self._lock:
            return list(self._buf)


def decision_to_rails(decision: Decision) -> list[dict]:
    return [asdict(r) for r in decision.rails]


def history_to_payload(samples: Iterable[Sample]) -> list[dict]:
    return [asdict(s) for s in samples]
