from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class EvccSnapshot:
    """Subset of evcc state the curtailment decision needs."""
    feed_in_price_eur_per_kwh: float | None
    grid_price_eur_per_kwh: float | None
    pv_power_w: float | None
    home_power_w: float | None
    grid_power_w: float | None  # negative = exporting (evcc convention)
    # Loadpoint signals — "managed" = mode != "off" (so under evcc's surplus control)
    any_loadpoint_connected: bool       # any managed loadpoint plugged in
    active_loadpoint_charge_power_w: float  # sum of chargePower across managed loadpoints
    # Convenience boolean for legacy consumers (UI, history): true iff active power > 0
    any_loadpoint_charging: bool


class EvccMCPClient:
    """Minimal MCP-over-HTTP client for evcc's getState tool."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self._url = base_url.rstrip("/")
        self._timeout = timeout
        self._sid: str | None = None

    def __enter__(self) -> EvccMCPClient:
        self._initialize()
        return self

    def __exit__(self, *exc: object) -> None:
        self._sid = None  # MCP HTTP sessions self-expire; nothing to close.

    def snapshot(self) -> EvccSnapshot:
        # We need three things per loadpoint:
        #   mode         — "off" = passive uncontrolled, anything else = managed by evcc
        #   connected    — is a vehicle plugged in?
        #   chargePower  — actual current consumption
        #
        # `homePower` in evcc deliberately *excludes* loadpoints, so a heat-pump
        # in mode=off is invisible to `homePower` despite drawing kWh — we add
        # mode=off chargePower back in as part of "home".
        #
        # `any_loadpoint_charging` is broadened: it's True if any loadpoint is
        # actively charging OR is connected in a surplus-absorbing mode (i.e.,
        # would charge if PV surplus were exposed to evcc). This avoids the
        # catch-22 where curtailing hides the surplus and evcc never starts.
        jq = (
            "{tariffFeedIn, tariffGrid, pvPower, homePower, gridPower, "
            "loadpoints: [.loadpoints[] | {mode, charging: (.charging // false), "
            "connected: (.connected // false), chargePower: (.chargePower // 0)}]}"
        )
        result = self._call_tool("getState", {"jq": jq})
        text = result["content"][0]["text"]
        body = text.split("Response:\n", 1)[1] if "Response:\n" in text else text
        state = json.loads(body)

        loadpoints = state.get("loadpoints") or []
        unmanaged_load_w = sum(
            float(lp.get("chargePower") or 0)
            for lp in loadpoints
            if (lp.get("mode") or "off") == "off"
        )
        raw_home = state.get("homePower")
        total_home = (raw_home or 0) + unmanaged_load_w if raw_home is not None else None

        managed = [lp for lp in loadpoints if (lp.get("mode") or "off") != "off"]
        active_w = sum(float(lp.get("chargePower") or 0) for lp in managed)
        any_connected = any(lp.get("connected") for lp in managed)

        return EvccSnapshot(
            feed_in_price_eur_per_kwh=state.get("tariffFeedIn"),
            grid_price_eur_per_kwh=state.get("tariffGrid"),
            pv_power_w=state.get("pvPower"),
            home_power_w=total_home,
            grid_power_w=state.get("gridPower"),
            any_loadpoint_connected=any_connected,
            active_loadpoint_charge_power_w=active_w,
            any_loadpoint_charging=active_w > 0,
        )

    def _initialize(self) -> None:
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "sma-curtail", "version": "0.1"},
            },
        }
        sid, _ = self._post(body, sid=None)
        if not sid:
            raise RuntimeError(f"evcc MCP at {self._url} did not return a session id")
        self._sid = sid
        # MCP requires the initialized notification before further calls
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)

    def _call_tool(self, name: str, arguments: dict) -> dict:
        if self._sid is None:
            raise RuntimeError("not initialized")
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        _, raw = self._post(body, self._sid)
        for line in raw.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                if "error" in payload:
                    raise RuntimeError(f"evcc MCP error: {payload['error']}")
                return payload["result"]
        raise RuntimeError(f"evcc MCP: no SSE data line in response: {raw!r}")

    def _post(self, body: dict, sid: str | None) -> tuple[str | None, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid:
            headers["mcp-session-id"] = sid
        req = urllib.request.Request(self._url, data=json.dumps(body).encode(),
                                     method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return r.headers.get("mcp-session-id"), r.read().decode(errors="replace")
