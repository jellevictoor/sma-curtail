# sma-curtail

Curtails an SMA Sunny Boy SB4.0-1AV-40 inverter when exporting solar costs more
than it earns under the Belgian Ecopower dynamic tariff.

## Why

Ecopower 2026 formulas (per kWh, EPEX in €/MWh):

- `injection   = 0.00098 × EPEX − 0.015`
- `consumption = 0.00102 × EPEX + 0.131` (Fluvius West, residential tier 1)

Break-even points:

- injection flips negative below **EPEX +€15/MWh**
- consumption flips negative below **EPEX −€129/MWh**

Belgian wholesale prices crash routinely during sunny midday hours. Without
curtailment you pay `|injection|` per kWh on every kWh you export during those
hours. Backtest (Sep 2025 – Apr 2026, 7 months):

| Regime | Hours | Share |
|---|---|---|
| Profitable (`injection ≥ 0`) | 19,325 | 92.3% |
| Grey zone (`injection < 0`, `consumption ≥ 0`) | 1,581 | 7.6% |
| Paid-import (`consumption < 0`) | 28 | 0.13% |

Grey zone exports cost €8.72 over 7 months at the injection price; the daemon's
real value is recovering home self-consumption during sunny grey-zone hours
(~€40/year vs binary on/off) plus the 28 hrs/year of paid-import (~€30/year).

## The three regimes

`decide()` in `sma.curtailment` is one decision tree:

| Regime | Inverter | Why |
|---|---|---|
| `injection ≥ 0` | **100%** | every exported kWh earns money |
| `injection < 0` & `consumption ≥ 0` | **match-load** (target % tracks home power) | self-consume to dodge both export loss and import cost |
| `consumption < 0` | **0%** | every imported kWh pays you — even the EV's |

The paid-import rail **overrides the loadpoint rail**: when consumption is
negative we curtail even if evcc is charging the EV, so the EV's draw comes
from grid (paid) instead of from solar (lost paid-import).

## Architecture (hexagonal)

- `sma.curtailment` — pure decision logic, no I/O, fully tested
- `sma.ecopower` — pure tariff math, no I/O
- `sma.evcc` — MCP-over-HTTP client (PV, grid, home, loadpoints)
- `sma.adapters.influx_price` / `influx_metering` / `influx_writer`
- `sma.adapters.modbus_actuator` — writes register 40015
- `sma.adapters.mqtt_publisher` — Home Assistant auto-discovery
- `sma.adapters.solar_forecast` — forecast.solar
- `sma.web` — FastAPI dashboard (port 8980), tick loop, activity log

Domain (`curtailment`, `ecopower`) never imports adapters. Composition root in
`sma.web.server`.

## Decisions and the reasoning behind them

**Match-load over binary 0% in grey zone.** Binary captures €8.72/7mo of avoided
export loss; match-load adds ~€40/year on top by self-consuming home load
instead of importing during sunny grey-zone hours. We kept match-load because
the complexity is localised in `decide()`, and the deadband + EMA also serve as
inverter-write hygiene.

**Release to 100% when evcc loadpoint is engaged.** Catch-22 fix: curtailing to
match-load hides surplus from evcc, so the EV never starts. Releasing exposes
the surplus, evcc's 60s soak window engages, charging begins.

**5-minute soak window after a connect-or-active loadpoint signal.** Handles
"EV plugged but full" — without it the daemon would permanently release once
the car connects, leaking solar through negative injection. Once the window
expires, falls back to match-load.

**Consumption-price rail short-circuits the loadpoint rail.** When
`consumption < 0`, releasing for evcc is wrong — we want the EV's draw to come
from the grid (paid), not from solar (lost paid-import). New rail bypasses
everything else and forces 0%.

**EMA on `home_power_w` (alpha=0.15).** Filters washer pulses, dishwasher
heater cycles, oven thermostats. Half-life ~65s, ~5min to fully track a step
change. Cost of the lag during grey zone: ~½ cent per appliance cycle.

**Target deadband (3%).** Stops the inverter setpoint flapping 1-2% on
home-power jitter.

**Inverter and evcc both optional at startup.** Daemon must boot when either
is unreachable — auto-reconnect each tick. The Modbus TCP server has hung in
the field; the daemon must survive that.

**Modbus heartbeat every 300s.** SMA's grid-management watchdog fires at 600s
of inactivity and reverts to 100%. We rewrite the current target every 5
minutes regardless of change.

**Register 40015 is RAM-backed.** Per SMA engineer Falko Schmidt:
grid-management params don't write to flash. Safe to update arbitrarily often.

## Things considered and dismissed

- **"Briefly release to sample whether evcc would charge"** — killed by the 60s
  evcc soak time. We continuously release while a loadpoint is connected.
- **"Read uncurtailed PV from the inverter"** — SB4.0 operates off-MPP when
  curtailed, so the DC reading reflects the limit, not the potential.
  forecast.solar is the only honest source. Plumbed as
  `estimated_uncurtailed_pv_w`, not yet used.
- **"Tighter coupling between sma and evcc"** — they already cooperate cleanly
  via the loadpoint signal. evcc owns load-side decisions, sma owns
  inverter-side. Don't blur the boundary.

## Hardware

- SMA Sunny Boy SB4.0-1AV-40, 4 kVA single-phase
- Modbus TCP at 192.168.1.3:502, unit ID 3
- Register 40015 (Active power limit, FIX0 percent, 0–100)
- Required: Sunny Portal → "External setting active power" mode
- Active power ramp gradient: 20%/sec (faster than our tick — not the bottleneck)

## Run

```
uv run python -m sma.web         # dev
docker compose up -d             # local docker
uv run pytest                    # 48 tests
uv run python scripts/backtest_grey_zone.py   # regime distribution + recovered € estimate
```

Config in `.env` (see `.env.example`).

## Deploy to a Raspberry Pi 5

Build + push happens in **GitHub Actions** (`.github/workflows/build.yml`):

- runs `pytest` on every PR + push to main
- on push to main / tags, cross-builds `linux/arm64` and publishes to
  `ghcr.io/jellevictoor/sma-curtail` with tags `latest`, `main`, and the
  short SHA (plus semver tags if you push a `v*` tag).

Make the package public once after the first build (GitHub → repo
→ Packages → sma-curtail → Package settings → Change visibility → Public).
Otherwise the RPi needs a token to pull.

On the RPi, after copying `.env` and `docker-compose.yml`:

```bash
# pull + run from the registry — no local build, no source code on the RPi
SMA_IMAGE=ghcr.io/jellevictoor/sma-curtail:latest docker compose pull
SMA_IMAGE=ghcr.io/jellevictoor/sma-curtail:latest docker compose up -d
```

To pin a specific build instead of the rolling `latest`, use the short-SHA
tag (e.g. `ghcr.io/jellevictoor/sma-curtail:299f402`).

## Security

**Dependency pinning posture.** Upper bounds in `pyproject.toml` are deliberate
— they're the firewall against silent supply-chain hops to brand-new releases.
The lockfile (`uv.lock`) is the actual source of truth and includes content
hashes for every package. Both Dockerfile and CI use `uv sync --frozen`, so a
build will fail rather than silently drift.

Re-audit before bumping anything:

```bash
uv export --no-dev --frozen --format requirements-txt \
  | uv tool run --from pip-audit pip-audit --disable-pip -r /dev/stdin
```

Last clean audit: 2026-04-29 (no known CVEs in prod or dev closure).

**Image hygiene.** Pinned `python:3.13.5-slim` and `ghcr.io/astral-sh/uv:0.5.31`
— don't float to `:3.13` or `:0.5`. The container runs as a non-root `app`
user (UID 1000). `.dockerignore` keeps `.env`, `.venv`, `.git`, tests and local
state out of the build context.

**Secrets.** `.env` is gitignored. `.env.example` ships placeholders;
`INFLUX_TOKEN` is the only required secret. MQTT credentials are optional.
Never commit `.env`.
