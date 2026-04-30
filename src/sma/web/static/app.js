// Polls /api/state, /api/history, /api/prices, /api/power_history.
// Theme-agnostic: chart colours come from CSS classes (see <style> in index.html).

const fmt = (v, dec = 0, suffix = "") =>
  v === null || v === undefined ? "—" : `${Number(v).toFixed(dec)}${suffix}`;
const sign = (v, dec = 4) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(dec)}`;

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

// --- theme toggle -----------------------------------------------------------

function initTheme() {
  const root = document.documentElement;
  const btn = document.getElementById("theme-toggle");
  const icon = document.getElementById("theme-icon");
  const label = document.getElementById("theme-label");
  const sync = () => {
    const dark = root.classList.contains("dark");
    icon.textContent = dark ? "☀" : "🌙";
    label.textContent = dark ? "light" : "dark";
  };
  sync();
  btn.addEventListener("click", () => {
    const dark = !root.classList.contains("dark");
    root.classList.toggle("dark", dark);
    localStorage.setItem("theme", dark ? "dark" : "light");
    sync();
  });
}

// Shared time domain across both charts.
let sharedTimeDomain = null;
let pastGrid = [];
let solarForecast = [];

const CHART_MARGIN = { top: 14, right: 16, bottom: 22, left: 64 };

// --- state panel + banner ---------------------------------------------------

function renderState(state) {
  const sample = state.sample;
  const rails = state.rails || [];
  const policy = state.policy || {};

  const banner = document.getElementById("banner");
  const dot = document.getElementById("status-dot");
  const label = document.getElementById("status-label");
  const summary = document.getElementById("status-summary");
  const pct = document.getElementById("status-pct");

  const baseBanner = "panel rounded px-4 py-3 flex items-center gap-3 border-l-4 text-sm";
  const evcc = document.getElementById("status-evcc");
  if (!sample) {
    banner.className = `${baseBanner} border-l-muted`;
    label.textContent = "connecting…";
    summary.textContent = "waiting for first tick";
    dot.style.background = "var(--text-muted)";
    pct.textContent = "—%";
    pct.className = "num text-muted-s";
  } else if (sample.curtail) {
    banner.className = `${baseBanner} border-l-curtail`;
    label.textContent = sample.target_percent === 0 ? "CURTAILED" : "LIMITED";
    const watts = sample.target_watts != null ? ` (≤${sample.target_watts} W)` : "";
    summary.textContent = sample.summary;
    dot.style.background = "var(--accent-curtail)";
    dot.classList.add("animate-pulse");
    pct.textContent = `${sample.target_percent}%${watts}`;
    pct.className = "num text-curtail font-semibold";
  } else {
    banner.className = `${baseBanner} border-l-produce`;
    label.textContent = "PRODUCING";
    summary.textContent = sample.summary;
    dot.style.background = "var(--accent-produce)";
    dot.classList.remove("animate-pulse");
    pct.textContent = "100%";
    pct.className = "num text-produce font-semibold";
  }

  if (sample) {
    document.getElementById("m-price").textContent = sign(sample.injection_price_eur_per_kwh, 4);
    document.getElementById("updated").textContent = new Date(sample.timestamp).toLocaleTimeString();
    evcc.textContent = sample.any_loadpoint_charging
      ? "EV/heat-pump charging — surplus absorbed"
      : "";
    renderEnergyFlow(sample);
  }

  const ul = document.getElementById("rails");
  ul.innerHTML = "";
  rails.forEach(r => {
    const li = document.createElement("li");
    li.className = "flex items-baseline gap-3 py-1.5";
    li.innerHTML = `
      <span class="${r.ok ? "text-produce" : "text-curtail"} font-bold w-4 inline-block">${r.ok ? "✓" : "✗"}</span>
      <span class="text-pri w-44">${r.name}</span>
      <span class="${r.ok ? "text-sec" : "text-curtail"} flex-1">${r.detail}</span>`;
    ul.appendChild(li);
  });

  const hb = (state.config || {}).modbus_heartbeat_seconds;
  document.getElementById("policy").textContent =
    `policy enter<${fmt(policy.enter_below_eur_per_kwh, 4)} exit>${fmt(policy.exit_above_eur_per_kwh, 4)} ` +
    `pv≥${fmt(policy.pv_active_threshold_w, 0)}W export≥${fmt(policy.exporting_threshold_w, 0)}W ` +
    `· inverter 192.168.1.3:502 unit 3 · register 40015 (FIX0 %)` +
    (hb ? ` · heartbeat ${hb}s · writes only on change or heartbeat` : "");

  const cfg = state.config || {};
  if (cfg.tick_seconds) document.getElementById("header-tick").textContent = `${cfg.tick_seconds}s tick`;
  document.getElementById("inverter-status").style.display = cfg.inverter_connected === false ? "" : "none";
  document.getElementById("evcc-status").style.display     = cfg.evcc_connected     === false ? "" : "none";

  // Money tile (since daemon start, seeded from Influx since midnight).
  // Use higher precision when values are small so the user sees them changing.
  const m = state.money_today || {};
  const eur = (v) => {
    if (v == null) return "—";
    const a = Math.abs(Number(v));
    const dp = a >= 1 ? 2 : a >= 0.1 ? 3 : 4;
    return `${v >= 0 ? "+" : ""}${Number(v).toFixed(dp)} €`;
  };
  // Money-flow perspective: signed from the user's POV.
  //   negative = out of your pocket, positive = into your pocket.
  //   line totals = sum of their parts (no sign-flips between headline and detail).
  // Curtailment effect: saved (avoided cost) is +, lost (forced import) is −.
  document.getElementById("m-saved").textContent = eur(m.saved_eur);
  document.getElementById("m-lost").textContent  = eur(-Math.abs(m.lost_eur || 0));
  document.getElementById("m-net").textContent   = eur(m.net_eur);
  // Grid: cost (import) is −, earn (export revenue) is +. Server's bill_eur is
  // cost − revenue (positive = paid grid); flip it so the column is sign-honest.
  document.getElementById("m-cost").textContent = eur(-Math.abs(m.cost_eur || 0));
  document.getElementById("m-earn").textContent = eur(m.revenue_eur);
  document.getElementById("m-bill").textContent = eur(-Number(m.bill_eur || 0));
}

// --- energy bar (evcc-style) ----------------------------------------------

const FLOW_MIN_W = 30;   // below this → treated as idle / collapsed

function renderEnergyFlow(sample) {
  const pv   = sample.pv_power_w   ?? 0;
  const home = sample.home_power_w ?? 0;
  const grid = sample.grid_power_w ?? 0;  // positive = importing, negative = exporting

  const gridIn  = grid > 0 ? grid : 0;
  const gridOut = grid < 0 ? -grid : 0;

  // Three direction-pair flows. Exactly one of (pv→grid) and (grid→home) is non-zero.
  const pv2home   = Math.max(0, Math.min(pv, home));   // self-consumption (green)
  const pv2grid   = Math.max(0, pv - home);            // export          (amber)
  const grid2home = Math.max(0, home - pv);            // import          (sky)

  const total = Math.max(pv2home + pv2grid + grid2home,
                         pv + gridIn,
                         home + gridOut, 1);

  setSegment("seg-self",   pv2home,   "seg-self-w",   total);
  setSegment("seg-export", pv2grid,   "seg-export-w", total);
  setSegment("seg-import", grid2home, "seg-import-w", total);

  setCell(".src-pv",   pv,     "src-pv-w",       total);
  setCell(".src-grid", gridIn, "src-grid-in-w",  total);
  setCell(".snk-home", home,    "snk-home-w",      total);
  setCell(".snk-grid", gridOut, "snk-grid-out-w",  total);
}

function setSegment(elemId, watts, labelId, total) {
  const el = document.getElementById(elemId);
  const label = document.getElementById(labelId);
  if (watts >= FLOW_MIN_W) {
    el.classList.remove("idle");
    el.style.flexGrow = (watts / total).toString();
    label.textContent = `${Math.round(watts).toLocaleString()} W`;
  } else {
    el.classList.add("idle");
    el.style.flexGrow = "0";
    label.textContent = "";
  }
}

function setCell(selector, watts, watsId, total) {
  const el = document.querySelector(selector);
  if (watts >= FLOW_MIN_W) {
    el.classList.remove("idle");
    el.style.flexGrow = (watts / total).toString();
    document.getElementById(watsId).textContent = `${Math.round(watts).toLocaleString()} W`;
  } else {
    el.classList.add("idle");
    el.style.flexGrow = "0";
  }
}

// --- power chart (live actuals + 24h Influx backfill) ----------------------

function renderPowerChart(samples) {
  document.getElementById("sample-count").textContent = samples.length;

  const live = samples.map(s => ({
    t: new Date(s.timestamp), pv: s.pv_power_w, grid: s.grid_power_w,
    curtail: s.curtail, source: "live",
  }));
  const earliestLive = live.length ? live[0].t.getTime() : Infinity;
  const past = pastGrid
    .filter(p => new Date(p.timestamp).getTime() < earliestLive)
    .map(p => ({
      t: new Date(p.timestamp), pv: null, grid: p.grid_power_w,
      curtail: false, source: "past",
    }));
  const data = [...past, ...live];
  if (!data.length) return;

  const svg = d3.select("#chart-power");
  svg.selectAll("*").remove();
  const W = svg.node().getBoundingClientRect().width;
  const H = 240;
  const m = CHART_MARGIN;
  const innerW = W - m.left - m.right;
  const innerH = H - m.top - m.bottom;

  const x = d3.scaleTime()
    .domain(sharedTimeDomain || d3.extent(data, d => d.t))
    .range([0, innerW]);
  const yPower = d3.scaleLinear().domain(d3.extent([
    ...data.map(d => d.pv).filter(v => v != null),
    ...data.map(d => d.grid).filter(v => v != null),
    0,
  ])).nice().range([innerH, 0]);

  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);

  g.append("g").attr("class", "grid")
    .call(d3.axisLeft(yPower).tickSize(-innerW).tickFormat(""))
    .selectAll("text").remove();

  const now = new Date();
  if (sharedTimeDomain && sharedTimeDomain[1] > now) {
    g.append("rect").attr("class", "future-region")
      .attr("x", x(now)).attr("y", 0)
      .attr("width", Math.max(0, innerW - x(now))).attr("height", innerH);
  }
  if (sharedTimeDomain && now >= sharedTimeDomain[0] && now <= sharedTimeDomain[1]) {
    g.append("line").attr("class", "now-line")
      .attr("x1", x(now)).attr("x2", x(now))
      .attr("y1", 0).attr("y2", innerH);
  }

  const bands = contiguousBands(data, d => d.curtail);
  g.selectAll("rect.curtail-band").data(bands).join("rect")
    .attr("class", "curtail-band")
    .attr("x", d => x(d.start)).attr("y", 0)
    .attr("width", d => Math.max(2, x(d.end) - x(d.start)))
    .attr("height", innerH);

  g.append("line").attr("class", "zero-line")
    .attr("x1", 0).attr("x2", innerW)
    .attr("y1", yPower(0)).attr("y2", yPower(0));

  const line = (acc) => d3.line()
    .defined(d => acc(d) != null)
    .x(d => x(d.t)).y(d => yPower(acc(d)))
    .curve(d3.curveMonotoneX);
  g.append("path").datum(data).attr("class", "line-pv")   .attr("d", line(d => d.pv));
  g.append("path").datum(data).attr("class", "line-grid") .attr("d", line(d => d.grid));

  // Solar forecast (future side, dashed PV-coloured line)
  const fcData = (solarForecast || [])
    .map(p => ({ t: new Date(p.timestamp), pv: p.pv_power_w }))
    .filter(p => sharedTimeDomain ? (p.t >= sharedTimeDomain[0] && p.t <= sharedTimeDomain[1]) : true);
  if (fcData.length) {
    const fcLine = d3.line()
      .defined(d => d.pv != null)
      .x(d => x(d.t)).y(d => yPower(d.pv))
      .curve(d3.curveMonotoneX);
    g.append("path").datum(fcData)
      .attr("class", "line-pv")
      .attr("stroke-dasharray", "4,3")
      .attr("opacity", 0.7)
      .attr("d", fcLine);
  }

  g.append("g").attr("class", "axis").attr("transform", `translate(0,${innerH})`)
    .call(d3.axisBottom(x).ticks(6));
  g.append("g").attr("class", "axis")
    .call(d3.axisLeft(yPower).tickFormat(d => `${d}W`));

  attachTooltip({
    svg, gInner: g, innerW, innerH, x, data,
    formatRow: (d) => {
      const dir = d.grid == null ? "—"
                : d.grid < 0 ? `<span class="num">exporting ${(-d.grid).toFixed(0)} W</span>`
                : `<span class="num">importing ${d.grid.toFixed(0)} W</span>`;
      const status = d.curtail
        ? `<span class="text-curtail">curtailed (0%)</span>`
        : `<span class="text-produce">producing (100%)</span>`;
      const src = d.source === "past" ? `<span class="text-muted-s">(meter)</span>` : "";
      return `
        <div class="text-muted-s num">${d.t.toLocaleString()} ${src}</div>
        <div style="margin-top:4px">PV    <span class="num" style="color:var(--accent-pv)">${d.pv == null ? "—" : d.pv.toFixed(0) + " W"}</span></div>
        <div>grid  <span class="num" style="color:var(--accent-grid)">${d.grid == null ? "—" : d.grid.toFixed(0) + " W"}</span></div>
        <div class="text-muted-s" style="font-size:10px">${dir}</div>
        <div style="margin-top:4px">${status}</div>`;
    },
  });
}

// --- price chart (forecast) -------------------------------------------------

function renderPriceChart(prices) {
  const points = prices.points || [];
  const now = new Date(prices.now);
  const breakEvenEpex = prices.break_even_epex_eur_mwh;
  const enterBelow = prices.thresholds.enter_below_eur_per_kwh;

  const current = points.find(p => {
    const t = new Date(p.timestamp).getTime();
    return t <= now.getTime() && t + 15 * 60 * 1000 > now.getTime();
  });
  document.getElementById("p-epex").textContent        = sign(current ? current.epex_eur_mwh / 1000 : null, 4);
  document.getElementById("p-consumption").textContent = sign(current?.consumption_eur_kwh, 4);
  document.getElementById("p-breakeven").textContent   = sign(breakEvenEpex / 1000, 4);

  if (current) {
    const epexKwh = current.epex_eur_mwh / 1000;
    const step = 0.98 * epexKwh;
    const inj = step - 0.015;
    const cons = current.consumption_eur_kwh;
    const consStep = 1.02 * epexKwh;
    const f = (v) => sign(v, 4);
    document.getElementById("pm-epex").textContent   = `${f(epexKwh)} €/kWh`;
    document.getElementById("pm-epex2").textContent  = f(epexKwh);
    document.getElementById("pm-step").textContent   = f(step);
    const result = document.getElementById("pm-result");
    result.textContent = `${f(inj)} €/kWh`;
    result.className = `font-semibold ${inj < 0 ? "text-curtail" : "text-produce"}`;
    const verdict = document.getElementById("pm-verdict");
    if (inj < 0) {
      verdict.innerHTML = `<span class="text-curtail font-semibold">unprofitable to export</span> — daemon will curtail when other rails allow.`;
    } else {
      verdict.innerHTML = `<span class="text-produce font-semibold">profitable to export</span> — every kWh exported earns ${f(inj)} €.`;
    }

    // Live consumption substitution
    document.getElementById("pm-c-epex").textContent  = `${f(epexKwh)} €/kWh`;
    document.getElementById("pm-c-epex2").textContent = f(epexKwh);
    document.getElementById("pm-c-step").textContent  = f(consStep);
    document.getElementById("pm-c-result").textContent = `${f(cons)} €/kWh`;
    const spread = cons - inj;  // the asymmetry that powers the economic rail
    document.getElementById("pm-c-spread").innerHTML =
      `<span class="text-pri">spread = ${f(spread)} €/kWh</span> — every kWh of self-consumption ` +
      `beats exporting by this much (or saves you this much vs curtailing & importing).`;
  } else {
    ["pm-epex", "pm-epex2", "pm-step", "pm-result",
     "pm-c-epex", "pm-c-epex2", "pm-c-step", "pm-c-result"
    ].forEach(id => document.getElementById(id).textContent = "—");
    document.getElementById("pm-verdict").textContent = "no current price slot in window.";
    document.getElementById("pm-c-spread").textContent = "";
  }

  const futureSlots = points.filter(p => new Date(p.timestamp) > now &&
                                          p.injection_eur_kwh < enterBelow);
  document.getElementById("p-curtail-hours").textContent = `${(futureSlots.length * 0.25).toFixed(2)} h`;

  if (!points.length) return;

  const svg = d3.select("#chart-prices");
  svg.selectAll("*").remove();
  const W = svg.node().getBoundingClientRect().width;
  const H = 280;
  const m = CHART_MARGIN;
  const innerW = W - m.left - m.right;
  const innerH = H - m.top - m.bottom;

  const data = points.map(p => ({
    t: new Date(p.timestamp),
    epex: p.epex_eur_mwh / 1000,
    inj: p.injection_eur_kwh,
    cons: p.consumption_eur_kwh,
  }));

  sharedTimeDomain = d3.extent(data, d => d.t);

  const x = d3.scaleTime().domain(sharedTimeDomain).range([0, innerW]);
  // Single shared axis: injection ≈ EPEX − 0.015, so they belong on the same scale.
  const yInj = d3.scaleLinear()
    .domain(d3.extent([
      ...data.map(d => d.inj),
      ...data.map(d => d.cons),
      ...data.map(d => d.epex),
      enterBelow, 0,
    ])).nice().range([innerH, 0]);

  const g = svg.append("g").attr("transform", `translate(${m.left},${m.top})`);

  const futureStart = data.find(d => d.t > now)?.t || now;
  if (futureStart < data[data.length - 1].t) {
    g.append("rect").attr("class", "future-region")
      .attr("x", x(futureStart)).attr("y", 0)
      .attr("width", innerW - x(futureStart)).attr("height", innerH);
  }

  // Horizontal zone (price axis): everything below enter threshold loses money to export.
  g.append("rect").attr("class", "curtail-zone")
    .attr("x", 0).attr("y", yInj(enterBelow))
    .attr("width", innerW)
    .attr("height", Math.max(0, innerH - yInj(enterBelow)));

  // Vertical bands (time axis): forecast windows where we'll curtail.
  const willCurtail = contiguousBands(data, d => d.inj < enterBelow);
  g.selectAll("rect.curtail-fc").data(willCurtail).join("rect")
    .attr("class", "curtail-fc")
    .attr("x", d => x(d.start)).attr("y", 0)
    .attr("width", d => Math.max(2, x(d.end) - x(d.start)))
    .attr("height", innerH);

  g.append("line").attr("class", "threshold-enter")
    .attr("x1", 0).attr("x2", innerW)
    .attr("y1", yInj(enterBelow)).attr("y2", yInj(enterBelow));
  {
    const lblG = g.append("g").attr("transform", `translate(6, ${yInj(enterBelow) - 4})`);
    const txt = lblG.append("text").attr("class", "threshold-label")
      .text(`enter < ${enterBelow.toFixed(4)} €/kWh`);
    const bb = txt.node().getBBox();
    lblG.insert("rect", "text")
      .attr("class", "threshold-label-bg")
      .attr("x", bb.x - 3).attr("y", bb.y - 1)
      .attr("width", bb.width + 6).attr("height", bb.height + 2)
      .attr("rx", 2);
  }

  if (now >= data[0].t && now <= data[data.length - 1].t) {
    g.append("line").attr("class", "now-line")
      .attr("x1", x(now)).attr("x2", x(now))
      .attr("y1", 0).attr("y2", innerH);
    g.append("text").attr("class", "now-label")
      .attr("x", x(now)).attr("y", -3)
      .attr("text-anchor", "middle").text("now");
  }

  const injLine  = d3.line().defined(d => d.inj  != null).x(d => x(d.t)).y(d => yInj(d.inj)).curve(d3.curveMonotoneX);
  const consLine = d3.line().defined(d => d.cons != null).x(d => x(d.t)).y(d => yInj(d.cons)).curve(d3.curveMonotoneX);
  const epexLine = d3.line().defined(d => d.epex != null).x(d => x(d.t)).y(d => yInj(d.epex)).curve(d3.curveMonotoneX);

  g.append("path").datum(data).attr("class", "line-epex")       .attr("d", epexLine);
  g.append("path").datum(data).attr("class", "line-consumption").attr("d", consLine);
  g.append("path").datum(data).attr("class", "line-injection")  .attr("d", injLine);

  g.append("g").attr("class", "axis").attr("transform", `translate(0,${innerH})`)
    .call(d3.axisBottom(x).ticks(8));
  g.append("g").attr("class", "axis")
    .call(d3.axisLeft(yInj).tickFormat(d => d.toFixed(3)));

  attachTooltip({
    svg, gInner: g, innerW, innerH, x, data,
    formatRow: (d) => {
      const isFuture = d.t > now;
      const willCurtail = d.inj < enterBelow;
      const verdict = willCurtail
        ? `<span class="text-curtail">curtail — exporting loses money</span>`
        : `<span class="text-produce">profitable to export</span>`;
      return `
        <div class="text-muted-s num">${d.t.toLocaleString()}${isFuture ? " <span class='text-muted-s'>(forecast)</span>" : ""}</div>
        <div style="margin-top:4px">EPEX        <span class="num" style="color:var(--accent-epex)">${sign(d.epex, 4)} €/kWh</span></div>
        <div>injection   <span class="num" style="color:var(--accent-price)">${sign(d.inj, 4)} €/kWh</span></div>
        <div>consumption <span class="num" style="color:var(--accent-consumption)">${sign(d.cons, 4)} €/kWh</span></div>
        <div style="margin-top:4px">${verdict}</div>`;
    },
  });
}

// --- tooltip (shared, theme-aware via CSS vars) ----------------------------

function _ensureTooltip() {
  let tip = document.getElementById("tooltip");
  if (tip) return tip;
  tip = document.createElement("div");
  tip.id = "tooltip";
  tip.style.cssText =
    "position:absolute;pointer-events:none;display:none;z-index:50;" +
    "border:1px solid var(--border);background:var(--bg-panel);color:var(--text-primary);" +
    "padding:8px 10px;border-radius:6px;font-size:11px;line-height:1.5;" +
    "box-shadow:0 4px 12px rgba(0,0,0,0.25);min-width:180px;";
  document.body.appendChild(tip);
  return tip;
}

function attachTooltip({ svg, gInner, innerW, innerH, x, data, formatRow }) {
  if (!data.length) return;
  const tip = _ensureTooltip();
  const bisect = d3.bisector(d => d.t).left;

  const guide = gInner.append("line")
    .attr("y1", 0).attr("y2", innerH)
    .attr("class", "now-line")
    .attr("opacity", 0).attr("pointer-events", "none");

  const overlay = gInner.append("rect")
    .attr("x", 0).attr("y", 0).attr("width", innerW).attr("height", innerH)
    .attr("fill", "transparent").style("cursor", "crosshair");

  overlay.on("mouseenter", () => { guide.attr("opacity", 0.4); tip.style.display = "block"; });
  overlay.on("mouseleave", () => { guide.attr("opacity", 0); tip.style.display = "none"; });
  overlay.on("mousemove", (event) => {
    const [mx] = d3.pointer(event, gInner.node());
    const t = x.invert(mx);
    const i = bisect(data, t);
    const a = data[i - 1], b = data[i];
    const d = !a ? b : !b ? a : (Math.abs(a.t - t) < Math.abs(b.t - t) ? a : b);
    if (!d) return;
    guide.attr("x1", x(d.t)).attr("x2", x(d.t));
    tip.innerHTML = formatRow(d);
    const tipW = tip.offsetWidth || 200;
    const wantLeft = event.pageX + 14 + tipW < window.innerWidth ? event.pageX + 14 : event.pageX - 14 - tipW;
    tip.style.left = wantLeft + "px";
    tip.style.top  = (event.pageY + 14) + "px";
  });
}

function contiguousBands(data, pred) {
  const bands = [];
  let start = null;
  for (let i = 0; i < data.length; i++) {
    if (pred(data[i]) && start === null) start = data[i].t;
    if ((!pred(data[i]) || i === data.length - 1) && start !== null) {
      bands.push({ start, end: data[i].t });
      start = null;
    }
  }
  return bands;
}

// --- polling loop -----------------------------------------------------------

async function refreshState()    { try { renderState(await fetchJSON("/api/state")); } catch (e) { console.warn(e); } }
async function refreshHistory()  { try { renderPowerChart((await fetchJSON("/api/history")).samples); } catch (e) { console.warn(e); } }
async function refreshLog()      { try { renderLog((await fetchJSON("/api/log")).entries); } catch (e) { console.warn(e); } }

function renderLog(entries) {
  const ul = document.getElementById("log");
  if (!ul) return;
  ul.innerHTML = "";
  // Newest first.
  for (const e of [...entries].reverse()) {
    const li = document.createElement("li");
    const t = new Date(e.timestamp).toLocaleTimeString();
    const lvlColor =
      e.level === "ERROR"   ? "text-curtail" :
      e.level === "WARNING" ? "text-price"   :
      "text-muted-s";
    li.className = "flex items-baseline gap-2 num";
    li.innerHTML = `
      <span class="text-muted-s shrink-0 w-16">${t}</span>
      <span class="${lvlColor} shrink-0 w-12">${e.level}</span>
      <span class="text-muted-s shrink-0 w-28">${e.logger}</span>
      <span class="text-pri flex-1">${escapeHTML(e.message)}</span>`;
    ul.appendChild(li);
  }
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function refreshPastGrid() {
  try { pastGrid = (await fetchJSON("/api/power_history")).points || []; refreshHistory(); }
  catch (e) { console.warn(e); }
}
async function refreshForecast() {
  try { solarForecast = (await fetchJSON("/api/solar_forecast")).points || []; refreshHistory(); }
  catch (e) { console.warn(e); }
}
async function refreshPrices()   {
  try { renderPriceChart(await fetchJSON("/api/prices")); refreshHistory(); }
  catch (e) { console.warn(e); }
}

initTheme();
refreshState(); refreshHistory(); refreshPrices(); refreshPastGrid(); refreshForecast(); refreshLog();
setInterval(refreshState, 5000);
setInterval(refreshHistory, 15000);
setInterval(refreshLog, 5000);
setInterval(refreshPrices, 5 * 60 * 1000);
setInterval(refreshPastGrid, 5 * 60 * 1000);
setInterval(refreshForecast, 30 * 60 * 1000);
window.addEventListener("resize", () => { refreshHistory(); refreshPrices(); });
