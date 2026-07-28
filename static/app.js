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

function renderAmbient(sensorMap) {
  const amb = document.getElementById("ambient");
  const cards = [];

  for (const [key, grp] of [["bme0", "A"], ["bme1", "B"]]) {
    const s = sensorMap[key];
    const online = s && s.online;
    const t = online && s.metrics.temp ? s.metrics.temp.value : null;
    const h = online && s.metrics.humidity ? s.metrics.humidity.value : null;
    const p = online && s.metrics.pressure ? s.metrics.pressure.value : null;
    cards.push(`
      <div class="amb">
        <div class="amb-label">Bed ${grp} air</div>
        <div class="amb-val ${online ? "" : "offline"}">${
          t !== null ? t.toFixed(1) : "&mdash;"
        }<span class="amb-unit">${t !== null ? "&deg;F" : ""}</span></div>
        <div class="amb-sub">${
          online && h !== null
            ? `${h.toFixed(0)}% RH &middot; ${p !== null ? p.toFixed(0) + " hPa" : ""}`
            : "offline"
        }</div>
      </div>`);
  }

  for (const [key, grp] of [["lux0", "A"], ["lux1", "B"]]) {
    const s = sensorMap[key];
    const online = s && s.online && s.metrics.value;
    const lx = online ? s.metrics.value.value : null;
    cards.push(`
      <div class="amb">
        <div class="amb-label">Bed ${grp} light</div>
        <div class="amb-val ${online ? "" : "offline"}">${
          lx !== null ? Math.round(lx) : "&mdash;"
        }<span class="amb-unit">${lx !== null ? "lux" : ""}</span></div>
        <div class="amb-sub">${online ? "" : "offline"}</div>
      </div>`);
  }

  amb.innerHTML = cards.join("");
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
  const status = document.getElementById("water-status");
  const auto = cfg.auto_water_enabled ? "on" : "off";
  const thr = cfg.threshold_pct === null ? "unset" : cfg.threshold_pct + "%";
  const lockout = w.lockout_remaining_s > 0
    ? `locked ${Math.floor(w.lockout_remaining_s / 60)}m ${w.lockout_remaining_s % 60}s`
    : "ready";
  status.innerHTML = `
    Auto-water <b>${auto}</b> &middot; threshold <b>${thr}</b>
    &middot; pulses today <b>${w.pulses_today}/${w.daily_cap ?? "?"}</b>
    &middot; ${lockout}
    &middot; last <b>${w.last ? w.last.replace("T", " ") : "never"}</b>`;

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
        const r = await fetch("/api/water", { method: "POST" });
        const d = await r.json();
        note(d.ok ? "Watering queued. The logger will pulse within a minute, subject to limits." : "Failed to queue.", d.ok ? "ok" : "warn");
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
function labelFor(key) { return _sensorLabels[key] || key; }

window.addEventListener("load", () => {
  loadSummaries();
  setInterval(loadSummaries, 5 * 60 * 1000);
});

async function tick() {
  const dot = document.getElementById("live-dot");
  try {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error("status " + r.status);
    const d = await r.json();

    const sensorMap = {};
    for (const s of d.sensors) { sensorMap[s.key] = s; _sensorLabels[s.key] = `${s.label} (${s.group})`; }

    // Expose threshold and watering events for the chart layer to draw.
    window.ogThreshold = d.config.threshold_pct;
    window.ogFrostF = d.config.frost_alert_f ? Number(d.config.frost_alert_f) : null;
    window.ogWatering = d.watering.recent || [];

    renderMedian(d.median_soil, d.median_n);
    renderBeds(sensorMap);
    renderAmbient(sensorMap);
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
