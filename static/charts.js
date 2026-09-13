// openGardener charts, organized by bed.
// Structure mirrors the physical setup: each BED has shared ambient charts
// (air temp, humidity, light, pressure) shown once, then its VARIETIES, each
// with only its own moisture and soil temp. Bed A and Bed B are separate
// sections. Kills the old triplication of shared bed data.
//
// Features retained: zoom & pan, threshold/frost lines, watering markers,
// min/max/current labels, per-section crosshair.

const BEDS = [
  { id: "A", label: "Bed A",
    varieties: [
      { id: "sanandreas_a", label: "San Andreas" },
      { id: "sequoia_a",    label: "Sequoia" },
      { id: "albion_a",     label: "Albion" },
    ] },
  { id: "B", label: "Bed B",
    varieties: [
      { id: "sanandreas_b", label: "San Andreas" },
      { id: "sequoia_b",    label: "Sequoia" },
      { id: "albion_b",     label: "Albion" },
    ] },
];

// ambient (per-bed) metrics
const AMBIENT = [
  { key: "air_temp",  title: "Air temp",  unit: "\u00b0F",  color: "#4a3826", pct: false },
  { key: "humidity",  title: "Humidity",  unit: "%",   color: "#3f7d45", pct: true  },
  { key: "light",     title: "Light",     unit: "lux", color: "#e8b04b", pct: false },
  { key: "pressure",  title: "Pressure",  unit: "hPa", color: "#7a8a72", pct: false },
];
// per-variety (own) metrics
const VARIETY_METRICS = [
  { key: "moisture",  title: "Moisture",  unit: "%",   color: "#7fb069", pct: true  },
  { key: "soil_temp", title: "Soil temp", unit: "\u00b0F",  color: "#b1492c", pct: false },
];

const ZONE = "America/Los_Angeles";
let currentRange = "24h";
const charts = {};  // chartKey -> Chart

// crosshair scoped per section (a section id groups charts that share an x)
const crosshair = {
  id: "ogCrosshair",
  afterDraw(chart) {
    const x = chart._ogCrosshairX;
    if (x == null) return;
    const { ctx, chartArea } = chart;
    if (x < chartArea.left || x > chartArea.right) return;
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(x, chartArea.top);
    ctx.lineTo(x, chartArea.bottom);
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(39,67,46,0.35)";
    ctx.setLineDash([3, 3]);
    ctx.stroke();
    ctx.restore();
  },
};
Chart.register(crosshair);

const sectionCharts = {}; // sectionId -> [chartKey,...]
function setCrosshair(sectionId, xPixel) {
  (sectionCharts[sectionId] || []).forEach(k => {
    const ch = charts[k];
    if (ch) { ch._ogCrosshairX = xPixel; ch.draw(); }
  });
}

function annotationsFor(metricKey) {
  const ann = {};
  if (metricKey === "moisture" && window.ogThreshold != null) {
    ann.threshold = {
      type: "line", yMin: window.ogThreshold, yMax: window.ogThreshold,
      borderColor: "#b23b3b", borderWidth: 1.5, borderDash: [6, 4],
      label: { display: true, content: `water below ${window.ogThreshold}%`,
               position: "start", font: { size: 9 }, color: "#b23b3b",
               backgroundColor: "rgba(240,246,234,0.85)" },
    };
  }
  // Watering event markers on BOTH moisture and soil-temp charts. On moisture
  // you see the moisture jump; on soil temp you see the cooling dip as water
  // hits the root zone.
  if ((metricKey === "moisture" || metricKey === "soil_temp")
      && Array.isArray(window.ogWatering)) {
    window.ogWatering.forEach((e, i) => {
      if (!e.ts_start) return;
      ann[`w${i}`] = { type: "line", xMin: e.ts_start, xMax: e.ts_start,
                       borderColor: "rgba(63,125,69,0.5)", borderWidth: 1.5 };
    });
  }
  if (metricKey === "air_temp" && window.ogFrostF != null) {
    ann.frost = {
      type: "line", yMin: window.ogFrostF, yMax: window.ogFrostF,
      borderColor: "#3a6ea5", borderWidth: 1.5, borderDash: [6, 4],
      label: { display: true, content: `frost ${window.ogFrostF}\u00b0F`,
               position: "start", font: { size: 9 }, color: "#3a6ea5",
               backgroundColor: "rgba(240,246,234,0.85)" },
    };
  }
  return ann;
}

function makeChart(canvasId, chartKey, sectionId, metric) {
  const ctx = document.getElementById(canvasId).getContext("2d");
  const yScale = {
    type: "linear",
    grid: { color: "rgba(216,230,205,0.55)" },
    ticks: { font: { size: 9 }, maxTicksLimit: 5 },
  };
  if (metric.pct) { yScale.min = 0; yScale.max = 100; }

  charts[chartKey] = new Chart(ctx, {
    type: "line",
    data: { datasets: [{
      label: metric.title, borderColor: metric.color,
      backgroundColor: metric.color, borderWidth: 1.5, pointRadius: 0,
      tension: 0.25, data: [],
    }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: "index", intersect: false },
      parsing: { xAxisKey: "ts", yAxisKey: "value" },
      onHover: (evt) => setCrosshair(sectionId, evt.x),
      scales: {
        x: { type: "time", time: { tooltipFormat: "MMM d HH:mm" },
             adapters: { date: { zone: ZONE } },
             ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 5,
                      font: { size: 9 } },
             grid: { display: false } },
        y: yScale,
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: (c) => `${c.parsed.y} ${metric.unit}` } },
        annotation: { annotations: annotationsFor(metric.key) },
        zoom: {
          pan: { enabled: true, mode: "x" },
          zoom: { wheel: { enabled: true }, pinch: { enabled: true },
                  drag: { enabled: true, backgroundColor: "rgba(63,125,69,0.12)" },
                  mode: "x",
                  onZoomComplete: () => updateStats(chartKey, metric) },
        },
      },
    },
  });
  sectionCharts[sectionId] = sectionCharts[sectionId] || [];
  sectionCharts[sectionId].push(chartKey);

  const canvas = document.getElementById(canvasId);
  canvas.addEventListener("mouseleave", () => setCrosshair(sectionId, null));
}

function metricCardHTML(canvasId, statId, metric, extra) {
  return `
    <div class="metric-card">
      <div class="metric-card-title">
        <span>${metric.title} <span class="metric-unit">${metric.unit}</span></span>
        <span class="metric-stats" id="${statId}">&mdash;</span>
      </div>
      <div class="metric-canvas-wrap"><canvas id="${canvasId}"></canvas></div>
      ${extra || ""}
    </div>`;
}

function buildLayout() {
  const grid = document.getElementById("chart-grid");

  // 1) one garden-wide ambient section (single air + light source)
  const ambientSection = `
    <div class="bed-section">
      <div class="bed-section-head">
        <span class="bed-section-name">Garden</span>
        <span class="bed-section-tag">shared air &amp; light</span>
        <button class="zoom-reset" data-section="ambient">reset zoom</button>
      </div>
      <div class="ambient-grid">
        ${AMBIENT.map(m => metricCardHTML(
            `c-ambient-${m.key}`, `s-ambient-${m.key}`, m,
            m.key === "pressure"
              ? `<div class="pressure-tendency" id="tend-ambient">&mdash;</div>`
              : "")).join("")}
      </div>
    </div>`;

  // 2) each bed shows only its varieties' own soil charts
  const bedSections = BEDS.map(bed => `
    <div class="bed-section">
      <div class="bed-section-head">
        <span class="bed-section-name">${bed.label}</span>
        <span class="bed-section-tag">soil by variety</span>
        <button class="zoom-reset" data-section="bed-${bed.id}">reset zoom</button>
      </div>
      <div class="variety-grid">
        ${bed.varieties.map(v => `
          <div class="variety-card">
            <div class="variety-card-head">${v.label}</div>
            ${VARIETY_METRICS.map(m => metricCardHTML(
                `c-${v.id}-${m.key}`, `s-${v.id}-${m.key}`, m)).join("")}
          </div>`).join("")}
      </div>
    </div>`).join("");

  grid.innerHTML = ambientSection + bedSections;

  document.querySelectorAll(".zoom-reset").forEach(btn => {
    btn.addEventListener("click", () => {
      (sectionCharts[btn.dataset.section] || []).forEach(k => {
        if (charts[k]) charts[k].resetZoom();
      });
    });
  });
}

function updateStats(chartKey, metric) {
  const ch = charts[chartKey];
  const el = document.getElementById("s-" + chartKey);
  if (!ch || !el) return;
  const data = ch.data.datasets[0].data;
  if (!data.length) { el.textContent = "\u2014"; return; }
  const xScale = ch.scales.x, lo = xScale.min, hi = xScale.max;
  const vis = data.filter(pt => {
    const t = new Date(pt.ts).getTime();
    return t >= lo && t <= hi;
  });
  const use = vis.length ? vis : data;
  const vals = use.map(p => p.value).filter(v => v != null);
  if (!vals.length) { el.textContent = "\u2014"; return; }
  const min = Math.min(...vals), max = Math.max(...vals);
  const cur = vals[vals.length - 1];
  el.innerHTML = `<b>${cur}${metric.unit}</b> &middot; lo ${min} &middot; hi ${max}`;
}

async function loadAmbient() {
  try {
    const r = await fetch(`/api/ambient/${currentRange}`, { cache: "no-store" });
    if (!r.ok) return;
    const d = await r.json();
    // pressure tendency badge on the pressure card
    const tEl = document.getElementById("tend-ambient");
    if (tEl) {
      const t = d.pressure_tendency;
      if (t && t.change_3h != null) {
        const arrow = t.arrow === "down" ? "\u2198"
                    : t.arrow === "up" ? "\u2197" : "\u2192";
        const sign3 = t.change_3h > 0 ? "+" : "";
        const ctx24 = t.change_24h != null
          ? ` &middot; 24h ${t.change_24h > 0 ? "+" : ""}${t.change_24h}` : "";
        const cls = t.arrow === "down" && t.change_3h <= -3 ? "warn"
                  : t.arrow === "up" ? "good" : "";
        tEl.innerHTML = `<span class="tend-arrow ${cls}">${arrow}</span> `
          + `<b>${t.words}</b> &middot; 3h ${sign3}${t.change_3h} hPa${ctx24}`;
      } else {
        tEl.innerHTML = `<span class="tend-arrow">\u2192</span> building history\u2026`;
      }
    }
    for (const m of AMBIENT) {
      const key = `ambient-${m.key}`;
      const ch = charts[key];
      if (!ch) continue;
      const raw = d.series[m.key] || [];
      ch.data.datasets[0].data = raw.map(pt => ({
        ts: pt.ts,
        value: (m.pct && pt.value !== null)
          ? Math.max(0, Math.min(100, pt.value)) : pt.value,
      }));
      ch.options.plugins.annotation.annotations = annotationsFor(m.key);
      ch.update("none");
      updateStats(key, m);
    }
  } catch (e) { /* keep as-is */ }
}

async function loadVariety(v) {
  try {
    const r = await fetch(`/api/variety/${v.id}/${currentRange}`, { cache: "no-store" });
    if (!r.ok) return;
    const d = await r.json();
    for (const m of VARIETY_METRICS) {
      const key = `${v.id}-${m.key}`;
      const ch = charts[key];
      if (!ch) continue;
      const raw = d.series[m.key] || [];
      ch.data.datasets[0].data = raw.map(pt => ({
        ts: pt.ts,
        value: (m.pct && pt.value !== null)
          ? Math.max(0, Math.min(100, pt.value)) : pt.value,
      }));
      ch.options.plugins.annotation.annotations = annotationsFor(m.key);
      ch.update("none");
      updateStats(key, m);
    }
  } catch (e) { /* keep as-is */ }
}

function loadAll() {
  let i = 0;
  setTimeout(() => loadAmbient(), i++ * 150);
  for (const bed of BEDS) {
    for (const v of bed.varieties) setTimeout(() => loadVariety(v), i++ * 150);
  }
}

function initCharts() {
  buildLayout();
  // garden-wide ambient charts
  for (const m of AMBIENT) makeChart(`c-ambient-${m.key}`,
                                     `ambient-${m.key}`, "ambient", m);
  // per-variety soil charts, grouped by bed
  for (const bed of BEDS) {
    const sid = `bed-${bed.id}`;
    for (const v of bed.varieties)
      for (const m of VARIETY_METRICS)
        makeChart(`c-${v.id}-${m.key}`, `${v.id}-${m.key}`, sid, m);
  }
  document.querySelectorAll(".range-switch button").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".range-switch button")
        .forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentRange = btn.dataset.range;
      for (const k in charts) charts[k].resetZoom();
      loadAll();
    });
  });
  loadAll();
}

window.addEventListener("load", initCharts);
setInterval(loadAll, 60000);
