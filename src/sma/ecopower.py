"""Ecopower 'Dynamische burgerstroom' tariff calculations.

Pure domain logic. Coefficients sourced from the official tariff card
(202601_dbs_tariefkaart.pdf, valid from 1 January 2026).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FluviusRegion(Enum):
    ANTWERPEN          = "Antwerpen"
    HALLE_VILVOORDE    = "Halle-Vilvoorde"
    IMEWO              = "Imewo"
    KEMPEN             = "Kempen"
    LIMBURG            = "Limburg"
    MIDDEN_VLAANDEREN  = "Midden-Vlaanderen"
    WEST               = "West"
    ZENNE_DIJLE        = "Zenne-Dijle"


# Afnametarief (€/kWh, single/dual rate) — only the consumption-side fee that varies by region.
_AFNAMETARIEF_EUR_PER_KWH: dict[FluviusRegion, float] = {
    FluviusRegion.ANTWERPEN:         0.0505027,
    FluviusRegion.HALLE_VILVOORDE:   0.0531770,
    FluviusRegion.IMEWO:             0.0522864,
    FluviusRegion.KEMPEN:            0.0597838,
    FluviusRegion.LIMBURG:           0.0542695,
    FluviusRegion.MIDDEN_VLAANDEREN: 0.0498061,
    FluviusRegion.WEST:              0.0631937,
    FluviusRegion.ZENNE_DIJLE:       0.0553921,
}

# Ecopower formula coefficients (per quarter-hour, EPEX in €/MWh).
_INJECTION_EPEX_COEFF      = 0.00098   # × EPEX(€/MWh)
_INJECTION_FIXED_EUR_KWH   = -0.015
_CONSUMPTION_EPEX_COEFF    = 0.00102
_CONSUMPTION_FIXED_EUR_KWH = 0.004

# Per-kWh consumption add-ons (€/kWh, residential, tier 1: 0..3,000 kWh).
_GSC_EUR_KWH                  = 0.011
_WKK_EUR_KWH                  = 0.00392
_BIJDRAGE_ENERGIE_EUR_KWH     = 0.0019261
_ACCIJNS_TIER1_EUR_KWH        = 0.04748


@dataclass(frozen=True)
class PriceBreakdown:
    epex_eur_mwh: float
    injection_eur_kwh: float    # what you NET earn (or pay if negative) when exporting
    consumption_eur_kwh: float  # what you pay when importing


def injection_price_eur_kwh(epex_eur_mwh: float) -> float:
    """Net injection revenue (€/kWh) at given EPEX day-ahead price.

    Negative when exporting actually costs money — that is the curtailment trigger.
    """
    return _INJECTION_EPEX_COEFF * epex_eur_mwh + _INJECTION_FIXED_EUR_KWH


def consumption_price_eur_kwh(epex_eur_mwh: float, region: FluviusRegion) -> float:
    """Total cost (€/kWh) when drawing 1 kWh from the grid.

    Includes Ecopower energy, GSC, WKK, regional Afnametarief, energy contribution,
    and excise tax (residential tier 1, 0-3000 kWh).
    """
    return (
        _CONSUMPTION_EPEX_COEFF * epex_eur_mwh
        + _CONSUMPTION_FIXED_EUR_KWH
        + _GSC_EUR_KWH
        + _WKK_EUR_KWH
        + _AFNAMETARIEF_EUR_PER_KWH[region]
        + _BIJDRAGE_ENERGIE_EUR_KWH
        + _ACCIJNS_TIER1_EUR_KWH
    )


def break_even_epex_eur_mwh() -> float:
    """EPEX price at which injection price = 0 €/kWh."""
    return -_INJECTION_FIXED_EUR_KWH / _INJECTION_EPEX_COEFF


def breakdown(epex_eur_mwh: float, region: FluviusRegion) -> PriceBreakdown:
    return PriceBreakdown(
        epex_eur_mwh=epex_eur_mwh,
        injection_eur_kwh=injection_price_eur_kwh(epex_eur_mwh),
        consumption_eur_kwh=consumption_price_eur_kwh(epex_eur_mwh, region),
    )
