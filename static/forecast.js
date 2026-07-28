// openGardener mini forecast (masthead)
// Renders the next few NWS daily periods as compact animated glyph+temp units
// in the masthead center. Skip status shows when rain-skip would hold watering.

function glyphFor(short, isDay) {
  const s = (short || "").toLowerCase();
  if (/(thunder|storm|rain|shower|drizzle|snow|sleet|flurr)/.test(s)) return rainGlyph();
  if (/(cloud|overcast|fog|haze|mist)/.test(s)) return cloudGlyph();
  if (/(partly|mostly)\s+(sunny|clear)/.test(s)) return (isDay ? sunGlyph() : moonGlyph()) + cloudGlyph();
  if (/(sun|clear|fair)/.test(s)) return isDay ? sunGlyph() : moonGlyph();
  return isDay ? sunGlyph() : moonGlyph();
}

function sunGlyph() {
  const rays = Array.from({ length: 8 }, (_, i) =>
    `<span style="transform:rotate(${i * 45}deg)"></span>`).join("");
  return `<div class="wx-sun"><div class="rays">${rays}</div><div class="disc"></div></div>`;
}
function moonGlyph() {
  return `<div class="wx-moon"><div class="m"></div></div>`;
}
function cloudGlyph() {
  return `<div class="wx-cloud"><div class="puff p1"></div><div class="puff p2"></div><div class="puff p3"></div></div>`;
}
function rainGlyph() {
  return cloudGlyph() +
    `<div class="wx-rain"><div class="drop d1"></div><div class="drop d2"></div><div class="drop d3"></div></div>`;
}

// Shorten NWS period names for the tight masthead space.
function shortName(name) {
  if (!name) return "";
  const map = {
    "This Afternoon": "Today", "Tonight": "Tonight", "Today": "Today",
    "Overnight": "Tonight",
  };
  if (map[name]) return map[name];
  // "Wednesday Night" -> "Wed nt", "Wednesday" -> "Wed"
  const parts = name.split(" ");
  const day = parts[0].slice(0, 3);
  return parts.includes("Night") ? day + " nt" : day;
}

async function loadForecast() {
  const el = document.getElementById("mini-forecast");
  if (!el) return;
  try {
    const r = await fetch("/api/forecast", { cache: "no-store" });
    const d = await r.json();
    if (d.error || !d.periods || !d.periods.length) {
      el.innerHTML = "";
      return;
    }

    // If rain-skip is enabled and active, show a skip note in place of glyphs.
    let skipNote = "";
    if (d.skip && d.skip.enabled && d.skip.active) {
      skipNote = `<div class="mf-skip">watering held: rain expected</div>`;
    }

    const items = d.periods.slice(0, 5).map(p => {
      const precip = p.precipProb != null && p.precipProb > 0
        ? `<div class="mf-precip">${p.precipProb}%</div>` : "";
      return `
        <div class="mf-item">
          <div class="mf-name">${shortName(p.name)}</div>
          <div class="mf-glyph fc-glyph">${glyphFor(p.short, p.isDaytime)}</div>
          <div class="mf-temp">${p.temp}&deg;</div>
          ${precip}
        </div>`;
    }).join("");

    el.innerHTML = items + skipNote;
  } catch (e) {
    el.innerHTML = "";
  }
}

window.addEventListener("load", loadForecast);
setInterval(loadForecast, 30 * 60 * 1000);
