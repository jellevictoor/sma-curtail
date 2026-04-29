from datetime import datetime, timezone

from sma.curtailment import Decision, RailCheck
from sma.web.state import History, Sample


def _decision(curtail: bool) -> Decision:
    rails = (
        RailCheck("price", True, "fake price"),
        RailCheck("pv", True, "fake pv"),
    )
    return Decision(
        curtail=curtail,
        target_percent=0 if curtail else 100,
        target_watts=0 if curtail else 4000,
        rails=rails, summary="testing",
    )


def _sample(curtail: bool, price: float, pv: float, grid: float) -> Sample:
    return Sample(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        curtail=curtail,
        target_percent=0 if curtail else 100,
        target_watts=0 if curtail else 4000,
        injection_price_eur_per_kwh=price,
        consumption_price_eur_per_kwh=0.13,
        pv_power_w=pv,
        grid_power_w=grid,
        home_power_w=300.0,
        any_loadpoint_charging=False,
        summary="testing",
    )


def test_history_ring_buffer_caps_at_max():
    h = History(max_samples=3)
    for i in range(5):
        h.append(_sample(curtail=False, price=0.01 * i, pv=1000 + i, grid=-500 + i))
    samples = h.snapshot()
    assert len(samples) == 3
    # We should have the LAST 3 samples (indexes 2,3,4 — pv 1002,1003,1004)
    assert [int(s.pv_power_w) for s in samples] == [1002, 1003, 1004]


def test_app_imports_and_routes_present():
    # Smoke test: importing the FastAPI app shouldn't require any live infra.
    # Don't actually start the lifespan (it would try to reach real services).
    from sma.web.server import app
    paths = {route.path for route in app.routes}
    for needed in ("/", "/api/state", "/api/history", "/static"):
        assert any(p == needed or p.startswith(needed) for p in paths), f"missing {needed}"


def test_sample_now_round_trip():
    d = _decision(curtail=True)
    s = Sample.now(decision=d, injection_price=-0.05, consumption_price=0.13,
                   pv_w=3700, grid_w=-3400, home_w=300, charging=False)
    assert s.curtail is True
    assert s.summary == "testing"
    assert s.injection_price_eur_per_kwh == -0.05
    assert s.consumption_price_eur_per_kwh == 0.13
    assert s.timestamp.endswith("+00:00")
