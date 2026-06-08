"""Durable log of inverter connection-loss / recovery events.

The SMA comms processor's Modbus-TCP server is known to wedge after some
uptime (see SMAModbusClient docstring). When that happens the daemon drops the
stale client and reconnects on the next tick; each such transition is recorded
here so we can see — and visualise — how often the inverter falls off the bus.

SQLite, one short-lived connection per call: the tick runs on asyncio worker
threads (varies per tick) and HTTP handlers read from yet another thread, so a
shared connection would trip sqlite's same-thread guard. Per-call connections in
WAL mode are safe across threads and cheap at this frequency (a handful of writes
per hour at worst).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, UTC
from pathlib import Path

LOST = "lost"
RECONNECTED = "reconnected"


@dataclass(frozen=True)
class ConnectionEvent:
    timestamp: str   # ISO8601 UTC
    component: str   # "inverter"
    kind: str        # LOST | RECONNECTED
    detail: str


class ConnectionEventStore:
    def __init__(self, db_path: str | Path):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS connection_events (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts        TEXT NOT NULL,
                    component TEXT NOT NULL,
                    kind      TEXT NOT NULL,
                    detail    TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, component: str, kind: str, detail: str = "") -> None:
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO connection_events (ts, component, kind, detail) VALUES (?,?,?,?)",
                (ts, component, kind, detail),
            )

    def recent(self, limit: int = 100) -> list[ConnectionEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT ts, component, kind, detail FROM connection_events "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            ConnectionEvent(r["ts"], r["component"], r["kind"], r["detail"])
            for r in rows
        ]

    def summary(self, component: str = "inverter", window_hours: int = 24) -> dict:
        """Health snapshot for the UI: outage count in the window + current state."""
        since = (datetime.now(UTC) - timedelta(hours=window_hours)).isoformat(timespec="seconds")
        with self._connect() as conn:
            losses = conn.execute(
                "SELECT COUNT(*) AS n FROM connection_events "
                "WHERE component = ? AND kind = ? AND ts >= ?",
                (component, LOST, since),
            ).fetchone()["n"]
            last = conn.execute(
                "SELECT ts, kind, detail FROM connection_events "
                "WHERE component = ? ORDER BY id DESC LIMIT 1",
                (component,),
            ).fetchone()
        return {
            "component": component,
            "window_hours": window_hours,
            "losses_in_window": losses,
            "last_kind": last["kind"] if last else None,
            "last_ts": last["ts"] if last else None,
            "last_detail": last["detail"] if last else None,
        }
