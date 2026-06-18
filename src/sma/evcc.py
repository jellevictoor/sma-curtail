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


class EvccClient:
    """Reads evcc state over its native REST API (`GET /api/state`)."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self._url = base_url.rstrip("/") + "/api/state"
        self._timeout = timeout

    def __enter__(self) -> EvccClient:
        self.snapshot()  # fail fast if unreachable, mirroring the old MCP handshake
        return self

    def __exit__(self, *exc: object) -> None:
        return None

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
        state = self._get_state()

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

    def _get_state(self) -> dict:
        req = urllib.request.Request(self._url, method="GET")
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            return json.loads(r.read().decode(errors="replace"))
