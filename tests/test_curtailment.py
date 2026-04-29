import pytest

from sma.curtailment import CurtailmentInputs, CurtailmentPolicy, decide


def inp(
    price=-0.05, pv=4000.0, grid=-3500.0, charging=False,
    home=300.0, cons=0.13, est_pv=None,
) -> CurtailmentInputs:
    return CurtailmentInputs(
        injection_price_eur_per_kwh=price,
        pv_power_w=pv,
        grid_power_w=grid,
        any_loadpoint_charging=charging,
        home_power_w=home,
        consumption_price_eur_per_kwh=cons,
        estimated_uncurtailed_pv_w=est_pv,
    )


def _curtail(state, inputs, policy=None) -> bool:
    return decide(state, inputs, policy or CurtailmentPolicy()).curtail


def _target(state, inputs, policy=None) -> int:
    return decide(state, inputs, policy or CurtailmentPolicy()).target_percent


# Fail-safe behaviour --------------------------------------------------------

def test_no_price_means_release():
    assert _curtail(False, inp(price=None)) is False
    assert _target (False, inp(price=None)) == 100


def test_no_pv_means_release():
    assert _curtail(False, inp(pv=50.0)) is False
    assert _target (False, inp(pv=None)) == 100


def test_loadpoint_charging_releases():
    assert _curtail(False, inp(charging=True)) is False
    assert _curtail(True,  inp(charging=True)) is False


# Export check (only on transitions INTO curtailment) ------------------------

def test_must_be_exporting_to_engage():
    assert _curtail(False, inp(grid=+500.0)) is False


def test_can_stay_engaged_when_grid_reads_zero():
    # Already curtailed → grid reads ~0 because we're capped. Don't bail just on that.
    assert _curtail(True, inp(grid=0.0)) is True


def test_unknown_grid_blocks_entry_but_not_continuation():
    assert _curtail(False, inp(grid=None)) is False
    assert _curtail(True,  inp(grid=None)) is True


# Negative consumption price (paid to import) -------------------------------

def test_negative_consumption_price_forces_full_curtail():
    # EPEX -550 €/MWh: injection -0.554, consumption -0.43. Even though we'd
    # match-load otherwise, importing pays more than self-consuming.
    d = decide(False, inp(price=-0.554, cons=-0.43, home=500), CurtailmentPolicy())
    assert d.curtail is True
    assert d.target_percent == 0


def test_negative_consumption_overrides_loadpoint_release():
    # EV is charging but consumption is negative → curtail anyway, so EV's
    # draw comes from grid (paid) rather than solar (lost paid-import).
    d = decide(False, inp(price=-0.55, cons=-0.4, charging=True), CurtailmentPolicy())
    assert d.curtail is True
    assert d.target_percent == 0


def test_zero_consumption_price_does_not_trigger():
    # Boundary: exactly 0 means equivalent NPV → existing match-load applies.
    d = decide(False, inp(price=-0.05, cons=0.0, home=500, pv=3000, grid=-2500), CurtailmentPolicy())
    assert d.target_percent == 13


def test_unknown_consumption_price_does_not_trigger():
    d = decide(False, inp(price=-0.05, cons=None, home=500, pv=3000, grid=-2500), CurtailmentPolicy())
    assert d.target_percent == 13


# Hysteresis (price rail) ---------------------------------------------------

def test_enters_below_negative_threshold():
    assert _curtail(False, inp(price=-0.01)) is True


def test_does_not_enter_inside_hysteresis_band():
    assert _curtail(False, inp(price=0.0))   is False
    assert _curtail(False, inp(price=0.003)) is False


def test_holds_curtailment_inside_hysteresis_band():
    assert _curtail(True, inp(price=0.0))   is True
    assert _curtail(True, inp(price=0.003)) is True


def test_releases_above_exit_threshold():
    assert _curtail(True, inp(price=0.01)) is False


def test_policy_rejects_inverted_band():
    with pytest.raises(ValueError):
        CurtailmentPolicy(enter_below_eur_per_kwh=0.01, exit_above_eur_per_kwh=-0.01)


# Match-load target ---------------------------------------------------------

def test_target_matches_home_load():
    # PV 3000, home 500, max 4000 → ceil(500/4000*100) = 13%
    d = decide(False, inp(pv=3000, home=500, grid=-2500), CurtailmentPolicy())
    assert d.curtail is True
    assert d.target_percent == 13
    assert d.target_watts == 520  # 13% × 4000


def test_target_clamps_to_full_when_home_exceeds_max():
    # Home > inverter max → 100% (no effective limit, produce flat-out)
    d = decide(False, inp(pv=3000, home=5000, grid=-100), CurtailmentPolicy())
    # At home=5000, exporting=100W is below the 200W threshold → release
    assert d.curtail is False
    assert d.target_percent == 100


def test_target_zero_when_home_unknown_falls_back_to_full_curtail():
    d = decide(False, inp(home=None), CurtailmentPolicy())
    assert d.curtail is True
    assert d.target_percent == 0   # safe fallback


def test_target_value_uses_configured_inverter_max():
    # Bigger inverter (10 kW): 500W home → ceil(500/10000*100) = 5%
    policy = CurtailmentPolicy(inverter_max_power_w=10000)
    d = decide(False, inp(pv=3000, home=500, grid=-2500), policy)
    assert d.target_percent == 5
    assert d.target_watts == 500


def test_deadband_holds_target_through_small_jitter():
    # Already at 13% (520 W). Home jumps from 500 → 460 W (raw_target = 12%).
    # With deadband=3, change of 1% is inside the band → stay at 13%.
    policy = CurtailmentPolicy(target_deadband_percent=3)
    inputs = CurtailmentInputs(
        injection_price_eur_per_kwh=-0.05, pv_power_w=3000, grid_power_w=-2500,
        any_loadpoint_charging=False, home_power_w=460, consumption_price_eur_per_kwh=0.13,
        last_target_percent=13,
    )
    d = decide(True, inputs, policy)
    assert d.target_percent == 13   # held by deadband


def test_deadband_releases_when_change_exceeds_band():
    # Same setup, but home jumps to 200 W (raw=5%). Δ=8 > deadband=3 → update.
    policy = CurtailmentPolicy(target_deadband_percent=3)
    inputs = CurtailmentInputs(
        injection_price_eur_per_kwh=-0.05, pv_power_w=3000, grid_power_w=-2500,
        any_loadpoint_charging=False, home_power_w=200, consumption_price_eur_per_kwh=0.13,
        last_target_percent=13,
    )
    d = decide(True, inputs, policy)
    assert d.target_percent == 5


# Decision payload ----------------------------------------------------------

def test_decision_carries_rails_for_ui():
    d = decide(False, inp(price=-0.05, pv=4000, grid=-3500, charging=False, home=200), CurtailmentPolicy())
    assert d.curtail is True
    rail_names = [r.name for r in d.rails]
    assert rail_names == ["price", "pv", "consumption-price", "loadpoint", "export", "price-vs-threshold", "target"]
    assert all(r.ok for r in d.rails)


def test_decision_summary_when_blocked_by_loadpoint():
    d = decide(False, inp(charging=True), CurtailmentPolicy())
    assert d.curtail is False
    assert "loadpoint" in d.summary.lower()
    blocking = next(r for r in d.rails if not r.ok)
    assert blocking.name == "loadpoint"
