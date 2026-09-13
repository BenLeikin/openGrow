#!/usr/bin/env python3
"""Daily AI garden report for openGardener.

Sends the current sensor picture (per-variety soil moisture and temperature,
ambient air/light/pressure, recent watering, dry-down trend) to the Claude API
and gets back a structured horticultural assessment: overall health, per-variety
notes, water and heat assessment, concerns, and concrete recommendations. The
report is stored as JSON for the dashboard and a one-line summary is pushed to
Discord. Designed to run once a day, so cost is a few cents at most.

Adapted from the grow project's ai_report.py. That version is photo-centric
(judging seedlings from a tray image); this one is data-centric because the
garden has no camera yet. When a camera is added, a photo can be folded in the
same way -- pass photo_path to generate().

API key (in priority order):
  1. ANTHROPIC_API_KEY environment variable
  2. a .anthropic_key file next to this module (gitignored)

No SDK dependency -- POSTs to the Messages API with stdlib urllib.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import opengardener_db as db

API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-4-8"
KEY_FILE = Path(__file__).with_name(".anthropic_key")
REPORT_FILE = os.path.expanduser("~/.opengardener_ai_report.json")
MAX_IMG_W = 1024


def api_key():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k.strip()
    try:
        if KEY_FILE.exists():
            return KEY_FILE.read_text().strip() or None
    except Exception:
        pass
    return None


def have_key():
    return bool(api_key())


def _image_b64(path):
    """Base64 JPEG of a photo, downscaled to MAX_IMG_W. Only used if/when a
    camera is added; falls back to raw bytes without OpenCV."""
    try:
        import cv2
        img = cv2.imread(str(path))
        if img is not None:
            h, w = img.shape[:2]
            if max(h, w) > MAX_IMG_W:
                s = MAX_IMG_W / float(max(h, w))
                img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                return base64.b64encode(buf.tobytes()).decode()
    except Exception:
        pass
    return base64.b64encode(Path(path).read_bytes()).decode()


PROMPT = """You are an expert horticulturist reviewing a daily automated report \
from an outdoor strawberry garden monitor. The garden has two beds (A and B), \
each with three day-neutral strawberry varieties: San Andreas, Sequoia, and \
Albion. Each planter is a hanging planter with its own soil moisture and soil \
temperature probe. Air, light, and barometric pressure are measured once for \
the whole garden. The system waters via drip on a threshold, with safety limits.

Soil moisture is calibrated so 100% is field capacity (saturated planter soil \
just after watering) and lower values mean drier. The published rule of thumb is \
to water day-neutral strawberries at roughly 50% of field capacity; small \
hanging planters dry fast, so err slightly wet. Root-zone temperature matters: \
best function is around 64-68F, and sustained soil temps above ~86F cause heat \
stress that can stall fruit set and speed ripening.

Study the controller data provided, then return ONE JSON object and nothing else \
(no prose, no code fences) with exactly these fields:

{
  "summary": "1-2 sentence plain-English status, suitable for a phone notification",
  "overall_health": "good" | "watch" | "problem",
  "per_variety": [ {"planter": "San Andreas (A)", "note": "<short observation>"} ],
  "water": {"assessment": "too_dry" | "ok" | "too_wet" | "unsure", "reason": "<use the moisture numbers and dry-down trend>"},
  "heat": {"assessment": "cool" | "ok" | "warm" | "hot" | "unsure", "reason": "<use the soil temperatures>"},
  "concerns": [ "<specific issue: a planter drying much faster, one running hot, a sensor that looks stuck/offline, watering not recovering moisture, etc.>" ],
  "recommendations": [ "<concrete action the grower can take today>" ],
  "confidence": "low" | "medium" | "high"
}

Rules:
- Be specific and honest. If the data is thin or ambiguous, say so and lower "confidence" rather than inventing detail.
- Only flag a concern the data actually supports; use an empty array if there are none.
- "per_variety" should list only notable planters (driest, hottest, an outlier, or a standout), not all six every day.
- Compare A vs B for each variety when useful, since that is the whole point of the paired planting.
- Keep each string concise."""


def collect_data():
    """Build the current garden picture from the database."""
    data = {"date": time.strftime("%Y-%m-%d")}

    # per-variety latest soil moisture + temp
    # map sensor keys to variety labels
    varieties = [
        ("soil0", "temp0", "San Andreas (A)"),
        ("soil1", "temp1", "Sequoia (A)"),
        ("soil2", "temp2", "Albion (A)"),
        ("soil3", "temp3", "San Andreas (B)"),
        ("soil4", "temp4", "Sequoia (B)"),
        ("soil5", "temp5", "Albion (B)"),
    ]
    latest = {r["sensor_key"]: r for r in db.latest_readings()}
    per = []
    for soil_k, temp_k, label in varieties:
        m = latest.get(soil_k, {}).get("value")
        t = latest.get(temp_k, {}).get("value")
        per.append({"planter": label,
                    "moisture_pct": round(m) if m is not None else None,
                    "soil_temp_f": round(t, 1) if t is not None else None})
    data["planters"] = per

    # 24h dry-down: change in median soil moisture over the last day
    med_now, _ = db.median_soil_moisture(band=(-5.0, 105.0))
    data["median_soil_now"] = round(med_now) if med_now is not None else None

    # ambient
    air = latest.get("bme0", {})
    data["air_temp_f"] = air.get("value")
    data["ambient"] = {
        "air_temp_f": (latest.get("bme0") or {}).get("value"),
    }
    # pull air metrics explicitly
    for r in db.latest_readings():
        if r["sensor_key"] == "bme0":
            data["ambient"][r["metric"]] = r["value"]
        if r["sensor_key"] == "lux0" and r["metric"] == "value":
            data["ambient"]["light_lux"] = r["value"]

    # pressure tendency (weather signal)
    pt = db.pressure_tendency("bme0")
    if pt:
        data["pressure_tendency"] = {
            "words": pt["words"], "change_3h": pt["change_3h"],
            "change_24h": pt["change_24h"]}

    # watering
    recent = db.recent_watering(limit=5)
    data["pulses_today"] = db.pulses_today()
    data["recent_watering"] = [
        {"ts": e.get("ts_start"), "trigger": e.get("trigger"),
         "seconds": e.get("duration_seconds"),
         "median_at_trigger": e.get("median_at_trigger")}
        for e in recent]

    # config context
    cfg = db.get_config()
    data["threshold_pct"] = cfg.get("threshold_pct")
    data["auto_water"] = cfg.get("auto_water_enabled") == "1"
    data["location"] = "Thousand Oaks, CA"
    return data


def build_context(d):
    """Compact text block for the prompt from the collected data."""
    L = [f"Date: {d.get('date','?')}", f"Location: {d.get('location','?')}"]
    L.append(f"Auto-water: {'on' if d.get('auto_water') else 'off'}, "
             f"threshold {d.get('threshold_pct','?')}% of field capacity")
    if d.get("median_soil_now") is not None:
        L.append(f"Median soil moisture now: {d['median_soil_now']}%")
    L.append("Per-planter (moisture % / soil temp F):")
    for p in d.get("planters", []):
        L.append(f"  {p['planter']}: "
                 f"{p['moisture_pct'] if p['moisture_pct'] is not None else 'n/a'}% / "
                 f"{p['soil_temp_f'] if p['soil_temp_f'] is not None else 'n/a'}F")
    amb = d.get("ambient", {})
    if amb:
        L.append(f"Ambient: air {amb.get('temp','?')}F, "
                 f"humidity {amb.get('humidity','?')}%, "
                 f"pressure {amb.get('pressure','?')}hPa, "
                 f"light {amb.get('light_lux','?')} lux")
    pt = d.get("pressure_tendency")
    if pt:
        L.append(f"Pressure trend: {pt['words']} "
                 f"(3h {pt['change_3h']}, 24h {pt['change_24h']} hPa)")
    L.append(f"Watering pulses today: {d.get('pulses_today',0)}")
    rw = d.get("recent_watering") or []
    if rw:
        L.append("Recent watering:")
        for e in rw[:5]:
            L.append(f"  {e.get('ts','?')} {e.get('trigger','?')} "
                     f"{e.get('seconds','?')}s (median at trigger "
                     f"{e.get('median_at_trigger','?')}%)")
    return "\n".join(L)


def _extract_json(text):
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
    try:
        i, j = t.index("{"), t.rindex("}")
        return json.loads(t[i:j + 1])
    except Exception:
        return None


def generate(data=None, photo_path=None, model=None, max_tokens=2048, timeout=90):
    """Call the Claude API with the garden data (and optionally a photo).
    Returns a dict {ok, report?, raw?, ts, model, usage?, error?}. Never raises."""
    key = api_key()
    if not key:
        return {"ok": False, "error": "no API key (set ANTHROPIC_API_KEY or add a "
                ".anthropic_key file)", "ts": int(time.time())}
    if data is None:
        data = collect_data()
    model = model or DEFAULT_MODEL

    content = []
    if photo_path and Path(photo_path).exists():
        try:
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": _image_b64(photo_path)}})
        except Exception:
            pass
    content.append({"type": "text",
                    "text": PROMPT + "\n\nController data:\n" + build_context(data)})

    body = {"model": model, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": ANTHROPIC_VERSION}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:300]
        except Exception:
            pass
        return {"ok": False, "error": f"API HTTP {e.code}: {detail}",
                "ts": int(time.time())}
    except Exception as e:
        return {"ok": False, "error": f"request failed: {e}", "ts": int(time.time())}

    text = "".join(b.get("text", "") for b in resp.get("content", [])
                   if b.get("type") == "text")
    report = _extract_json(text)
    out = {"ok": True, "ts": int(time.time()), "model": model,
           "usage": resp.get("usage"), "raw": text}
    if report is not None:
        out["report"] = report
    else:
        out["report"] = {"summary": text.strip()[:300] or "(no summary)",
                         "overall_health": "watch",
                         "concerns": ["AI reply was not valid JSON; see raw text"],
                         "recommendations": [], "confidence": "low"}
        out["parse_error"] = True
    return out


def run_and_store(push=True):
    """Generate a report, save it for the dashboard, and push a Discord summary.
    Returns the result dict."""
    result = generate()
    try:
        with open(REPORT_FILE, "w") as f:
            json.dump(result, f, indent=2)
    except Exception as e:
        print(f"could not save AI report: {e}")

    if push and result.get("ok") and result.get("report"):
        rep = result["report"]
        health = rep.get("overall_health", "watch")
        level = {"good": "good", "watch": "watch", "problem": "problem"}.get(
            health, "info")
        summary = rep.get("summary", "(no summary)")
        recs = rep.get("recommendations") or []
        try:
            import discord_alert
            fields = []
            if recs:
                fields.append({"name": "Recommendations",
                               "value": "\n".join(f"- {r}" for r in recs[:4])})
            concerns = rep.get("concerns") or []
            if concerns:
                fields.append({"name": "Concerns",
                               "value": "\n".join(f"- {c}" for c in concerns[:4])})
            discord_alert.send(f"Daily garden report ({health})", summary,
                               level=level, fields=fields or None)
        except Exception as e:
            print(f"discord push failed: {e}")
    return result


def load_latest():
    """Read the last stored report for the dashboard."""
    try:
        with open(REPORT_FILE) as f:
            return json.load(f)
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    print("API key present:", have_key())
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        print(json.dumps(run_and_store(), indent=2)[:2000])
    else:
        # dry preview of the context that would be sent, no API call
        print(build_context(collect_data()))
