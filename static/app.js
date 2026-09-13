// openGardener dashboard
// Polls /api/status and renders the beds as variety pairs (bed A vs bed B for
// the same strawberry variety), plus ambient tiles and watering history.
// Read-only in this phase; controls arrive with the command path.

const REFRESH_MS = 60000;

// Realtime clock in the garden's timezone (Pacific), so it always matches the
// sensor timestamps regardless of where the dashboard is being viewed from.
function tickClock() {
  const el = document.getElementById("live-clock");
  if (!el) return;
  const now = new Date();
  const s = now.toLocaleString("en-US", {
    timeZone: "America/Los_Angeles",
    weekday: "short", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  });
  el.textContent = s + " PT";
}
setInterval(tickClock, 1000);
tickClock();

// The three varieties, in display order. Each maps to its soil/temp keys in
// each bed. Bed B is mirror-planted, so the keys cross over by design.
const VARIETIES = [
  { name: "San Andreas", a: { soil: "soil0", temp: "temp0" }, b: { soil: "soil5", temp: "temp5" } },
  { name: "Sequoia",     a: { soil: "soil1", temp: "temp1" }, b: { soil: "soil4", temp: "temp4" } },
  { name: "Albion",      a: { soil: "soil2", temp: "temp2" }, b: { soil: "soil3", temp: "temp3" } },
];

function fmtPct(v) {
  if (v === null || v === undefined) return null;
  // Clamp display to 0-100. The raw value can overshoot when soil is wetter
  // or drier than the calibration points; the DB keeps the true number, but
  // a moisture reading over 100% or below 0% is meaningless to read.
  return Math.round(Math.max(0, Math.min(100, v)));
}

function moistClass(v) {
  // dry warning tint when moisture is low. Threshold here is display-only;
  // the real watering threshold is set separately from data.
  return (v !== null && v !== undefined && v < 25) ? "dry" : "";
}

function plotHTML(sensorMap, keys) {
  const soil = sensorMap[keys.soil];
  const temp = sensorMap[keys.temp];

  const online = soil && soil.online && soil.metrics.value;
  if (!online) {
    return `
      <div class="plot-moist offline">&mdash;</div>
      <div class="offline-tag">offline</div>
      <div class="bar"><span style="width:0"></span></div>`;
  }

  const v = soil.metrics.value.value;
  const pct = fmtPct(v);
  const cls = moistClass(v);
  const clamped = Math.max(0, Math.min(100, v));

  let tempHTML = '<div class="plot-temp">temp &mdash;</div>';
  if (temp && temp.online && temp.metrics.value) {
    tempHTML = `<div class="plot-temp">${temp.metrics.value.value.toFixed(1)}&deg;F root</div>`;
  }

  return `
    <div class="plot-moist ${cls}">${pct}<small>%</small></div>
    ${tempHTML}
    <div class="bar"><span class="${cls}" style="width:${clamped}%"></span></div>`;
}

function renderBeds(sensorMap) {
  const beds = document.getElementById("beds");
  beds.innerHTML = VARIETIES.map(vt => `
    <div class="variety">
      <div class="variety-head">
        <span class="variety-name">${vt.name}</span>
        <span class="variety-tag">bed A &middot; bed B</span>
      </div>
      <div class="pair">
        <div class="plot">
          <div class="plot-bed">Bed A</div>
          ${plotHTML(sensorMap, vt.a)}
        </div>
        <div class="divider"></div>
        <div class="plot">
          <div class="plot-bed">Bed B</div>
          ${plotHTML(sensorMap, vt.b)}
        </div>
      </div>
    </div>
  `).join("");
}

function renderNodes(sensorMap) {
  const panel = document.getElementById("nodes");
  if (!panel) return;
  const nodes = Object.values(sensorMap).filter(s => s.kind === "node");
  if (!nodes.length) { panel.innerHTML = ""; return; }

  // Single-cell LiPo bands (voltage, not a linear %; it sits ~3.7V then knees).
  function batt(v) {
    if (v == null) return { txt: "&mdash;", cls: "" };
    if (v > 4.1) return { txt: `${v.toFixed(2)}V full`, cls: "good" };
    if (v >= 3.7) return { txt: `${v.toFixed(2)}V healthy`, cls: "good" };
    if (v >= 3.5) return { txt: `${v.toFixed(2)}V low`, cls: "watch" };
    if (v >= 3.3) return { txt: `${v.toFixed(2)}V critical`, cls: "bad" };
    return { txt: `${v.toFixed(2)}V cutting out`, cls: "bad" };
  }
  function sig(r) {
    if (r == null) return { txt: "&mdash;", cls: "" };
    if (r > -60) return { txt: `${r} dBm strong`, cls: "good" };
    if (r > -75) return { txt: `${r} dBm fine`, cls: "good" };
    if (r > -85) return { txt: `${r} dBm marginal`, cls: "watch" };
    return { txt: `${r} dBm poor`, cls: "bad" };
  }

  panel.innerHTML = `<h2 class="eyebrow">Wireless nodes</h2>
    <div class="node-grid">` + nodes.map(nd => {
    const online = nd.online;
    const v = nd.metrics.battery ? nd.metrics.battery.value : null;
    const r = nd.metrics.rssi ? nd.metrics.rssi.value : null;
    const e = nd.metrics.errors ? nd.metrics.errors.value : null;
    const b = batt(v), s = sig(r);
    const errCls = (e == null) ? "" : (e > 0 ? "watch" : "good");
    const errTxt = (e == null) ? "&mdash;"
                 : (e === 0 ? "all sensors ok" : `${e} sensor${e > 1 ? "s" : ""} failed`);
    return `
      <div class="node-card ${online ? "" : "node-offline"}">
        <div class="node-head">
          <span class="node-name">${nd.label}</span>
          <span class="node-status ${online ? "good" : "bad"}">${
            online ? "online" : "offline"}</span>
        </div>
        <div class="node-row"><span>Battery</span>
          <b class="${b.cls}" title="Read through a 1M/1M divider; coarse trend, not a fuel gauge.">${b.txt}</b></div>
        <div class="node-row"><span>Signal</span>
          <b class="${s.cls}">${s.txt}</b></div>
        <div class="node-row"><span>Sensors</span>
          <b class="${errCls}">${errTxt}</b></div>
      </div>`;
  }).join("") + `</div>`;
}

function renderAmbient(sensorMap) {
  const amb = document.getElementById("ambient");
  const cards = [];

  // Single garden-wide air tile (one shared BME280).
  {
    const s = sensorMap["bme0"];
    const online = s && s.online;
    const t = online && s.metrics.temp ? s.metrics.temp.value : null;
    cards.push(`
      <div class="amb">
        <div class="amb-label">Air</div>
        <div class="amb-val ${online ? "" : "offline"}">${
          t !== null ? t.toFixed(1) : "&mdash;"
        }<span class="amb-unit">${t !== null ? "&deg;F" : ""}</span></div>
        <div class="amb-sub">${online ? "temperature" : "offline"}</div>
      </div>`);
  }

  // Garden-wide humidity tile.
  {
    const s = sensorMap["bme0"];
    const online = s && s.online && s.metrics.humidity;
    const h = online ? s.metrics.humidity.value : null;
    cards.push(`
      <div class="amb">
        <div class="amb-label">Humidity</div>
        <div class="amb-val ${online ? "" : "offline"}">${
          h !== null ? h.toFixed(0) : "&mdash;"
        }<span class="amb-unit">${h !== null ? "% RH" : ""}</span></div>
        <div class="amb-sub">${online ? "relative humidity" : "offline"}</div>
      </div>`);
  }

  // Garden-wide pressure tile with tendency + sparkline (filled async).
  {
    const s = sensorMap["bme0"];
    const online = s && s.online && s.metrics.pressure;
    const p = online ? s.metrics.pressure.value : null;
    cards.push(`
      <div class="amb amb-pressure">
        <div class="amb-label">Pressure</div>
        <div class="amb-val ${online ? "" : "offline"}">${
          p !== null ? p.toFixed(0) : "&mdash;"
        }<span class="amb-unit">${p !== null ? "hPa" : ""}</span></div>
        <div class="amb-sub" id="pressure-tend">${online ? "&hellip;" : "offline"}</div>
        <svg class="amb-spark" id="pressure-spark" viewBox="0 0 120 32"
             preserveAspectRatio="none"></svg>
      </div>`);
  }

  // Single garden-wide light tile (one shared BH1750).
  {
    const s = sensorMap["lux0"];
    const online = s && s.online && s.metrics.value;
    const lx = online ? s.metrics.value.value : null;
    cards.push(`
      <div class="amb">
        <div class="amb-label">Light</div>
        <div class="amb-val ${online ? "" : "offline"}">${
          lx !== null ? Math.round(lx) : "&mdash;"
        }<span class="amb-unit">${lx !== null ? "lux" : ""}</span></div>
        <div class="amb-sub">${online ? "" : "offline"}</div>
      </div>`);
  }

  amb.innerHTML = cards.join("");
  fillPressureTile();
}

// Fetch recent pressure history + tendency and draw the sparkline in the tile.
async function fillPressureTile() {
  const tendEl = document.getElementById("pressure-tend");
  const spark = document.getElementById("pressure-spark");
  if (!tendEl && !spark) return;
  try {
    const r = await fetch("/api/ambient/24h", { cache: "no-store" });
    const d = await r.json();

    // tendency text
    const t = d.pressure_tendency;
    if (tendEl && t && t.change_3h != null) {
      const arrow = t.arrow === "down" ? "\u2198"
                  : t.arrow === "up" ? "\u2197" : "\u2192";
      const sign = t.change_3h > 0 ? "+" : "";
      const cls = t.arrow === "down" && t.change_3h <= -3 ? "warn"
                : t.arrow === "up" ? "good" : "";
      tendEl.innerHTML =
        `<span class="tend-arrow ${cls}">${arrow}</span> ${t.words} `
        + `&middot; 3h ${sign}${t.change_3h}`;
    } else if (tendEl) {
      tendEl.textContent = "building history";
    }

    // sparkline
    if (spark) {
      const series = (d.series && d.series.pressure) || [];
      const vals = series.map(p => p.value).filter(v => v != null);
      if (vals.length >= 2) {
        const min = Math.min(...vals), max = Math.max(...vals);
        const range = max - min || 1;
        const w = 120, h = 32, pad = 2;
        const pts = vals.map((v, i) => {
          const x = pad + (i / (vals.length - 1)) * (w - 2 * pad);
          const y = pad + (1 - (v - min) / range) * (h - 2 * pad);
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(" ");
        spark.innerHTML =
          `<polyline points="${pts}" fill="none" stroke="var(--soil)" `
          + `stroke-width="1.5" vector-effect="non-scaling-stroke" />`;
      }
    }
  } catch (e) { /* leave placeholder */ }
}

function renderMedian(median, n) {
  const el = document.getElementById("median-big");
  if (median === null || median === undefined) {
    el.textContent = "--";
    el.classList.remove("dry");
    return;
  }
  const shown = Math.round(Math.max(0, Math.min(100, median)));
  el.textContent = shown + "%";
  el.classList.toggle("dry", median < 25);
}

function renderWatering(w, cfg) {
  const auto = cfg.auto_water_enabled ? "on" : "off";
  const thr = cfg.threshold_pct === null ? "unset" : cfg.threshold_pct + "%";
  const locked = w.lockout_remaining_s > 0;
  const lockout = locked
    ? `${Math.floor(w.lockout_remaining_s / 60)}m ${w.lockout_remaining_s % 60}s`
    : "none";

  // Inline status chips (OpenSeedling-style: state sits next to the controls).
  const chMed = document.getElementById("chip-median");
  if (chMed) chMed.textContent =
    (window.ogMedian != null ? Math.round(window.ogMedian) : "--");
  const chPul = document.getElementById("chip-pulses");
  if (chPul) chPul.textContent = `${w.pulses_today}/${w.daily_cap ?? "?"}`;
  const chLock = document.getElementById("chip-lockout");
  if (chLock) chLock.textContent = lockout;
  const lockWrap = document.getElementById("chip-lockout-wrap");
  if (lockWrap) lockWrap.classList.toggle("chip-active", locked);

  const status = document.getElementById("water-status");
  if (status) status.innerHTML =
    `last <b>${w.last ? w.last.replace("T", " ") : "never"}</b>`;

  // sync controls to current config (without clobbering focused inputs)
  const autoCb = document.getElementById("auto-toggle");
  const autoState = document.getElementById("auto-state");
  if (autoCb && document.activeElement !== autoCb) {
    autoCb.checked = cfg.auto_water_enabled;
  }
  if (autoState) autoState.textContent = auto;
  const thrIn = document.getElementById("threshold-input");
  if (thrIn && document.activeElement !== thrIn && cfg.threshold_pct !== null) {
    thrIn.value = cfg.threshold_pct;
  }

  const log = document.getElementById("water-log");
  if (!w.recent || w.recent.length === 0) {
    log.innerHTML = `<tr><td class="empty">No watering events yet.</td></tr>`;
    return;
  }
  const rows = w.recent.map(e => `
    <tr>
      <td>${e.ts_start ? e.ts_start.replace("T", " ") : ""}</td>
      <td>${e.trigger || ""}</td>
      <td>${e.duration_seconds != null ? e.duration_seconds + "s" : ""}</td>
      <td>${e.median_at_trigger != null ? Math.round(e.median_at_trigger) + "%" : ""}</td>
      <td>${e.result || ""}</td>
    </tr>`).join("");
  log.innerHTML =
    `<tr><th>When</th><th>Trigger</th><th>Duration</th><th>Median</th><th>Result</th></tr>`
    + rows;
}

// ---- controls ----
function note(msg, cls) {
  const el = document.getElementById("control-note");
  if (!el) return;
  el.textContent = msg;
  el.className = "control-note" + (cls ? " " + cls : "");
}

let waterConfirming = false;
let waterConfirmTimer = null;

function initControls() {
  const btn = document.getElementById("water-btn");
  if (btn) {
    btn.addEventListener("click", async () => {
      if (!waterConfirming) {
        // first tap: arm confirmation
        waterConfirming = true;
        btn.textContent = "Confirm watering?";
        btn.classList.add("confirm");
        note("Tap again to water. Cancels in 4s.", "warn");
        waterConfirmTimer = setTimeout(() => {
          waterConfirming = false;
          btn.textContent = "Water now";
          btn.classList.remove("confirm");
          note("");
        }, 4000);
        return;
      }
      // second tap: fire
      clearTimeout(waterConfirmTimer);
      waterConfirming = false;
      btn.textContent = "Water now";
      btn.classList.remove("confirm");
      try {
        const secsEl = document.getElementById("water-seconds");
        const secs = secsEl ? parseInt(secsEl.value, 10) || 30 : 30;
        const r = await fetch("/api/water", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ seconds: secs }),
        });
        const d = await r.json();
        note(d.ok
          ? `Watering ${secs}s, starting within a couple seconds.`
          : "Failed to send.", d.ok ? "ok" : "warn");
      } catch (e) {
        note("Request failed.", "warn");
      }
    });
  }

  const autoCb = document.getElementById("auto-toggle");
  if (autoCb) {
    autoCb.addEventListener("change", async () => {
      try {
        await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ auto_water_enabled: autoCb.checked ? 1 : 0 }),
        });
        note(autoCb.checked
          ? "Auto-water ON. It will water when median drops to the threshold."
          : "Auto-water OFF.", "ok");
      } catch (e) { note("Failed to update.", "warn"); }
    });
  }

  const thrSave = document.getElementById("thr-save");
  if (thrSave) {
    thrSave.addEventListener("click", async () => {
      const v = document.getElementById("threshold-input").value;
      try {
        const r = await fetch("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ threshold_pct: v }),
        });
        const d = await r.json();
        note(`Threshold set to ${d.updated.threshold_pct}%.`, "ok");
      } catch (e) { note("Failed to save threshold.", "warn"); }
    });
  }
}
window.addEventListener("load", initControls);

// ---- device panel: hardware stats + reboot ----
function fmtUptime(s) {
  if (s == null) return "\u2014";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

async function loadHwStats() {
  try {
    const r = await fetch("/api/hwstats", { cache: "no-store" });
    if (!r.ok) return;
    const s = await r.json();
    const el = document.getElementById("hwstats");
    if (!el) return;

    // CPU temp: warn above 70C
    const tempCls = s.cpu_temp_c == null ? "" :
      (s.cpu_temp_c >= 70 ? "warn" : "good");
    const tempF = s.cpu_temp_c != null
      ? Math.round(s.cpu_temp_c * 9 / 5 + 32) : null;

    // throttle: healthy is 0x0
    const thrOk = s.throttled === "0x0";
    const thrText = s.throttled == null ? "\u2014"
      : (thrOk ? "healthy" : s.throttled);
    const thrDetail = s.throttle_flags
      ? (s.throttle_flags.undervoltage_occurred ? "undervoltage seen" :
         s.throttle_flags.throttled_occurred ? "throttling seen" : "no issues")
      : "";

    const cards = [
      ["CPU temp", s.cpu_temp_c != null ? `${s.cpu_temp_c}\u00b0C` : "\u2014",
       tempF != null ? `${tempF}\u00b0F` : "", tempCls],
      ["Load", s.loadavg ? s.loadavg[0].toFixed(2) : "\u2014",
       s.cores ? `${s.cores} cores` : "", ""],
      ["Memory", s.mem_pct != null ? `${s.mem_pct}%` : "\u2014",
       s.mem_total_mb ? `${s.mem_used_mb}/${s.mem_total_mb} MB` : "",
       s.mem_pct >= 90 ? "warn" : ""],
      ["Disk", s.disk_pct != null ? `${s.disk_pct}%` : "\u2014",
       s.disk_total_gb ? `${s.disk_used_gb}/${s.disk_total_gb} GB` : "",
       s.disk_pct >= 90 ? "warn" : ""],
      ["Uptime", fmtUptime(s.uptime_s), "", ""],
      ["Power", thrText, thrDetail, thrOk ? "good" : (s.throttled ? "warn" : "")],
      ["Core V", s.core_volt || "\u2014", s.arm_mhz ? `${s.arm_mhz} MHz` : "", ""],
      ["Host", s.hostname || "\u2014", s.ip || "", ""],
    ];

    if (s.wifi) {
      const q = s.wifi.quality_pct;
      cards.push(["WiFi", q != null ? `${q}%` : "\u2014",
        `${s.wifi.ssid || ""} ${s.wifi.signal_dbm != null ? s.wifi.signal_dbm + " dBm" : ""}`.trim(),
        q != null && q < 40 ? "warn" : ""]);
    }

    el.innerHTML = cards.map(([label, val, sub, cls]) => `
      <div class="hw">
        <div class="hw-label">${label}</div>
        <div class="hw-val ${cls}">${val}</div>
        ${sub ? `<div class="hw-sub">${sub}</div>` : ""}
      </div>`).join("");
  } catch (e) { /* leave as-is */ }
}

let rebootConfirming = false;
let rebootTimer = null;
function initDevice() {
  const btn = document.getElementById("reboot-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const note = document.getElementById("device-note");
    if (!rebootConfirming) {
      rebootConfirming = true;
      btn.textContent = "Confirm reboot?";
      btn.classList.add("confirm");
      note.textContent = "Tap again to reboot. Cancels in 4s.";
      note.className = "device-note warn";
      rebootTimer = setTimeout(() => {
        rebootConfirming = false;
        btn.textContent = "Reboot Pi";
        btn.classList.remove("confirm");
        note.textContent = "";
      }, 4000);
      return;
    }
    clearTimeout(rebootTimer);
    rebootConfirming = false;
    btn.textContent = "Reboot Pi";
    btn.classList.remove("confirm");
    try {
      await fetch("/api/reboot", { method: "POST" });
      note.textContent = "Reboot queued. Valve will close, then the Pi restarts (~1 min).";
      note.className = "device-note ok";
    } catch (e) {
      note.textContent = "Request failed.";
      note.className = "device-note warn";
    }
  });
  loadHwStats();
  setInterval(loadHwStats, 30000);
}
window.addEventListener("load", initDevice);

// ---- records & summaries ----
function fmtTs(ts) { return ts ? ts.replace("T", " ") : "\u2014"; }

async function loadSummaries() {
  const grid = document.getElementById("summary-grid");
  if (!grid) return;
  try {
    const r = await fetch("/api/summaries", { cache: "no-store" });
    const d = await r.json();
    const cards = [];

    // watering totals
    const wk = d.watering_week || {};
    const secs = wk.secs || 0;
    cards.push(sumCard("This week's watering",
      `${wk.n || 0} pulses`,
      `${Math.round(secs)}s total valve time`));

    // wettest / driest (7d)
    if (d.wettest) {
      cards.push(sumCard("Wettest reading (7d)",
        `${Math.round(Math.min(100, d.wettest.value))}%`,
        `${labelFor(d.wettest.sensor_key)} \u00b7 ${fmtTs(d.wettest.ts)}`));
    }
    if (d.driest) {
      cards.push(sumCard("Driest reading (7d)",
        `${Math.round(Math.max(0, d.driest.value))}%`,
        `${labelFor(d.driest.sensor_key)} \u00b7 ${fmtTs(d.driest.ts)}`));
    }
    if (d.hottest_soil) {
      cards.push(sumCard("Hottest soil (7d)",
        `${d.hottest_soil.value.toFixed(0)}\u00b0F`,
        `${labelFor(d.hottest_soil.sensor_key)} \u00b7 ${fmtTs(d.hottest_soil.ts)}`));
    }

    // per-variety current moisture comparison
    if (d.by_variety && d.by_variety.length) {
      const rows = d.by_variety.map(v =>
        `<div class="sv-row"><span>${v.label} <em>${v.grp}</em></span>`
        + `<b>${v.value != null ? Math.round(Math.min(100, v.value)) + "%" : "\u2014"}</b></div>`
      ).join("");
      cards.push(`<div class="summary-card wide">
        <div class="sum-label">Current soil by variety</div>
        <div class="sv-list">${rows}</div></div>`);
    }

    // export button
    cards.push(`<div class="summary-card">
      <div class="sum-label">Data</div>
      <button class="export-btn" id="export-btn">Export CSV</button>
      <div class="sum-sub">full reading history</div></div>`);

    grid.innerHTML = cards.join("");

    const eb = document.getElementById("export-btn");
    if (eb) eb.addEventListener("click", () => {
      window.location.href = "/api/export.csv";
    });
  } catch (e) {
    grid.innerHTML = `<p class="loading">Summaries unavailable.</p>`;
  }
}

function sumCard(label, big, sub) {
  return `<div class="summary-card">
    <div class="sum-label">${label}</div>
    <div class="sum-big">${big}</div>
    <div class="sum-sub">${sub || ""}</div></div>`;
}

// map a sensor_key to its variety label using the status data if present
let _sensorLabels = {};
// Label and bed group per sensor key, kept separate from the combined
// display string above because the calibration panel groups rows by bed.
let _sensorMeta = {};
function labelFor(key) { return _sensorLabels[key] || key; }

window.addEventListener("load", () => {
  loadSummaries();
  setInterval(loadSummaries, 5 * 60 * 1000);
});

// ---- AI garden assessment ----
function healthClass(h) {
  return h === "good" ? "good" : h === "problem" ? "problem" : "watch";
}

function renderAiReport(d) {
  const body = document.getElementById("ai-body");
  if (!body) return;

  if (!d || !d.available) {
    body.innerHTML = d && d.has_key === false
      ? `<p class="ai-empty">No API key configured on the Pi. Add one to enable the garden assessment.</p>`
      : `<p class="ai-empty">No assessment yet. Tap "Generate now" for one.</p>`;
    return;
  }
  if (!d.ok) {
    body.innerHTML = `<p class="ai-empty">Last attempt failed: ${d.error || "unknown error"}</p>`;
    return;
  }
  const rep = d.report || {};
  const when = d.ts ? new Date(d.ts * 1000).toLocaleString("en-US", {
    timeZone: "America/Los_Angeles", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit" }) : "";
  const health = rep.overall_health || "watch";

  let html = `
    <div class="ai-summary-row">
      <span class="ai-badge ${healthClass(health)}">${health}</span>
      <span class="ai-summary">${rep.summary || ""}</span>
    </div>
    <div class="ai-meta">assessed ${when} PT`;
  if (rep.confidence) html += ` &middot; confidence ${rep.confidence}`;
  html += `</div>`;

  // water + heat quick reads
  const chips = [];
  if (rep.water && rep.water.assessment)
    chips.push(`<span class="ai-chip">water: <b>${rep.water.assessment.replace("_", " ")}</b></span>`);
  if (rep.heat && rep.heat.assessment)
    chips.push(`<span class="ai-chip">heat: <b>${rep.heat.assessment}</b></span>`);
  if (chips.length) html += `<div class="ai-chips">${chips.join("")}</div>`;

  // concerns
  if (rep.concerns && rep.concerns.length) {
    html += `<div class="ai-block"><div class="ai-block-label">Concerns</div><ul>`
      + rep.concerns.map(c => `<li>${c}</li>`).join("") + `</ul></div>`;
  }
  // recommendations
  if (rep.recommendations && rep.recommendations.length) {
    html += `<div class="ai-block"><div class="ai-block-label">Recommendations</div><ul>`
      + rep.recommendations.map(r => `<li>${r}</li>`).join("") + `</ul></div>`;
  }
  // notable per-variety
  if (rep.per_variety && rep.per_variety.length) {
    html += `<div class="ai-block"><div class="ai-block-label">Notable planters</div><ul>`
      + rep.per_variety.map(p => `<li><b>${p.planter}:</b> ${p.note}</li>`).join("")
      + `</ul></div>`;
  }
  body.innerHTML = html;
}

async function loadAiReport() {
  try {
    const r = await fetch("/api/ai_report", { cache: "no-store" });
    renderAiReport(await r.json());
  } catch (e) {
    const body = document.getElementById("ai-body");
    if (body) body.innerHTML = `<p class="ai-empty">Could not load assessment.</p>`;
  }
}

function initAiReport() {
  const btn = document.getElementById("ai-run-btn");
  if (btn) {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "Generating\u2026";
      try {
        await fetch("/api/ai_report/run", { method: "POST" });
        // the logger generates it; poll a few times for the fresh result
        let tries = 0;
        const poll = setInterval(async () => {
          tries++;
          await loadAiReport();
          if (tries >= 12) {  // ~1 min
            clearInterval(poll);
            btn.disabled = false;
            btn.textContent = "Generate now";
          }
        }, 5000);
      } catch (e) {
        btn.disabled = false;
        btn.textContent = "Generate now";
      }
    });
  }
  loadAiReport();
  setInterval(loadAiReport, 10 * 60 * 1000);
}
window.addEventListener("load", initAiReport);

async function tick() {
  const dot = document.getElementById("live-dot");
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error("status " + r.status);
    const d = await r.json();

    const sensorMap = {};
    for (const s of d.sensors) {
      sensorMap[s.key] = s;
      _sensorLabels[s.key] = `${s.label} (${s.group})`;
      _sensorMeta[s.key] = { label: s.label, group: s.group };
    }

    // Expose threshold and watering events for the chart layer to draw.
    window.ogThreshold = d.config.threshold_pct;
    window.ogMedian = d.median_soil;
    window.ogFrostF = d.config.frost_alert_f ? Number(d.config.frost_alert_f) : null;
    window.ogWatering = d.watering.recent || [];

    renderMedian(d.median_soil, d.median_n);
    renderBeds(sensorMap);
    renderAmbient(sensorMap);
    renderNodes(sensorMap);
    renderWatering(d.watering, d.config);

    document.getElementById("stamp").textContent =
      "updated " + d.now.replace("T", " ");
    dot.classList.remove("stale");
  } catch (e) {
    document.getElementById("stamp").textContent = "connection lost, retrying";
    dot.classList.add("stale");
  }
}

tick();
setInterval(tick, REFRESH_MS);

/* ------------------------------------------------------------------ *
 * Probe calibration
 *
 * Drives the calibration API that already lives in opengardener_ingest.py:
 *   GET  /api/calibration                 -> per-sensor dry/wet/temp_comp,
 *                                            latest raw volts and timestamp
 *   POST /api/calibration/capture         -> {sensor_key, point}
 *   POST /api/calibration/temp_comp       -> {sensor_key, coeff, ref_f}
 *
 * That API keys on sensor_key (soil0..soil5), not planter id, so names come
 * from the sensor registry in /api/status. Nothing here touches hardware: the
 * server records whatever raw voltage was last written to the readings table,
 * which is why every row shows how old that voltage is. Both beds report from
 * sleeping nodes, so a capture taken against a ten-minute-old voltage is a
 * capture of where the probe was ten minutes ago.
 * ------------------------------------------------------------------ */

const CAL_POLL_MS = 15000;
// Nodes deep-sleep ~600s between posts. Three sleeps of slack before a
// voltage is treated as too old to anchor a calibration to.
const CAL_MAX_AGE_S = 1800;

let calTimer = null;
let calBusy = false;

function calAge(ts) {
  if (!ts) return null;
  const t = Date.parse(ts.replace(" ", "T"));
  if (isNaN(t)) return null;
  return Math.max(0, Math.round((Date.now() - t) / 1000));
}

function calAgeText(age) {
  if (age === null) return "no reading";
  if (age < 90) return age + "s ago";
  if (age < 5400) return Math.round(age / 60) + "m ago";
  return Math.round(age / 3600) + "h ago";
}

function calStateText(s) {
  if (s.valid) return "calibrated";
  if (s.dry === null && s.wet === null) return "not calibrated";
  if (s.dry === null) return "wet captured, dry missing";
  if (s.wet === null) return "dry captured, wet missing";
  return "inverted: dry must read higher than wet";
}

function calRowHTML(key, s, label) {
  const age = calAge(s.latest_ts);
  const stale = (age === null || age > CAL_MAX_AGE_S);
  const v = (s.latest_volts === null || s.latest_volts === undefined)
    ? "\u2014" : s.latest_volts.toFixed(4) + "V";
  // Unit only when there is a number; "\u2014V" reads like a broken value.
  const anchor = x => (x === null || x === undefined)
    ? "\u2014" : Number(x).toFixed(3) + '<span class="u">V</span>';
  const pct = (s.would_read_pct === null || s.would_read_pct === undefined)
    ? "\u2014" : s.would_read_pct + "%";
  // A percentage outside 0-100 means this voltage sits past one of the
  // anchors, so the anchor was captured in the wrong condition.
  const pctBad = (s.would_read_pct !== null && s.would_read_pct !== undefined
                  && (s.would_read_pct < 0 || s.would_read_pct > 100));
  const tc = s.temp_comp || {};
  return `
    <div class="cal-row" data-key="${key}">
      <div class="cal-id">
        <b>${label}</b>
        <span class="cal-meta">${key}</span>
      </div>
      <div class="cal-live">
        <span class="cal-volts${stale ? " stale" : ""}">${v}</span>
        <span class="cal-age${stale ? " stale" : ""}">${calAgeText(age)}</span>
      </div>
      <div class="cal-anchors">
        <span class="schip">dry <b>${anchor(s.dry)}</b></span>
        <span class="schip">wet <b>${anchor(s.wet)}</b></span>
        <span class="schip${pctBad ? " chip-bad" : ""}">reads <b>${pct}</b></span>
      </div>
      <div class="cal-actions">
        <button type="button" class="cal-btn" data-act="dry"
          ${stale ? "disabled" : ""}>Capture dry</button>
        <button type="button" class="cal-btn" data-act="wet"
          ${stale ? "disabled" : ""}>Capture wet</button>
      </div>
      <div class="cal-tc">
        <span class="cal-tclabel">temp comp</span>
        <input type="number" class="cal-coeff" step="0.01" min="0" max="1"
          value="${tc.coeff === undefined || tc.coeff === null ? "" : tc.coeff}"
          aria-label="${label} temperature coefficient">
        <span class="u">%/&deg;F @</span>
        <input type="number" class="cal-ref" step="1"
          value="${tc.ref_f === undefined || tc.ref_f === null ? 70 : tc.ref_f}"
          aria-label="${label} reference temperature">
        <span class="u">&deg;F</span>
        <button type="button" class="cal-btn" data-act="tc">Set</button>
      </div>
      <div class="cal-state ${s.valid ? "ok" : "bad"}">${calStateText(s)}</div>
    </div>`;
}

function renderCalibration(d) {
  const grid = document.getElementById("cal-grid");
  if (!grid) return;
  const sensors = d.sensors || {};
  const keys = Object.keys(sensors).sort();
  if (!keys.length) {
    grid.innerHTML = '<p class="loading">no soil sensors reported</p>';
    return;
  }

  // Group by bed using the sensor registry's group, so the rows follow the
  // same A/B split as the rest of the dashboard rather than key order.
  const beds = {};
  for (const k of keys) {
    const meta = _sensorMeta[k] || {};
    const bed = meta.group || "?";
    (beds[bed] = beds[bed] || []).push([k, sensors[k], meta.label || k]);
  }

  grid.innerHTML = Object.keys(beds).sort().map(b => `
    <div class="cal-bed">
      <div class="cal-bed-head">Bed ${b}</div>
      ${beds[b].map(([k, s, label]) => calRowHTML(k, s, label)).join("")}
    </div>`).join("");

  const sub = document.getElementById("cal-sub");
  if (sub) {
    const good = keys.filter(k => sensors[k].valid).length;
    sub.textContent = `${good}/${keys.length} calibrated`;
    sub.classList.toggle("bad", good < keys.length);
  }

  grid.querySelectorAll(".cal-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const row = btn.closest(".cal-row");
      const key = row.dataset.key;
      if (btn.dataset.act === "tc") {
        const coeff = row.querySelector(".cal-coeff").value;
        const ref = row.querySelector(".cal-ref").value;
        calPost("/api/calibration/temp_comp",
                { sensor_key: key, coeff: Number(coeff), ref_f: Number(ref) });
      } else {
        calPost("/api/calibration/capture",
                { sensor_key: key, point: btn.dataset.act });
      }
    });
  });

  const path = document.getElementById("cal-path");
  if (path && d.path) path.textContent = d.path;
}

async function calPost(url, body) {
  if (calBusy) return;
  const info = document.getElementById("cal-info");
  calBusy = true;
  if (info) { info.classList.remove("bad"); info.textContent = "saving\u2026"; }
  try {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json().catch(() => ({}));
    if (info) {
      if (j.ok && j.point) {
        info.textContent = `${j.sensor_key} ${j.point} = ${j.volts}V`
          + (j.complete ? "" : " (other point still missing)");
      } else if (j.ok) {
        info.textContent = `${body.sensor_key} temp comp set`
          + (j.warning ? ` \u2014 ${j.warning}` : "");
        if (j.warning) info.classList.add("bad");
      } else {
        // The server refuses an inverted pair outright, so this is where a
        // swapped dry/wet capture is reported. Keep the full message.
        info.textContent = j.error || ("HTTP " + r.status);
        info.classList.add("bad");
      }
    }
  } catch (e) {
    if (info) { info.textContent = "request failed"; info.classList.add("bad"); }
  } finally {
    calBusy = false;
    loadCalibration();
  }
}

async function loadSensorMeta() {
  // The calibration panel can open before the first dashboard tick has run,
  // and without the registry every row would be titled "soil3" and filed
  // under bed "?". One extra fetch, only when the map is still empty.
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    const d = await r.json();
    for (const s of (d.sensors || [])) {
      _sensorMeta[s.key] = { label: s.label, group: s.group };
    }
  } catch (e) { /* rows fall back to the sensor key */ }
}

async function loadCalibration() {
  try {
    if (!Object.keys(_sensorMeta).length) await loadSensorMeta();
    const r = await fetch("/api/calibration", { cache: "no-store" });
    if (!r.ok) throw new Error("status " + r.status);
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || "not ok");
    renderCalibration(d);
  } catch (e) {
    const grid = document.getElementById("cal-grid");
    if (grid) grid.innerHTML =
      '<p class="loading">calibration unavailable: ' + e.message + "</p>";
  }
}

function initCalibration() {
  const wrap = document.getElementById("cal-wrap");
  if (!wrap) return;
  // Only poll while the panel is open. The live voltage has to move faster
  // than the 60s dashboard refresh or "watch it while the probe soaks" is
  // guesswork; closed, this costs nothing.
  wrap.addEventListener("toggle", () => {
    clearInterval(calTimer);
    if (wrap.open) {
      loadCalibration();
      calTimer = setInterval(loadCalibration, CAL_POLL_MS);
    }
  });
  loadCalibration();   // fill the summary line without opening the panel
}
window.addEventListener("load", initCalibration);
