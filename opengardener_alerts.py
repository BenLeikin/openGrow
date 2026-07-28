#!/usr/bin/env python3
"""openGardener alert layer.

Decides WHEN to send Discord alerts. The logger calls check() once per cycle.

Debounce model: each condition fires ONCE when it enters the bad state, stays
quiet while it persists, and sends an all-clear ONCE when it recovers. State is
persisted to a small JSON file so a logger restart doesn't re-fire everything.

Plus a once-daily health heartbeat, so silence is never ambiguous: if the Pi
died you stop getting the daily "all good" and know something's wrong.

Conditions:
  - frost:        air temp <= frost_alert_f (config) on any bed sensor
  - undervoltage: Pi throttle flags show under-voltage now
  - sensor_offline: a sensor that was reporting stops (per-key)
  - watering_fail: a watering pulse aborted abnormally
The logger reports service/watering failures by calling note_event().
"""

import json
import os
from datetime import datetime, date

import discord_alert
import opengardener_db as db

STATE_FILE = os.path.expanduser("~/.opengardener_alerts.json")
SOIL_BAND = (-5.0, 105.0)


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"alert state save failed: {e}")


def _fire(state, key, active, title, message, level, fields=None):
    """Edge-triggered send. Fires on False->True (problem) and True->False
    (recovery). Returns True if a message was sent."""
    was = state.get(key, {}).get("active", False)
    if active and not was:
        discord_alert.send(title, message, level=level, fields=fields)
        state[key] = {"active": True, "since": datetime.now().isoformat(timespec="seconds")}
        return True
    if not active and was:
        discord_alert.send(f"Resolved: {title}",
                           "Condition cleared.", level="good")
        state[key] = {"active": False}
        return True
    return False


def _hwstats():
    """Minimal local hardware read for the undervoltage check (the logger runs
    on the Pi, so read directly rather than hitting the web API)."""
    import subprocess
    try:
        out = subprocess.run(["vcgencmd", "get_throttled"],
                             capture_output=True, text=True, timeout=3).stdout
        code = out.strip().split("=")[1]
        bits = int(code, 16)
        return {"undervoltage_now": bool(bits & 0x1)}
    except Exception:
        return {"undervoltage_now": False}


def check(logger=None, known_sensor_keys=None):
    """Run all alert conditions once. Call each logger cycle."""
    log = logger or print
    state = _load_state()
    cfg = db.get_config()

    # --- frost ---
    frost_f = float(cfg.get("frost_alert_f") or 33)
    latest = db.latest_readings()
    air_temps = [r["value"] for r in latest
                 if r["metric"] == "temp" and r["sensor_key"].startswith("bme")
                 and r["value"] is not None]
    coldest = min(air_temps) if air_temps else None
    frost_active = coldest is not None and coldest <= frost_f
    if _fire(state, "frost", frost_active,
             "Frost warning",
             f"Air temperature {coldest:.0f}\u00b0F, at/below {frost_f:.0f}\u00b0F. "
             f"Open blossoms are at risk." if coldest is not None else "",
             "problem"):
        log(f"alert: frost {'ON' if frost_active else 'clear'}")

    # --- undervoltage ---
    uv = _hwstats()["undervoltage_now"]
    if _fire(state, "undervoltage", uv,
             "Pi undervoltage",
             "The Pi is reporting under-voltage now. Check the power supply; "
             "sensor readings and the valve may be unreliable.",
             "problem"):
        log(f"alert: undervoltage {'ON' if uv else 'clear'}")

    # --- soil heat warning (watch-level, configurable) ---
    # Fires when any planter's root-zone temp exceeds the threshold. Default
    # 86F is the published strawberry root-stress reference; tune from observed
    # data once real planters have been watched through hot days.
    heat_f = float(cfg.get("soil_heat_warn_f") or 86)
    soil_temps = {r["sensor_key"]: r["value"] for r in latest
                  if r["metric"] == "value"
                  and r["sensor_key"].startswith("temp")
                  and r["value"] is not None}
    hot = {k: v for k, v in soil_temps.items() if v >= heat_f}
    heat_active = len(hot) > 0
    if heat_active:
        # name planters (map temp key -> label via sensors table)
        labels = {s["sensor_key"]: f"{s['label']} ({s['grp']})"
                  for s in db.get_sensors(kind="soil_temp")}
        hot_desc = ", ".join(f"{labels.get(k, k)} {v:.0f}\u00b0F"
                             for k, v in sorted(hot.items()))
        msg = (f"Root zone above {heat_f:.0f}\u00b0F: {hot_desc}. "
               f"Heat can stall fruit set and speed ripening; consider shade "
               f"or a cooling watering pulse.")
    else:
        msg = ""
    if _fire(state, "soil_heat", heat_active,
             "Soil heat warning", msg, "watch"):
        log(f"alert: soil_heat {'ON' if heat_active else 'clear'}")

    # --- sensor offline ---
    # A sensor is "offline" if it was reporting recently but its latest reading
    # is older than ~10 min. Only track keys we've actually seen report.
    now = datetime.now()
    reporting = {}
    for r in latest:
        if r["metric"] != "value":
            continue
        try:
            age = (now - datetime.fromisoformat(r["ts"])).total_seconds()
        except Exception:
            continue
        reporting[r["sensor_key"]] = age

    # consider only soil/temp sensors that have ever reported
    seen = state.get("_seen_sensors", [])
    for key, age in reporting.items():
        if key not in seen:
            seen.append(key)
    state["_seen_sensors"] = seen

    offline = [k for k in seen
               if k not in reporting or reporting.get(k, 1e9) > 600]
    offline_active = len(offline) > 0
    if _fire(state, "sensor_offline", offline_active,
             "Sensor offline",
             f"Not reporting: {', '.join(sorted(offline))}. "
             f"Possible wiring or bus issue." if offline else "",
             "watch"):
        log(f"alert: sensor_offline {'ON' if offline_active else 'clear'} "
            f"({offline})")

    # --- daily heartbeat ---
    today = date.today().isoformat()
    if state.get("_heartbeat_date") != today:
        n_online = len(reporting)
        n_total = len(db.get_sensors())
        med, n_used = db.median_soil_moisture(band=SOIL_BAND)
        med_s = f"{med:.0f}%" if med is not None else "n/a"
        auto = "on" if cfg.get("auto_water_enabled") == "1" else "off"
        discord_alert.send(
            "openGardener daily report",
            f"{n_online} of {n_total} sensors reporting. "
            f"Median soil {med_s}. Auto-water {auto}. "
            f"{db.pulses_today()} pulses today.",
            level="good",
        )
        state["_heartbeat_date"] = today
        log("alert: daily heartbeat sent")

    _save_state(state)


def note_event(title, message, level="problem", logger=None):
    """Direct one-shot alert for discrete events the logger detects, e.g. a
    watering pulse aborting abnormally. Not debounced -- caller decides."""
    (logger or print)(f"alert event: {title}")
    discord_alert.send(title, message, level=level)


if __name__ == "__main__":
    # Manual run: evaluate once against current DB state.
    check()
    print("alert check complete")
