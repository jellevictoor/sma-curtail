"""FastAPI app: runs the tick loop in a background asyncio task and serves a
small HTML/D3 dashboard so you can see (and explain) the daemon's decisions.

Run with:
    uv run sma-web
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from influxdb_client import InfluxDBClient
from starlette.requests import Request

from sma.adapters.influx_metering import GridPoint, InfluxMeteringProvider
from sma.adapters.influx_price import InfluxPriceProvider, PricePoint
from sma.adapters.influx_writer import InfluxSampleWriter
from sma.adapters.modbus_actuator import ModbusActuator
from sma.adapters.mqtt_publisher import MQTTPublisher
from sma.adapters.solar_forecast import ForecastSolarProvider, SolarForecastPoint
from sma.client import SMAModbusClient
from sma.config import Config
from sma.curtailment import CurtailmentInputs, CurtailmentPolicy, Decision, decide
from sma.ecopower import FluviusRegion, break_even_epex_eur_mwh
from sma.evcc import EvccMCPClient, EvccSnapshot
from sma.web.state import (
    AppLogHandler,
    History,
    LogBuffer,
    LogEntry,
    Sample,
    decision_to_rails,
    history_to_payload,
)

log = logging.getLogger("sma.web")

PRICE_CACHE_S = 300.0
GRID_CACHE_S = 300.0
FORECAST_CACHE_S = 1800.0  # forecast.solar free tier limits us to 12 calls/h
LOADPOINT_GRACE_S = 300.0  # how long after a loadpoint signal we keep curtailment released
LOADPOINT_ACTIVE_W = 100   # threshold above which a loadpoint is "actually drawing"

_HERE = Path(__file__).parent


# --- composition ------------------------------------------------------------

@dataclass
class Providers:
    """Bundle of live infrastructure adapters used by the tick loop and HTTP
    handlers. Created once per `_run_with_providers()` lifecycle, torn down
    together. The inverter and actuator may be None when the Modbus connection
    is unreachable — the daemon keeps running (read-only) and reconnects each
    tick.
    """
    influx: InfluxDBClient
    prices: InfluxPriceProvider
    metering: InfluxMeteringProvider
    writer: InfluxSampleWriter
    evcc: EvccMCPClient | None
    inverter: SMAModbusClient | None
    actuator: ModbusActuator | None
    mqtt: MQTTPublisher | None = None
    solar: ForecastSolarProvider | None = None

    def close(self) -> None:
        closers = [self.writer.close, self.influx.close]
        if self.evcc is not None:
            closers.append(lambda: self.evcc.__exit__(None, None, None))
        if self.inverter is not None:
            closers.append(lambda: self.inverter.__exit__(None, None, None))
        if self.mqtt is not None:
            closers.append(self.mqtt.close)
        for closer in closers:
            try: closer()
            except Exception: pass  # noqa: BLE001, S110

    def try_connect_inverter(self, config: Config) -> None:
        """Re-attempt the Modbus connection."""
        try:
            inv = SMAModbusClient(
                config.inverter_host, config.inverter_port, config.inverter_unit_id,
                transport=config.inverter_transport,
            ).__enter__()
            self.inverter = inv
            self.actuator = ModbusActuator(inv)
            log.info("inverter reconnected at %s:%s (%s)",
                     config.inverter_host, config.inverter_port, config.inverter_transport)
        except Exception as exc:  # noqa: BLE001
            log.debug("inverter still unreachable: %s", exc)

    def try_connect_evcc(self, config: Config) -> None:
        """Re-attempt the evcc MCP connection."""
        try:
            self.evcc = EvccMCPClient(config.evcc_mcp_url).__enter__()
            log.info("evcc reconnected at %s", config.evcc_mcp_url)
        except Exception as exc:  # noqa: BLE001
            log.debug("evcc still unreachable: %s", exc)


def _build_providers(config: Config) -> Providers:
    influx = InfluxDBClient(url=config.influx_url, token=config.influx_token, org=config.influx_org)
    prices = InfluxPriceProvider(influx, config.influx_org, config.influx_bucket)
    metering = InfluxMeteringProvider(influx, config.influx_org, config.influx_metering_bucket)
    writer = InfluxSampleWriter(influx, config.influx_org, config.influx_metering_bucket)

    # evcc — optional. If unreachable at boot, daemon still starts; we retry every tick.
    evcc: EvccMCPClient | None = None
    try:
        evcc = EvccMCPClient(config.evcc_mcp_url).__enter__()
        log.info("evcc connected at %s", config.evcc_mcp_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("evcc unreachable (%s) — running without evcc data; will retry each tick", exc)

    # Inverter is optional — if Modbus isn't reachable, the daemon still runs
    # in observe-only mode and retries every tick.
    inv: SMAModbusClient | None = None
    actuator: ModbusActuator | None = None
    try:
        inv = SMAModbusClient(
            config.inverter_host, config.inverter_port, config.inverter_unit_id,
            transport=config.inverter_transport,
        ).__enter__()
        actuator = ModbusActuator(inv)
        log.info("inverter connected at %s:%s (%s)",
                 config.inverter_host, config.inverter_port, config.inverter_transport)
    except Exception as exc:  # noqa: BLE001
        log.warning("inverter unreachable (%s) — running observe-only; will retry each tick", exc)

    mqtt: MQTTPublisher | None = None
    if config.mqtt_host:
        try:
            mqtt = MQTTPublisher(
                host=config.mqtt_host, port=config.mqtt_port,
                username=config.mqtt_username, password=config.mqtt_password,
                discovery_prefix=config.mqtt_discovery_prefix,
            )
            log.info("mqtt connected to %s:%s (discovery %s)",
                     config.mqtt_host, config.mqtt_port, config.mqtt_discovery_prefix)
        except Exception as exc:  # noqa: BLE001
            log.warning("mqtt connect failed (%s) — running without HA discovery", exc)
            mqtt = None

    solar: ForecastSolarProvider | None = None
    if config.solar_lat is not None and config.solar_lon is not None:
        solar = ForecastSolarProvider(
            lat=config.solar_lat, lon=config.solar_lon,
            dec=config.solar_dec, az=config.solar_az, kwp=config.solar_kwp,
        )

    return Providers(influx, prices, metering, writer, evcc, inv, actuator,
                     mqtt=mqtt, solar=solar)


# --- shared mutable state --------------------------------------------------

@dataclass
class _Cache:
    """Tiny TTL cache for expensive Influx queries."""
    value: object = None
    fetched_at: float = 0.0
    ttl_s: float = 300.0

    def stale(self) -> bool:
        return (time.monotonic() - self.fetched_at) > self.ttl_s

    def store(self, value: object) -> None:
        self.value = value
        self.fetched_at = time.monotonic()


@dataclass
class AppState:
    config: Config
    policy: CurtailmentPolicy
    region: FluviusRegion
    history: History = field(default_factory=History)
    log_buffer: LogBuffer = field(default_factory=LogBuffer)
    last_written_percent: int | None = None
    last_write_monotonic: float = 0.0

    # Mutable per-tick state
    curtailed: bool = False
    last_decision: Decision | None = None
    last_sample: Sample | None = None
    last_uncurtailed_pv_w: float | None = None
    home_power_w_ema: float | None = None     # smoothed home consumption for match-load decision
    # Loadpoint engagement window: tracks "evcc has a chance to absorb surplus".
    # Refreshed when a loadpoint just connected, OR is currently drawing >100 W.
    # Stays "engaged" for LOADPOINT_GRACE_S afterward; once expired the daemon
    # falls back to match-load curtailment (no point exposing surplus to a full EV).
    last_loadpoint_signal_monotonic: float = 0.0
    last_loadpoint_connected: bool = False
    money_today: dict = field(default_factory=lambda: {"saved_eur": 0.0, "lost_eur": 0.0, "net_eur": 0.0, "samples": 0})

    # Provider handle (set when tick loop is connected)
    providers: Providers | None = None

    # Caches for HTTP endpoints
    _prices_cache:   _Cache = field(default_factory=lambda: _Cache(value=[], ttl_s=PRICE_CACHE_S))
    _grid_cache:     _Cache = field(default_factory=lambda: _Cache(value=[], ttl_s=GRID_CACHE_S))
    _forecast_cache: _Cache = field(default_factory=lambda: _Cache(value=[], ttl_s=FORECAST_CACHE_S))
    _mu: Lock = field(default_factory=Lock)

    @staticmethod
    def from_config(config: Config) -> AppState:
        return AppState(
            config=config,
            policy=CurtailmentPolicy(
                enter_below_eur_per_kwh=config.enter_below_eur_kwh,
                exit_above_eur_per_kwh=config.exit_above_eur_kwh,
                inverter_max_power_w=config.inverter_max_power_w,
                target_deadband_percent=config.target_deadband_percent,
            ),
            region=FluviusRegion[config.fluvius_region],
        )

    def commit(self, sample: Sample, decision: Decision, new_state: bool) -> None:
        with self._mu:
            self.curtailed = new_state
            self.last_decision = decision
            self.last_sample = sample
            self.history.append(sample)
            self.money_today = _money_from_history(self.history.snapshot())

    def policy_dict(self) -> dict:
        return {
            "enter_below_eur_per_kwh": self.policy.enter_below_eur_per_kwh,
            "exit_above_eur_per_kwh":  self.policy.exit_above_eur_per_kwh,
            "pv_active_threshold_w":   self.policy.pv_active_threshold_w,
            "exporting_threshold_w":   self.policy.exporting_threshold_w,
        }

    def cached_prices(self) -> list[PricePoint]:
        if self._prices_cache.stale() and self.providers is not None:
            try:
                self._prices_cache.store(self.providers.prices.time_series(
                    hours_ago=24, hours_ahead=30, region=self.region,
                ))
            except Exception as exc:  # noqa: BLE001
                log.warning("price time_series fetch failed: %s", exc)
        return list(self._prices_cache.value or [])

    def cached_grid_history(self) -> list[GridPoint]:
        if self._grid_cache.stale() and self.providers is not None:
            try:
                self._grid_cache.store(self.providers.metering.grid_history(hours_ago=24, every="5m"))
            except Exception as exc:  # noqa: BLE001
                log.warning("grid history fetch failed: %s", exc)
        return list(self._grid_cache.value or [])

    def cached_solar_forecast(self) -> list[SolarForecastPoint]:
        if self._forecast_cache.stale() and self.providers is not None and self.providers.solar is not None:
            try:
                self._forecast_cache.store(self.providers.solar.fetch())
            except Exception as exc:  # noqa: BLE001
                log.warning("solar forecast fetch failed: %s", exc)
        return list(self._forecast_cache.value or [])


# --- the tick --------------------------------------------------------------

def run_one_tick(s: AppState) -> None:
    p = s.providers
    if p is None:
        return
    if p.actuator is None:
        p.try_connect_inverter(s.config)
    if p.evcc is None:
        p.try_connect_evcc(s.config)

    price = _safe(p.prices.current_injection_price_eur_kwh, "price")
    consumption = _safe(lambda: p.prices.current_consumption_price_eur_kwh(s.region), "consumption price")
    snap: EvccSnapshot | None = _safe(p.evcc.snapshot, "evcc") if p.evcc is not None else None

    pv_now = snap.pv_power_w if snap else None
    if not s.curtailed and pv_now is not None and pv_now > s.policy.pv_active_threshold_w:
        s.last_uncurtailed_pv_w = pv_now

    # Fall back to p1meter via Influx when evcc has no grid meter wired.
    grid_now = snap.grid_power_w if snap else None
    if grid_now is None:
        grid_now = _safe(p.metering.current_grid_power_w, "grid (p1meter fallback)")

    # EMA-smooth home_power_w to avoid match-load oscillation on transients
    # (washing-machine startup, EV inrush, heat-pump cycling). SMA itself does
    # no smoothing on the active-power setpoint — this is the recommended
    # input-side smoothing pattern from the evcc/openWB community.
    raw_home = snap.home_power_w if snap else None
    if raw_home is not None:
        a = s.config.home_power_ema_alpha
        s.home_power_w_ema = (
            raw_home if s.home_power_w_ema is None
            else a * raw_home + (1 - a) * s.home_power_w_ema
        )
    home_for_decision = s.home_power_w_ema

    # Loadpoint "engaged" with soak-window logic: refresh the timer on a fresh
    # connect-transition or when the loadpoint is actually drawing power, and
    # consider the loadpoint engaged for LOADPOINT_GRACE_S afterward. Once that
    # window expires (e.g. EV is full and connected but won't absorb more), we
    # fall back to match-load curtailment instead of permanently releasing.
    now_mono = time.monotonic()
    lp_connected = snap.any_loadpoint_connected if snap else False
    lp_active_w  = snap.active_loadpoint_charge_power_w if snap else 0.0
    just_connected   = lp_connected and not s.last_loadpoint_connected
    actively_drawing = lp_active_w > LOADPOINT_ACTIVE_W
    if just_connected:
        s.last_loadpoint_signal_monotonic = now_mono
        log.info("loadpoint connected — releasing for %ds soak window", int(LOADPOINT_GRACE_S))
    elif actively_drawing:
        s.last_loadpoint_signal_monotonic = now_mono
    s.last_loadpoint_connected = lp_connected
    loadpoint_engaged = (now_mono - s.last_loadpoint_signal_monotonic) < LOADPOINT_GRACE_S

    inputs = CurtailmentInputs(
        injection_price_eur_per_kwh=price,
        pv_power_w=pv_now,
        grid_power_w=grid_now,
        any_loadpoint_charging=loadpoint_engaged,
        home_power_w=home_for_decision,
        consumption_price_eur_per_kwh=consumption,
        estimated_uncurtailed_pv_w=s.last_uncurtailed_pv_w,
        # Pass last applied target so the deadband can hold steady through small home-w jitter.
        last_target_percent=s.last_written_percent,
    )
    prev_target = s.last_decision.target_percent if s.last_decision is not None else None
    decision = decide(s.curtailed, inputs, s.policy)

    # Write only when the target actually changes, or to refresh the inverter
    # watchdog at the configured heartbeat interval. Massively reduces wear.
    write_ok = True
    if p.actuator is not None:
        target_changed = decision.target_percent != s.last_written_percent
        heartbeat_due = (time.monotonic() - s.last_write_monotonic) >= s.config.modbus_heartbeat_seconds
        if target_changed or heartbeat_due:
            write_ok = _safe_call(p.actuator.set_percent, "modbus write", decision.target_percent)
            if write_ok:
                reason = "change" if target_changed else "heartbeat"
                log.info("wrote %d%% to inverter (%s)", decision.target_percent, reason)
                s.last_written_percent = decision.target_percent
                s.last_write_monotonic = time.monotonic()
            else:
                try: p.inverter and p.inverter.__exit__(None, None, None)
                except Exception: pass  # noqa: BLE001, S110
                p.inverter = None
                p.actuator = None
    else:
        write_ok = False  # no inverter → can't actually curtail
    new_state = decision.curtail if write_ok else s.curtailed

    # Sanity-check: only meaningful when we actually wrote something. PV well
    # above target with no recent successful write is just "we can't write".
    wrote_recently = (
        s.last_written_percent is not None
        and (time.monotonic() - s.last_write_monotonic) < 120
    )
    if (decision.curtail and wrote_recently and p.actuator is not None
            and pv_now is not None and pv_now > decision.target_watts + 200):
        log.warning("inverter PV %.0f W > target %d W (+200 tol) — limit not (yet) honoured",
                    pv_now, decision.target_watts)

    sample = Sample.now(
        decision=decision,
        injection_price=price,
        consumption_price=consumption,
        pv_w=inputs.pv_power_w,
        grid_w=inputs.grid_power_w,
        home_w=snap.home_power_w if snap else None,
        charging=inputs.any_loadpoint_charging,
    )
    s.commit(sample, decision, new_state)

    _persist(p.writer, sample)
    _publish_mqtt(s, p, sample, price, consumption)

    if decision.target_percent != prev_target:
        log.info("decision: %d%% (≤%d W) — %s",
                 decision.target_percent, decision.target_watts, decision.summary)


def _publish_mqtt(s: AppState, p: Providers, sample: Sample,
                  price: float | None, consumption: float | None) -> None:
    if p.mqtt is None:
        return
    # EPEX in €/kWh: derived from injection (= 0.98 × EPEX − 0.015) → EPEX = (inj+0.015)/0.98
    epex_eur_kwh = ((price + 0.015) / 0.98) if price is not None else None
    forecast_hours = _forecast_curtail_hours(s)
    try:
        p.mqtt.publish_state(
            curtail=sample.curtail, summary=sample.summary,
            injection_price=price, consumption_price=consumption,
            epex_eur_kwh=epex_eur_kwh,
            pv_w=sample.pv_power_w, grid_w=sample.grid_power_w, home_w=sample.home_power_w,
            curtail_hours_ahead=forecast_hours,
            money_today_net_eur=s.money_today.get("net_eur"),
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("mqtt publish failed: %s", exc)


def _forecast_curtail_hours(s: AppState) -> float | None:
    """Hours of forecasted negative-injection slots ahead, from cached prices."""
    points = s._prices_cache.value or []
    if not points:
        return None
    now = datetime.now(timezone.utc)
    enter = s.policy.enter_below_eur_per_kwh
    future_below = sum(1 for p in points
                       if p.timestamp > now and p.injection_eur_kwh < enter)
    return round(future_below * 0.25, 2)


def _persist(writer: InfluxSampleWriter, sample: Sample) -> None:
    try:
        writer.write_sample(
            curtail=sample.curtail,
            target_percent=sample.target_percent,
            injection_price=sample.injection_price_eur_per_kwh,
            consumption_price=sample.consumption_price_eur_per_kwh,
            pv_w=sample.pv_power_w,
            grid_w=sample.grid_power_w,
            home_w=sample.home_power_w,
            charging=sample.any_loadpoint_charging,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("influx writeback failed: %s", exc)


def _safe(call, label):
    try: return call()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s read failed: %s", label, exc)
        return None


def _safe_call(call, label, *args) -> bool:
    try:
        call(*args); return True
    except Exception as exc:  # noqa: BLE001
        log.error("%s failed: %s", label, exc)
        return False


# --- money calculation -----------------------------------------------------

def _money_from_history(samples: list[Sample]) -> dict:
    """Two perspectives on today's € impact, both over the in-memory window.

    ── household bill ──
    cost_eur     : sum over slots where grid > 0 of grid_kW × hours × consumption_price
                   (what the grid charged you for imports)
    revenue_eur  : sum over slots where grid < 0 of |grid_kW| × hours × injection_price
                   (what the grid paid you for exports — negative when injection price is negative)
    bill_eur     : cost − revenue. Positive = you paid the grid, negative = the grid paid you.

    ── curtailment effect ──
    saved_eur    : while curtailed and injection<0, exporting would have cost
                   estimated_export × |injection| → that's what the daemon "saved".
    lost_eur     : while curtailed, all home consumption is imported at consumption_price
                   (the "price" of curtailment vs. self-consuming).
    net_eur      : saved − lost.
    """
    if not samples:
        return {"saved_eur": 0.0, "lost_eur": 0.0, "net_eur": 0.0,
                "cost_eur": 0.0, "revenue_eur": 0.0, "bill_eur": 0.0,
                "samples": 0}
    saved = lost = 0.0
    cost = revenue = 0.0
    last_uncurt_pv: float | None = None
    for i, sample in enumerate(samples):
        if not sample.curtail and (sample.pv_power_w or 0) > 100:
            last_uncurt_pv = sample.pv_power_w
        if i == 0:
            continue
        hours = (datetime.fromisoformat(sample.timestamp) -
                 datetime.fromisoformat(samples[i - 1].timestamp)).total_seconds() / 3600.0
        if hours <= 0 or hours > 0.25:
            continue

        inj  = sample.injection_price_eur_per_kwh
        cons = sample.consumption_price_eur_per_kwh or 0.0
        home = sample.home_power_w or 0.0
        grid = sample.grid_power_w  # positive = import, negative = export

        # household bill
        if grid is not None:
            if grid > 0:
                cost += grid / 1000.0 * hours * cons
            elif grid < 0 and inj is not None:
                revenue += (-grid) / 1000.0 * hours * inj   # inj can be negative

        # curtailment effect
        if sample.curtail and inj is not None:
            export_w = max(0.0, (last_uncurt_pv or 0.0) - home)
            if inj < 0:
                saved += export_w * abs(inj) / 1000.0 * hours
            lost += home * cons / 1000.0 * hours

    return {
        "saved_eur":   round(saved, 4),
        "lost_eur":    round(lost, 4),
        "net_eur":     round(saved - lost, 4),
        "cost_eur":    round(cost, 4),
        "revenue_eur": round(revenue, 4),
        "bill_eur":    round(cost - revenue, 4),
        "samples":     len(samples),
    }


# --- background tick loop --------------------------------------------------

async def tick_loop(s: AppState) -> None:
    log.info("tick loop starting (every %ss)", s.config.tick_seconds)
    while True:
        try:
            await _run_until_failure(s)
        except asyncio.CancelledError:
            log.info("tick loop cancelled"); raise
        except Exception:  # noqa: BLE001
            log.exception("tick loop dropped — reconnecting in %ss", s.config.tick_seconds)
        await asyncio.sleep(s.config.tick_seconds)


async def _run_until_failure(s: AppState) -> None:
    s.providers = await asyncio.to_thread(_build_providers, s.config)
    # Seed the ring buffer with today's persisted samples so money_today
    # survives restarts (we've been writing them every tick to Influx).
    await asyncio.to_thread(_seed_history, s)
    try:
        while True:
            await asyncio.to_thread(run_one_tick, s)
            await asyncio.sleep(s.config.tick_seconds)
    finally:
        providers, s.providers = s.providers, None
        if providers is not None:
            await asyncio.to_thread(providers.close)


def _seed_history(s: AppState) -> None:
    """Hydrate History from Influx with today's persisted samples."""
    if s.providers is None: return
    try:
        rows = s.providers.metering.todays_curtail_samples()
    except Exception as exc:  # noqa: BLE001
        log.warning("history seed failed: %s", exc)
        return
    seeded = 0
    for r in rows:
        try:
            tp = int(r["target_percent"])
            s.history.append(Sample(
                timestamp=r["timestamp"],
                curtail=bool(r["curtail"]),
                target_percent=tp,
                target_watts=round(tp * s.policy.inverter_max_power_w / 100),
                injection_price_eur_per_kwh=r["injection_price_eur_per_kwh"],
                consumption_price_eur_per_kwh=r["consumption_price_eur_per_kwh"],
                pv_power_w=r["pv_power_w"],
                grid_power_w=r["grid_power_w"],
                home_power_w=r["home_power_w"],
                any_loadpoint_charging=bool(r["any_loadpoint_charging"]),
                summary="(seeded)",
            ))
            seeded += 1
        except Exception:  # noqa: BLE001
            continue
    s.money_today = _money_from_history(s.history.snapshot())
    log.info("seeded %d samples from Influx; money_today=%s", seeded, s.money_today)


# --- FastAPI ---------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    config = Config.from_env()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    state = AppState.from_config(config)
    app.state.app_state = state
    # Attach an in-memory handler so the UI can show what's happening.
    handler = AppLogHandler(state.log_buffer)
    handler.setLevel(logging.INFO)
    for name in ("sma.web", "sma.mqtt", "sma.solar_forecast"):
        logging.getLogger(name).addHandler(handler)
    task = asyncio.create_task(tick_loop(state))
    try:
        yield
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass


app = FastAPI(title="sma-curtail", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_HERE / "static"), name="static")
templates = Jinja2Templates(directory=_HERE / "templates")


def _state(request: Request) -> AppState:
    return request.app.state.app_state


@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/api/state")
async def api_state(request: Request):
    s = _state(request)
    inv_connected  = bool(s.providers and s.providers.actuator is not None)
    evcc_connected = bool(s.providers and s.providers.evcc is not None)
    return JSONResponse({
        "sample": asdict(s.last_sample) if s.last_sample else None,
        "rails":  decision_to_rails(s.last_decision) if s.last_decision else [],
        "policy": s.policy_dict(),
        "money_today": s.money_today,
        "config": {
            "tick_seconds": s.config.tick_seconds,
            "modbus_heartbeat_seconds": s.config.modbus_heartbeat_seconds,
            "inverter_connected": inv_connected,
            "evcc_connected": evcc_connected,
        },
    })


@app.get("/api/history")
async def api_history(request: Request):
    return JSONResponse({"samples": history_to_payload(_state(request).history.snapshot())})


@app.get("/api/log")
async def api_log(request: Request):
    entries = _state(request).log_buffer.snapshot()
    return JSONResponse({
        "entries": [asdict(e) for e in entries[-100:]],
    })


@app.get("/api/power_history")
async def api_power_history(request: Request):
    points = await asyncio.to_thread(_state(request).cached_grid_history)
    return JSONResponse({
        "points": [
            {"timestamp": p.timestamp.isoformat(timespec="seconds"),
             "grid_power_w": p.grid_power_w}
            for p in points
        ],
    })


@app.get("/api/solar_forecast")
async def api_solar_forecast(request: Request):
    points = await asyncio.to_thread(_state(request).cached_solar_forecast)
    return JSONResponse({
        "points": [
            {"timestamp": p.timestamp.isoformat(timespec="seconds"),
             "pv_power_w": p.pv_power_w}
            for p in points
        ],
    })


@app.get("/api/prices")
async def api_prices(request: Request):
    s = _state(request)
    points = await asyncio.to_thread(s.cached_prices)
    return JSONResponse({
        "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "break_even_epex_eur_mwh": break_even_epex_eur_mwh(),
        "thresholds": {
            "enter_below_eur_per_kwh": s.policy.enter_below_eur_per_kwh,
            "exit_above_eur_per_kwh":  s.policy.exit_above_eur_per_kwh,
        },
        "points": [
            {"timestamp": p.timestamp.isoformat(timespec="seconds"),
             "epex_eur_mwh": p.epex_eur_mwh,
             "injection_eur_kwh": p.injection_eur_kwh,
             "consumption_eur_kwh": p.consumption_eur_kwh}
            for p in points
        ],
    })


@app.get("/healthz")
async def healthz(request: Request):
    s = _state(request)
    sample = s.last_sample
    if sample is None:
        return JSONResponse({"status": "starting"}, status_code=503)
    age_s = (datetime.now(timezone.utc) - datetime.fromisoformat(sample.timestamp)).total_seconds()
    healthy = age_s < (s.config.tick_seconds * 5)
    return JSONResponse({
        "status": "ok" if healthy else "stale",
        "last_sample_age_s": age_s,
        "tick_seconds": s.config.tick_seconds,
        "curtailed": s.curtailed,
    }, status_code=200 if healthy else 503)


def run() -> None:
    """Entrypoint for `uv run sma-web`."""
    import uvicorn
    uvicorn.run("sma.web.server:app", host="0.0.0.0", port=Config.from_env().web_port, log_level="info")
