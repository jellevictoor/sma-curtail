"""Curtailment decision logic — pure domain.

The rule:
    curtail iff
        PV is producing               (else nothing to curtail)
      AND home is exporting to grid   (else PV is consumed locally → no harm)
      AND no flexible load absorbing  (let evcc finish absorbing surplus first)
      AND injection price < threshold (and not within hysteresis exit band)

When all rails pass, instead of fully shutting off the inverter (binary 0%),
we set the inverter's active-power-limit to **match the current home load** —
the inverter produces exactly enough to cover home consumption, with neither
export nor import. This dominates binary curtailment economically:

    binary 0%:   import all home_w at consumption price
    match-load:  produce home_w, no grid involvement → cost 0
    release:     export the surplus at the loss-making injection price

`decide()` returns a structured Decision with a `target_percent` field and
the rail-by-rail reasoning the UI renders.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CurtailmentPolicy:
    enter_below_eur_per_kwh: float = -0.001
    exit_above_eur_per_kwh: float = +0.005
    pv_active_threshold_w: float = 100.0
    exporting_threshold_w: float = 200.0
    inverter_max_power_w: int = 4000           # SB4.0-1AV-40 nameplate
    target_deadband_percent: int = 0           # snap back to previous target if delta < deadband

    def __post_init__(self) -> None:
        if self.enter_below_eur_per_kwh >= self.exit_above_eur_per_kwh:
            raise ValueError("enter_below must be strictly less than exit_above")


@dataclass(frozen=True)
class CurtailmentInputs:
    injection_price_eur_per_kwh: float | None
    pv_power_w: float | None
    grid_power_w: float | None              # negative = exporting
    any_loadpoint_charging: bool
    home_power_w: float | None = None
    consumption_price_eur_per_kwh: float | None = None
    estimated_uncurtailed_pv_w: float | None = None
    last_target_percent: int | None = None  # for deadband: previous target percent if curtailing


@dataclass(frozen=True)
class RailCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class Decision:
    curtail: bool                       # True iff target_percent < 100
    target_percent: int                 # 0..100, what we write to register 40015
    target_watts: int                   # informational: target_percent * inverter_max / 100
    rails: tuple[RailCheck, ...]
    summary: str


def decide(state: bool, inputs: CurtailmentInputs, policy: CurtailmentPolicy) -> Decision:
    rails: list[RailCheck] = []

    def _release(reason: str) -> Decision:
        return Decision(
            curtail=False, target_percent=100,
            target_watts=policy.inverter_max_power_w,
            rails=tuple(rails), summary=reason,
        )

    # Rail 1: price known
    price = inputs.injection_price_eur_per_kwh
    if price is None:
        rails.append(RailCheck("price",  False, "no injection price available"))
        return _release("released — price unknown (fail-safe)")
    rails.append(RailCheck("price", True, f"injection price {price:+.4f} €/kWh"))

    # Rail 2: PV actually producing
    pv = inputs.pv_power_w
    if pv is None or pv < policy.pv_active_threshold_w:
        rails.append(RailCheck(
            "pv", False,
            f"PV not producing (pv={pv:.0f} W < {policy.pv_active_threshold_w:.0f} W)"
            if pv is not None else "PV reading unavailable"))
        return _release("released — PV not producing")
    rails.append(RailCheck("pv", True, f"PV producing ({pv:.0f} W)"))

    # Rail 2.5: consumption price negative → full curtail, short-circuit.
    # We're paid to import, so any kWh of solar produced displaces a kWh of paid
    # import. Optimal: 0% solar, let home + evcc loads pull from grid. This
    # overrides the loadpoint rail: even if evcc is charging the EV, we want
    # the EV's draw to come from grid (paid) not from solar (lost revenue).
    cons = inputs.consumption_price_eur_per_kwh
    if cons is not None and cons < 0:
        rails.append(RailCheck(
            "consumption-price", True,
            f"consumption {cons:+.4f} €/kWh — paid to import, full curtail",
        ))
        return Decision(
            curtail=True, target_percent=0, target_watts=0,
            rails=tuple(rails),
            summary=f"limited to 0% — paid to import at {cons:+.4f} €/kWh",
        )
    rails.append(RailCheck(
        "consumption-price", True,
        f"consumption {cons:+.4f} €/kWh" if cons is not None
        else "consumption price unavailable",
    ))

    # Rail 3: no loadpoint engaged. "Engaged" = actively charging OR plugged-in
    # in a surplus-absorbing mode (i.e., would charge if PV surplus were exposed
    # to evcc). Releasing in the latter case avoids the catch-22 where curtailing
    # hides the surplus from evcc → evcc never starts → loadpoint stays idle.
    if inputs.any_loadpoint_charging:
        rails.append(RailCheck("loadpoint", False,
                               "evcc loadpoint engaged — releasing so evcc can absorb the surplus"))
        return _release("released — letting evcc manage the loadpoint")
    rails.append(RailCheck("loadpoint", True, "no loadpoint engaged"))

    # Rail 4: actually exporting to grid (skipped while already curtailed)
    if state:
        rails.append(RailCheck("export", True, "skipped (already curtailed → grid reads ~0)"))
    else:
        grid = inputs.grid_power_w
        if grid is None:
            rails.append(RailCheck("export", False, "grid meter reading unavailable"))
            return _release("released — grid reading unavailable")
        exporting_w = -grid
        if exporting_w < policy.exporting_threshold_w:
            rails.append(RailCheck(
                "export", False,
                f"only {exporting_w:.0f} W exported (need ≥ {policy.exporting_threshold_w:.0f} W)"))
            return _release("released — net importing or marginal export")
        rails.append(RailCheck("export", True, f"exporting {exporting_w:.0f} W to grid"))

    # Rail 5: price vs hysteresis band
    if state:
        threshold = policy.exit_above_eur_per_kwh
        engaging = price < threshold
        detail = (f"holding (price {price:+.4f} < release threshold {threshold:+.4f})"
                  if engaging
                  else f"releasing (price {price:+.4f} ≥ release threshold {threshold:+.4f})")
    else:
        threshold = policy.enter_below_eur_per_kwh
        engaging = price < threshold
        detail = (f"engaging (price {price:+.4f} < enter threshold {threshold:+.4f})"
                  if engaging
                  else f"holding off (price {price:+.4f} ≥ enter threshold {threshold:+.4f})")
    rails.append(RailCheck("price-vs-threshold", engaging, detail))
    if not engaging:
        return _release("released — exporting profitably")

    # All rails pass → engage match-load curtailment.
    home = inputs.home_power_w
    max_w = policy.inverter_max_power_w
    if home is None:
        # Fallback: no load info → full curtail (preserves prior behaviour).
        target_pct = 0
        target_w = 0
        match_detail = "no home reading — falling back to full curtail (0%)"
    else:
        raw_target = max(0, min(100, math.ceil(home / max_w * 100)))
        # Deadband: if the new target is within `target_deadband_percent` of the
        # previously applied target while still curtailing, snap back to the
        # previous one. Avoids 1-2% flapping on home-power jitter.
        last = inputs.last_target_percent
        deadband = policy.target_deadband_percent
        if (deadband > 0 and last is not None and last < 100
                and abs(raw_target - last) < deadband):
            target_pct = last
            match_detail = (
                f"match-load: held {target_pct}% (raw {raw_target}% within ±{deadband}% deadband, "
                f"home {home:.0f} W)"
            )
        else:
            target_pct = raw_target
            match_detail = (
                f"match-load: target {target_pct}% = {round(target_pct * max_w / 100)} W "
                f"(home {home:.0f} W of inverter max {max_w} W)"
            )
        target_w = round(target_pct * max_w / 100)
    rails.append(RailCheck("target", True, match_detail))

    summary = (f"limited to {target_pct}% — matching home load "
               f"(no export, no import)" if target_pct < 100
               else "running at 100% — no effective limit (home > inverter max)")
    return Decision(
        curtail=target_pct < 100,
        target_percent=target_pct,
        target_watts=target_w,
        rails=tuple(rails),
        summary=summary,
    )
