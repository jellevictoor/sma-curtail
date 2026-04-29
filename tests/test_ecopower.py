import pytest

from sma.ecopower import (
    FluviusRegion,
    break_even_epex_eur_mwh,
    breakdown,
    consumption_price_eur_kwh,
    injection_price_eur_kwh,
)


def test_injection_at_zero_epex():
    # Pure fixed deduction when EPEX is zero.
    assert injection_price_eur_kwh(0.0) == pytest.approx(-0.015)


def test_injection_at_negative_epex():
    # EPEX = -50 €/MWh: 0.00098 × -50 - 0.015 = -0.064
    assert injection_price_eur_kwh(-50.0) == pytest.approx(-0.064, abs=1e-4)


def test_injection_at_high_epex():
    # EPEX = 100 €/MWh: 0.00098 × 100 - 0.015 = 0.083
    assert injection_price_eur_kwh(100.0) == pytest.approx(0.083, abs=1e-4)


def test_break_even_epex_is_around_15_eur_per_mwh():
    bep = break_even_epex_eur_mwh()
    # 0.015 / 0.00098 = 15.306...
    assert bep == pytest.approx(15.306, abs=0.01)
    # Sanity: at break-even EPEX, injection price is zero.
    assert injection_price_eur_kwh(bep) == pytest.approx(0.0, abs=1e-9)


def test_consumption_at_zero_epex_west():
    # Sum of all per-kWh add-ons for Fluvius West:
    #   0.004 + 0.011 + 0.00392 + 0.0631937 + 0.0019261 + 0.04748 ≈ 0.13152
    price = consumption_price_eur_kwh(0.0, FluviusRegion.WEST)
    assert price == pytest.approx(0.13152, abs=1e-4)


def test_consumption_always_strictly_positive_at_realistic_epex():
    # Even at deeply negative EPEX, consumption stays positive (consumption coef is small).
    # At -100 €/MWh: 0.00102 × -100 = -0.102, plus ~0.13 fixed → still positive.
    for epex in (-100.0, 0.0, 50.0, 200.0):
        price = consumption_price_eur_kwh(epex, FluviusRegion.WEST)
        assert price > 0


def test_breakdown_struct():
    b = breakdown(50.0, FluviusRegion.WEST)
    assert b.epex_eur_mwh == 50.0
    assert b.injection_eur_kwh == pytest.approx(0.034, abs=1e-4)
    assert b.consumption_eur_kwh == pytest.approx(0.18252, abs=1e-4)


def test_consumption_varies_by_region():
    cheap = consumption_price_eur_kwh(0.0, FluviusRegion.MIDDEN_VLAANDEREN)
    expensive = consumption_price_eur_kwh(0.0, FluviusRegion.WEST)
    assert cheap < expensive  # Midden-Vlaanderen has the lowest Afnametarief
