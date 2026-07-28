#!/usr/bin/env python3
"""
openGardener Flask app.

Serves the dashboard page and read-only JSON endpoints. Reads from SQLite via
the shared opengardener_db module. Touches NO hardware: the logger owns the
bus and valve. Control endpoints (manual water, config) are deliberately absent
in this phase and will write to the commands table when added.

Runs behind nginx (opengarden.pilg0re.net) which terminates TLS. Binds to the
LAN so the nginx box at can proxy to it.

Run for a quick test:
  ~/garden/bin/python opengardener_web.py
Then hit http://10.0.2.221:5000/ from the LAN, or via nginx once configured.

Production: run under the systemd unit (opengardener-web.service) which uses
gunicorn for a proper WSGI server rather than Flask's dev server.
"""

import os
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template

import opengardener_db as db

app = Flask(__name__, template_folder="templates", static_folder="static")

# Soil in-band range: readings outside this are treated as a disconnected or
# faulty sensor and excluded from the median (matches the logger's watering
# logic). Wide enough to keep real readings, tight enough to drop the 0.06V /
# pinned-rail failures seen during bring-up.
SOIL_BAND = (-5.0, 105.0)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    """Everything the dashboard needs for one refresh: sensor registry, latest
    values, median, config, valve/watering status."""
    sensors = db.get_sensors()
    latest = db.latest_readings()

    # index latest by (sensor_key, metric)
    latest_map = {}
    for r in latest:
        latest_map[(r["sensor_key"], r["metric"])] = {
            "value": r["value"], "ts": r["ts"],
        }

    # build per-sensor view with online/offline
    now = datetime.now()
    stale_after = timedelta(minutes=10)
    sensor_view = []
    for s in sensors:
        key = s["sensor_key"]
        entry = {
            "key": key, "kind": s["kind"], "group": s["grp"],
            "label": s["label"], "detail": s["detail"],
            "metrics": {},
            "online": False,
        }
        # air sensors carry three metrics; others carry 'value'
        metrics = (["temp", "humidity", "pressure"]
                   if s["kind"] == "air" else ["value"])
        for m in metrics:
            hit = latest_map.get((key, m))
            if hit:
                entry["metrics"][m] = hit
                try:
                    age = now - datetime.fromisoformat(hit["ts"])
                    if age < stale_after:
                        entry["online"] = True
                except ValueError:
                    pass
        sensor_view.append(entry)

    median, n_used = db.median_soil_moisture(band=SOIL_BAND)

    cfg = db.get_config()

    # Derive watering gate status from the DB (the valve controller enforces
    # the same logic; this mirrors it for display without touching hardware).
    from datetime import timedelta as _td
    cfg_lockout = float(cfg.get("soak_lockout_minutes") or 30)
    cfg_cap = int(float(cfg.get("daily_pulse_cap") or 2))
    last_water = db.last_watering_time()
    lockout_remaining = 0
    if last_water:
        try:
            lw = datetime.fromisoformat(last_water)
            rem = (lw + _td(minutes=cfg_lockout)) - now
            lockout_remaining = max(0, int(rem.total_seconds()))
        except ValueError:
            pass

    threshold = cfg.get("threshold_pct", "")
    status = {
        "now": now.isoformat(timespec="seconds"),
        "sensors": sensor_view,
        "median_soil": median,
        "median_n": n_used,
        "config": {
            "auto_water_enabled": cfg.get("auto_water_enabled", "0") == "1",
            "threshold_pct": float(threshold) if threshold not in (None, "")
            else None,
            "max_pulse_seconds": cfg.get("max_pulse_seconds"),
            "soak_lockout_minutes": cfg.get("soak_lockout_minutes"),
            "daily_pulse_cap": cfg.get("daily_pulse_cap"),
            "frost_alert_f": cfg.get("frost_alert_f"),
        },
        "watering": {
            "last": db.last_watering_time(),
            "pulses_today": db.pulses_today(),
            "daily_cap": cfg_cap,
            "lockout_remaining_s": lockout_remaining,
            "recent": db.recent_watering(limit=10),
        },
    }
    return jsonify(status)


@app.route("/api/history/<sensor_key>")
@app.route("/api/history/<sensor_key>/<metric>")
def api_history(sensor_key, metric="value"):
    hours = 24
    return jsonify({
        "sensor_key": sensor_key,
        "metric": metric,
        "hours": hours,
        "points": db.history(sensor_key, metric=metric, hours=hours),
    })


# Which planter maps to which soil/temp key and which bed (for air/light).
# Bed B is mirror-planted, so the keys cross over.
PLANTERS = {
    "sanandreas_a": {"soil": "soil0", "temp": "temp0", "bed": "A",
                     "label": "San Andreas", "bed_air": "bme0", "bed_light": "lux0"},
    "sequoia_a":    {"soil": "soil1", "temp": "temp1", "bed": "A",
                     "label": "Sequoia", "bed_air": "bme0", "bed_light": "lux0"},
    "albion_a":     {"soil": "soil2", "temp": "temp2", "bed": "A",
                     "label": "Albion", "bed_air": "bme0", "bed_light": "lux0"},
    "albion_b":     {"soil": "soil3", "temp": "temp3", "bed": "B",
                     "label": "Albion", "bed_air": "bme1", "bed_light": "lux1"},
    "sequoia_b":    {"soil": "soil4", "temp": "temp4", "bed": "B",
                     "label": "Sequoia", "bed_air": "bme1", "bed_light": "lux1"},
    "sanandreas_b": {"soil": "soil5", "temp": "temp5", "bed": "B",
                     "label": "San Andreas", "bed_air": "bme1", "bed_light": "lux1"},
}

RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}

# Per-bed shared sensors (ambient). One air + one light sensor per bed.
BEDS = {
    "A": {"label": "Bed A", "air": "bme0", "light": "lux0",
          "varieties": [
              {"id": "sanandreas_a", "label": "San Andreas",
               "soil": "soil0", "temp": "temp0"},
              {"id": "sequoia_a", "label": "Sequoia",
               "soil": "soil1", "temp": "temp1"},
              {"id": "albion_a", "label": "Albion",
               "soil": "soil2", "temp": "temp2"},
          ]},
    "B": {"label": "Bed B", "air": "bme1", "light": "lux1",
          "varieties": [
              {"id": "sanandreas_b", "label": "San Andreas",
               "soil": "soil3", "temp": "temp3"},
              {"id": "sequoia_b", "label": "Sequoia",
               "soil": "soil4", "temp": "temp4"},
              {"id": "albion_b", "label": "Albion",
               "soil": "soil5", "temp": "temp5"},
          ]},
}


@app.route("/api/bed/<bed_id>/<range_key>")
def api_bed(bed_id, range_key):
    """One bed's shared ambient series (air temp/humidity/pressure + light),
    fetched once per bed rather than duplicated across its planters."""
    bed = BEDS.get(bed_id.upper())
    if not bed:
        return jsonify({"error": "unknown bed"}), 404
    hours = RANGE_HOURS.get(range_key, 24)

    def series(key, metric="value"):
        return db.history_downsampled(key, metric=metric, hours=hours)

    return jsonify({
        "bed": bed_id.upper(),
        "label": bed["label"],
        "hours": hours,
        "pressure_tendency": db.pressure_tendency(bed["air"]),
        "series": {
            "air_temp": series(bed["air"], "temp"),
            "humidity": series(bed["air"], "humidity"),
            "pressure": series(bed["air"], "pressure"),
            "light": series(bed["light"]),
        },
    })


@app.route("/api/variety/<variety_id>/<range_key>")
def api_variety(variety_id, range_key):
    """One variety's own series: moisture + soil temp only."""
    match = None
    for bed in BEDS.values():
        for v in bed["varieties"]:
            if v["id"] == variety_id:
                match = v
                break
    if not match:
        return jsonify({"error": "unknown variety"}), 404
    hours = RANGE_HOURS.get(range_key, 24)

    def series(key, metric="value"):
        return db.history_downsampled(key, metric=metric, hours=hours)

    return jsonify({
        "variety": variety_id,
        "label": match["label"],
        "hours": hours,
        "series": {
            "moisture": series(match["soil"]),
            "soil_temp": series(match["temp"]),
        },
    })


@app.route("/api/planter/<planter_id>/<range_key>")
def api_planter(planter_id, range_key):
    """All series for one planter's chart, in one request. Moisture and soil
    temp are per-planter; air (temp/humidity/pressure) and light are the
    shared bed values, labeled as bed context."""
    p = PLANTERS.get(planter_id)
    if not p:
        return jsonify({"error": "unknown planter"}), 404
    hours = RANGE_HOURS.get(range_key, 24)

    def series(key, metric="value"):
        return db.history_downsampled(key, metric=metric, hours=hours)

    return jsonify({
        "planter": planter_id,
        "label": p["label"],
        "bed": p["bed"],
        "hours": hours,
        "series": {
            "moisture": series(p["soil"]),
            "soil_temp": series(p["temp"]),
            "air_temp": series(p["bed_air"], "temp"),
            "humidity": series(p["bed_air"], "humidity"),
            "pressure": series(p["bed_air"], "pressure"),
            "light": series(p["bed_light"]),
        },
    })


@app.route("/api/health")
def api_health():
    """Cheap liveness check for monitoring."""
    try:
        n = len(db.get_sensors())
        return jsonify({"ok": True, "sensors": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---- control endpoints (write to commands/config; logger acts) ----
# These are the only write paths. They never touch hardware; they enqueue a
# command or update config, and the logger (the hardware owner) acts on it.

from flask import request


@app.route("/api/water", methods=["POST"])
def api_water():
    """Enqueue a manual watering pulse. The logger applies the same 30s cap
    and soak lockout as auto-water, so this can't override safety."""
    db.enqueue_command("water_now")
    return jsonify({"ok": True, "queued": "water_now"})


@app.route("/api/config", methods=["POST"])
def api_config():
    """Update editable config. Accepts a JSON object of key/value pairs, but
    only whitelisted keys are honored."""
    allowed = {
        "auto_water_enabled", "threshold_pct", "max_pulse_seconds",
        "soak_lockout_minutes", "daily_pulse_cap", "frost_alert_f",
        "weather_enabled", "rain_skip_inches", "rain_skip_hours",
        "soil_heat_warn_f",
    }
    data = request.get_json(silent=True) or {}
    updated = {}
    for k, v in data.items():
        if k not in allowed:
            continue
        # basic validation per key
        if k == "auto_water_enabled":
            v = "1" if str(v) in ("1", "true", "True", "on") else "0"
        elif k == "weather_enabled":
            v = "1" if str(v) in ("1", "true", "True", "on") else "0"
        else:
            try:
                fv = float(v)
            except (ValueError, TypeError):
                continue
            # sanity clamps so a fat-fingered value can't do harm
            if k == "threshold_pct":
                fv = max(0.0, min(100.0, fv))
            if k == "max_pulse_seconds":
                fv = max(1.0, min(120.0, fv))   # never allow a huge pulse
            if k == "soak_lockout_minutes":
                fv = max(1.0, min(720.0, fv))
            if k == "daily_pulse_cap":
                fv = max(0.0, min(48.0, fv))
            if k == "rain_skip_inches":
                fv = max(0.0, min(5.0, fv))
            if k == "rain_skip_hours":
                fv = max(1.0, min(72.0, fv))
            if k in ("frost_alert_f", "soil_heat_warn_f"):
                fv = max(20.0, min(120.0, fv))
            v = str(int(fv)) if fv == int(fv) else str(fv)
        db.set_config_value(k, v)
        updated[k] = v
    return jsonify({"ok": True, "updated": updated})


@app.route("/api/reboot", methods=["POST"])
def api_reboot():
    """Enqueue a reboot. The logger (valve owner) picks it up, closes the
    valve, then reboots. The web app never reboots directly, so the valve is
    guaranteed closed first."""
    db.enqueue_command("reboot")
    return jsonify({"ok": True, "queued": "reboot"})


@app.route("/api/summaries")
def api_summaries():
    """Records and summaries over existing data. Read-only."""
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    conn.row_factory = sqlite3.Row

    def q1(sql, args=()):
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    out = {}
    try:
        out["wettest"] = q1(
            "SELECT sensor_key, value, ts FROM readings "
            "WHERE metric='value' AND sensor_key LIKE 'soil%' "
            "AND ts >= datetime('now','-7 days','localtime') "
            "AND value IS NOT NULL ORDER BY value DESC LIMIT 1;")
        out["driest"] = q1(
            "SELECT sensor_key, value, ts FROM readings "
            "WHERE metric='value' AND sensor_key LIKE 'soil%' "
            "AND ts >= datetime('now','-7 days','localtime') "
            "AND value IS NOT NULL ORDER BY value ASC LIMIT 1;")
        out["hottest_soil"] = q1(
            "SELECT sensor_key, value, ts FROM readings "
            "WHERE metric='value' AND sensor_key LIKE 'temp%' "
            "AND ts >= datetime('now','-7 days','localtime') "
            "AND value IS NOT NULL ORDER BY value DESC LIMIT 1;")
        out["watering_week"] = q1(
            "SELECT COUNT(*) AS n, COALESCE(SUM(duration_seconds),0) AS secs "
            "FROM watering_events "
            "WHERE ts_start >= datetime('now','-7 days','localtime');")
        out["watering_today"] = q1(
            "SELECT COUNT(*) AS n FROM watering_events "
            "WHERE ts_start >= datetime('now','start of day','localtime');")
        rows = conn.execute(
            "SELECT s.label AS label, s.grp AS grp, r.value AS value "
            "FROM sensors s JOIN ("
            "  SELECT sensor_key, value, MAX(id) AS mid FROM readings "
            "  WHERE metric='value' GROUP BY sensor_key) r "
            "  ON r.sensor_key = s.sensor_key "
            "WHERE s.kind='soil_moisture' ORDER BY s.sensor_key;").fetchall()
        out["by_variety"] = [dict(x) for x in rows]
    finally:
        conn.close()

    recent = db.recent_watering(limit=5)
    eff = []
    for e in recent:
        samples = db.effectiveness_for(e["id"])
        if samples:
            eff.append({"event_id": e["id"], "ts_start": e["ts_start"],
                        "before": e.get("median_at_trigger"),
                        "samples": samples})
    out["effectiveness"] = eff

    try:
        import opengardener_weather as weather
        hours = int(float(db.get_config_value("rain_skip_hours") or 12))
        inches, prob, summary = weather.rain_outlook(hours)
        out["weather"] = {"inches": inches, "prob": prob, "summary": summary,
                          "enabled": db.get_config_value("weather_enabled","0")=="1"}
    except Exception as e:
        out["weather"] = {"error": str(e)}

    return jsonify(out)


@app.route("/api/forecast")
def api_forecast():
    """Daily forecast periods for the week-ahead display, plus the rain-skip
    outlook so the UI can show whether watering would be held."""
    try:
        import opengardener_weather as weather
        periods = weather.daily_forecast() or []
        hours = int(float(db.get_config_value("rain_skip_hours") or 12))
        inches, prob, summary = weather.rain_outlook(hours)
        skip, reason = weather.should_skip()
        # Trim each period to what the UI needs.
        slim = []
        for p in periods:
            slim.append({
                "name": p.get("name"),
                "isDaytime": p.get("isDaytime"),
                "temp": p.get("temperature"),
                "tempUnit": p.get("temperatureUnit"),
                "short": p.get("shortForecast"),
                "detailed": p.get("detailedForecast"),
                "windSpeed": p.get("windSpeed"),
                "windDir": p.get("windDirection"),
                "precipProb": (p.get("probabilityOfPrecipitation") or {}).get("value"),
            })
        return jsonify({
            "periods": slim,
            "rain_outlook": {"inches": inches, "prob": prob, "summary": summary},
            "skip": {"active": skip, "reason": reason,
                     "enabled": db.get_config_value("weather_enabled","0")=="1"},
        })
    except Exception as e:
        return jsonify({"error": str(e), "periods": []}), 200


@app.route("/api/export.csv")
def api_export_csv():
    """Stream the full reading history as CSV. Auth is enforced at nginx."""
    import csv
    import io
    import sqlite3
    from flask import Response

    def generate():
        conn = sqlite3.connect(db.DB_PATH)
        try:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["ts", "sensor_key", "metric", "value"])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
            cur = conn.execute(
                "SELECT ts, sensor_key, metric, value FROM readings ORDER BY id;")
            n = 0
            for row in cur:
                w.writerow(row)
                n += 1
                if n % 500 == 0:
                    yield buf.getvalue()
                    buf.seek(0); buf.truncate(0)
            yield buf.getvalue()
        finally:
            conn.close()

    from datetime import datetime as _dt
    fname = f"opengardener_readings_{_dt.now():%Y%m%d}.csv"
    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.route("/api/hwstats")
def api_hwstats():
    """Pi hardware + network stats. Read-only, best-effort; any field that
    can't be read comes back null rather than failing the whole response."""
    import subprocess
    import shutil

    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=3).stdout.strip()
        except Exception:
            return None

    stats = {}

    # CPU temperature
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            stats["cpu_temp_c"] = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        stats["cpu_temp_c"] = None

    # Load average + core count
    try:
        stats["loadavg"] = list(os.getloadavg())
        stats["cores"] = os.cpu_count()
    except Exception:
        stats["loadavg"] = None

    # Memory from /proc/meminfo
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k] = int(v.strip().split()[0])  # kB
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        stats["mem_total_mb"] = round(total / 1024)
        stats["mem_used_mb"] = round((total - avail) / 1024)
        stats["mem_pct"] = round((total - avail) / total * 100) if total else None
    except Exception:
        stats["mem_total_mb"] = None

    # Disk on root
    try:
        du = shutil.disk_usage("/")
        stats["disk_total_gb"] = round(du.total / 1e9, 1)
        stats["disk_used_gb"] = round(du.used / 1e9, 1)
        stats["disk_pct"] = round(du.used / du.total * 100)
    except Exception:
        stats["disk_total_gb"] = None

    # Uptime
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        stats["uptime_s"] = int(up)
    except Exception:
        stats["uptime_s"] = None

    # Throttle status (undervoltage / thermal). 0x0 is healthy.
    thr = run(["vcgencmd", "get_throttled"])
    if thr and "=" in thr:
        code = thr.split("=")[1]
        stats["throttled"] = code
        try:
            bits = int(code, 16)
            stats["throttle_flags"] = {
                "undervoltage_now": bool(bits & 0x1),
                "throttled_now": bool(bits & 0x4),
                "undervoltage_occurred": bool(bits & 0x10000),
                "throttled_occurred": bool(bits & 0x40000),
            }
        except ValueError:
            stats["throttle_flags"] = None
    else:
        stats["throttled"] = None

    # Core voltage + clock
    volt = run(["vcgencmd", "measure_volts", "core"])
    stats["core_volt"] = volt.split("=")[1] if volt and "=" in volt else None
    clock = run(["vcgencmd", "measure_clock", "arm"])
    if clock and "=" in clock:
        try:
            stats["arm_mhz"] = round(int(clock.split("=")[1]) / 1e6)
        except ValueError:
            stats["arm_mhz"] = None

    # Network: hostname, IP, wifi signal
    stats["hostname"] = run(["hostname"])
    ip = run(["hostname", "-I"])
    stats["ip"] = ip.split()[0] if ip else None

    # WiFi signal from iwconfig / iw
    wifi = run(["iwconfig", "wlan0"])
    stats["wifi"] = None
    if wifi:
        import re
        ssid = re.search(r'ESSID:"([^"]*)"', wifi)
        sig = re.search(r"Signal level=(-?\d+)", wifi)
        quality = re.search(r"Link Quality=(\d+)/(\d+)", wifi)
        stats["wifi"] = {
            "ssid": ssid.group(1) if ssid else None,
            "signal_dbm": int(sig.group(1)) if sig else None,
            "quality_pct": (round(int(quality.group(1)) /
                                  int(quality.group(2)) * 100)
                            if quality else None),
        }

    return jsonify(stats)


if __name__ == "__main__":
    # Dev server only. Production uses gunicorn via systemd.
    app.run(host="0.0.0.0", port=5000, debug=False)
