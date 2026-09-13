#!/usr/bin/env python3
"""
openGardener wireless node ingest.

Receives readings from battery-powered ESP32 sensor nodes and writes them into
the same `readings` table the wired logger uses, via opengardener_db. Once a
reading lands here, the dashboard, alerts, auto-water and AI report all treat
it identically to a wired reading -- that is the whole point.

Design rules:
  - Nodes send RAW values only (ADC volts, degrees C, lux). All calibration,
    inversion and temperature compensation stays on the Pi so the hard-won
    curve work is reused rather than reimplemented in firmware.
  - Nodes do NOT know their sensor keys. They send a node_id and channel
    numbers; the mapping to soil3/temp3/etc lives here. Reassigning a planter
    is a config edit, not a reflash.
  - This module never touches hardware. The logger still owns the valve.

Wire into opengardener_web.py with two lines:

    from opengardener_ingest import ingest_bp
    app.register_blueprint(ingest_bp)

Auth: shared secret in ~/.ingest_token (mode 600, gitignored alongside
.anthropic_key and .discord_webhook). Nodes send it as a Bearer token.
Generate one with:  openssl rand -hex 32 > ~/.ingest_token && chmod 600 ~/.ingest_token
"""

import hmac
import json
import os
from datetime import datetime

from flask import Blueprint, jsonify, request

import opengardener_db as db

ingest_bp = Blueprint("ingest", __name__)

TOKEN_PATH = os.path.expanduser("~/.ingest_token")

# Reject values outside these ranges outright. A node reporting nonsense
# should produce a gap in the record, not a poisoned median. Ranges are
# deliberately wide -- this catches wiring faults and pinned rails, not
# unusual weather.
RAW_LIMITS = {
    "soil_volts": (0.0, 3.4),      # ADS at 3.3V supply; above this is a fault
    "temp_c": (-30.0, 80.0),       # DS18B20 returns 85.0 on a failed convert
    "lux": (0.0, 200000.0),
    "humidity": (0.0, 100.0),
    "pressure": (800.0, 1100.0),   # hPa
    "battery_v": (2.5, 4.35),
}

# Which node owns which planters, and which ADS channel / probe serial maps to
# which sensor key. Node ids are arbitrary strings the firmware sends.
#
# DS18B20 serials here are in MicroPython's raw 8-byte hex form (as returned by
# ds18x20.scan()), which is NOT the same string Linux w1 reports. Populate
# these from an actual scan on the node, not from the Pi's device names.
NODE_MAP = {
    "bedb": {
        "label": "Bed B node",
        "soil": {                      # ADS1115 single-ended channel -> key
            0: "soil3",                # San Andreas B
            1: "soil4",                # Sequoia B
            2: "soil5",                # Albion B
        },
        "temp": {                      # DS18B20 rom hex -> key
            "28e41123000000a2": "temp3",   # planter 1, San Andreas B
            "28b2096c00000006": "temp4",   # planter 2, Sequoia B
            "28a71822000000f2": "temp5",   # planter 3, Albion B
        },
        # Each node carries its own air and light sensors, writing to its own
        # keys. They must NOT share bme0/lux0: latest_readings() takes MAX(id)
        # per key, so two nodes writing one key would alternate between two
        # physically different sensors and pressure_tendency() would read that
        # alternation as barometric change.
        "air": "bme1",
        "light": "lux1",
    },
    "beda": {
        "label": "Bed A node",
        "soil": {
            0: "soil0",                # San Andreas A
            1: "soil1",                # Sequoia A
            2: "soil2",                # Albion A
        },
        "temp": {
            "28fc9c2100000078": "temp0",
            "28bc11cb00000078": "temp1",
            "28410eca000000dc": "temp2",
        },
        # Do not enable these until the wired logger has stopped writing
        # bme0/lux0 (set LOCAL_SENSORS = False in opengardener_logger.py), or
        # the logger and this node will interleave into the same keys.
        "air": "bme0",
        "light": "lux0",
    },
}


def _c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def _log(msg):
    """Print to stdout, which gunicorn hands to journald. Read with
    `journalctl -u opengardener-web`. Deliberately not a file, so it rotates
    with everything else and needs no cleanup."""
    print(msg)


def _load_token():
    try:
        with open(TOKEN_PATH) as f:
            return f.read().strip()
    except OSError:
        return None


def _authorized(req):
    expected = _load_token()
    if not expected:
        return False
    hdr = req.headers.get("Authorization", "")
    if not hdr.startswith("Bearer "):
        return False
    return hmac.compare_digest(hdr[7:].strip(), expected)


def _in_range(kind, value):
    lo, hi = RAW_LIMITS[kind]
    return isinstance(value, (int, float)) and lo <= float(value) <= hi


CALIBRATION_PATH = os.path.expanduser("~/garden_calibration.json")

# Temperature-compensation coefficients above this magnitude are treated as
# overfit and ignored (the reading still gets its calibration curve, just no
# temp correction). Derived from a clean no-watering hot/cool window, a real
# coefficient for these probes lands around 0.05-0.3 %/degF. Negative values
# are rejected outright: capacitive probes read WETTER as they heat, so the
# correction must subtract with rising temperature. A negative coefficient
# amplifies the error instead of removing it, which means it was fit to noise.
TEMP_COMP_MAX = 0.3


def _load_calibration():
    """Read the calibration file on each request rather than caching it, so
    editing the file takes effect without a service restart. It is a few
    hundred bytes and reads are cheap next to the SQLite writes."""
    try:
        with open(CALIBRATION_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _soil_percent(sensor_key, volts, soil_temp_c=None):
    """Convert a raw ADS voltage to percent of field capacity.

    Mirrors the wired logger's conversion so wireless and wired planters share
    one scale. Returns None when the reading cannot be trusted, and the caller
    drops it -- an uncalibrated number written into a soil key would be read as
    a percentage by median_soil_moisture() and could trigger watering.

    IMPORTANT: the curves in garden_calibration.json were derived with the
    probes on the Pi's 5V rail. A node running them at 3.3V produces a
    compressed swing, so those dry/wet voltages do NOT transfer. Bed B entries
    must be re-derived in place at 3.3V before this returns meaningful values.
    """
    cal = _load_calibration().get(sensor_key.replace("soil", ""))
    if not cal:
        return None

    dry, wet = cal.get("dry"), cal.get("wet")
    if dry is None or wet is None or dry <= wet:
        # HW-390 is inverted: dry must be the HIGHER voltage. If it isn't,
        # the entry was calibrated with the two points swapped and would
        # produce backwards readings. Refuse rather than invert silently.
        return None

    pct = (dry - volts) / (dry - wet) * 100.0

    tc = cal.get("temp_comp") or {}
    coeff = tc.get("coeff") or 0.0
    if soil_temp_c is not None and 0.0 < coeff <= TEMP_COMP_MAX:
        temp_f = soil_temp_c * 9.0 / 5.0 + 32.0
        pct -= coeff * (temp_f - float(tc.get("ref_f", 70.0)))

    # Clamp to the band the dashboard and watering logic already treat as
    # plausible. Beyond this the probe is disconnected or shorted, and a wild
    # value is worse than no value.
    if not (-5.0 <= pct <= 105.0):
        return None
    return pct


def _pending_node_command(node_id):
    """Return a command for this node, or None.

    Commands are stored in the config table under `nodecmd_<node_id>` and are
    cleared as soon as they are handed out, so each is delivered exactly once.
    The node cannot be reached directly -- it is in deep sleep almost all the
    time -- so this piggybacks on the check-in it is already making. Worst-case
    latency is one sleep interval.

    Set one from the Pi:
      sqlite3 ~/opengardener.db "INSERT INTO config (key,value)
        VALUES ('nodecmd_bedb','stay_awake:300')
        ON CONFLICT(key) DO UPDATE SET value=excluded.value;"
    """
    key = f"nodecmd_{node_id}"
    cmd = db.get_config_value(key)
    if not cmd:
        return None
    db.set_config_value(key, "")      # deliver once
    _log(f"node {node_id}: delivering command {cmd!r}")
    return cmd


@ingest_bp.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Accept one node's batch of raw readings.

    Expected JSON:
      {
        "node_id": "bedb",
        "soil":    {"0": 2.412, "1": 2.388, "2": 2.501},   # ADS volts
        "temp":    {"28a71822000000f2": 21.5},             # rom hex -> deg C
        "air":     {"temp": 21.4, "humidity": 48.2, "pressure": 1013.2},
        "lux":     13550.0,
        "battery": 3.92
      }

    Every field except node_id is optional, so a node with a dead sensor still
    reports what it can. Unknown or out-of-range values are counted in
    "rejected" and left out of the database entirely.
    """
    if not _authorized(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    node_id = data.get("node_id")
    node = NODE_MAP.get(node_id)
    if not node:
        return jsonify({"ok": False, "error": f"unknown node_id {node_id!r}"}), 400

    ts = datetime.now().isoformat(timespec="seconds")
    _log(f"node {node_id} payload: {data}")
    rows = []
    rejected = []

    # --- soil temperature first: needed for moisture temp-compensation ---
    # Nodes send raw Celsius; the wired logger writes Fahrenheit via
    # log_cycle(). The database holds one scale, and the dashboard, frost
    # alert and soil-heat alert all assume Fahrenheit -- so convert here.
    # Storing Celsius would show 25 where the wired beds show 77, and would
    # fire the frost alert on a warm day.
    temps_c = {}
    for rom, value in (data.get("temp") or {}).items():
        key = node["temp"].get(rom)
        if not key:
            rejected.append(f"temp:{rom} unmapped")
            continue
        if not _in_range("temp_c", value):
            rejected.append(f"temp:{rom} out of range ({value})")
            continue
        temps_c[key] = float(value)
        rows.append((key, round(_c_to_f(float(value)), 3), "value"))

    # --- soil moisture: raw volts -> percent ---
    # The raw voltage is stored alongside the percentage under the 'volts'
    # metric. Two reasons: a percentage computed against a stale curve cannot
    # be recovered afterwards, and capturing a calibration point needs the
    # voltage the node actually sent, not a derived number. Storing it means
    # the dry/wet points can be captured from the dashboard rather than by
    # reading them off the node over serial.
    for ch, volts in (data.get("soil") or {}).items():
        try:
            key = node["soil"].get(int(ch))
        except (TypeError, ValueError):
            key = None
        if not key:
            rejected.append(f"soil:{ch} unmapped")
            continue
        if not _in_range("soil_volts", volts):
            rejected.append(f"soil:{ch} out of range ({volts})")
            continue
        rows.append((key, round(float(volts), 4), "volts"))
        paired_temp = temps_c.get(key.replace("soil", "temp"))
        pct = _soil_percent(key, float(volts), paired_temp)
        if pct is None:
            rejected.append(f"soil:{ch} uncalibrated, dropped")
            continue
        rows.append((key, round(pct, 2), "value"))

    # --- garden-wide air ---
    air_key = node.get("air")
    air = data.get("air") or {}
    if air_key and air:
        for metric, kind in (("temp", "temp_c"),
                             ("humidity", "humidity"),
                             ("pressure", "pressure")):
            if metric not in air:
                continue
            if not _in_range(kind, air[metric]):
                rejected.append(f"air:{metric} out of range ({air[metric]})")
                continue
            v = float(air[metric])
            if metric == "temp":
                v = round(_c_to_f(v), 2)   # log_cycle() writes temp_f
            rows.append((air_key, v, metric))

    # --- garden-wide light ---
    light_key = node.get("light")
    if light_key and data.get("lux") is not None:
        if _in_range("lux", data["lux"]):
            rows.append((light_key, float(data["lux"]), "value"))
        else:
            rejected.append(f"lux out of range ({data['lux']})")

    # --- node battery, keyed per node so several can coexist ---
    if data.get("battery") is not None:
        if _in_range("battery_v", data["battery"]):
            rows.append((f"node_{node_id}", float(data["battery"]), "battery"))
        else:
            rejected.append(f"battery out of range ({data['battery']})")

    # --- node diagnostics ---
    # RSSI is worth trending: it tells you whether an enclosure position is
    # marginal, and it degrades as foliage fills in around a node over a
    # season. A node that starts retrying its association is a node whose
    # battery life is quietly halving.
    if data.get("rssi") is not None:
        try:
            rssi = int(data["rssi"])
            if -120 <= rssi <= 0:
                rows.append((f"node_{node_id}", rssi, "rssi"))
        except (TypeError, ValueError):
            pass

    # Sensor faults reported by the node itself. Stored as a count so it can
    # be charted and alerted on; the strings go to the log, since the readings
    # table holds numbers only.
    node_errors = data.get("errors") or []
    if isinstance(node_errors, list):
        rows.append((f"node_{node_id}", float(len(node_errors)), "errors"))
        for msg in node_errors[:8]:
            _log(f"node {node_id}: {msg}")

    # 4 = deep-sleep wake (the normal case). Anything else on a settled node
    # means it rebooted for another reason -- watchdog, brownout, power cycle
    # -- which is worth seeing in the log.
    rc = data.get("reset_cause")
    if rc is not None and rc != 4:
        _log(f"node {node_id}: unexpected reset_cause={rc}")

    # readings.sensor_key is a foreign key onto sensors.sensor_key, and
    # insert_readings() uses executemany -- so a single unregistered key would
    # abort the entire batch and lose every good reading with it. Filter
    # against the registry first so one bad key costs one reading.
    known = {s["sensor_key"] for s in db.get_sensors()}
    keep = []
    for r in rows:
        if r[0] in known:
            keep.append(r)
        else:
            rejected.append(f"{r[0]} not registered in sensors table")

    written = 0
    if keep:
        try:
            db.insert_readings(keep, ts=ts)
            written = len(keep)
        except Exception as e:
            # Never return a stack trace to a node. It can't act on one, and
            # the node should treat a failed POST as "retry next wake" rather
            # than as a reason to stay awake.
            return jsonify({"ok": False, "node_id": node_id,
                            "error": f"write failed: {e}"}), 500

    return jsonify({
        "ok": True,
        "node_id": node_id,
        "ts": ts,
        "written": written,
        "rejected": rejected,
        "command": _pending_node_command(node_id),
    })


@ingest_bp.route("/api/ingest/ping", methods=["GET"])
def api_ingest_ping():
    """Lets a node verify its token and reachability without writing data.
    Useful during bring-up and as a firmware self-test after deep sleep."""
    if not _authorized(request):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return jsonify({"ok": True, "nodes": sorted(NODE_MAP)})


# ---------------------------------------------------------------- calibration
# These endpoints are reached from the dashboard, so they are covered by the
# same nginx Basic auth as the rest of the UI rather than the node token.

def _latest_volts(sensor_key):
    """Most recent raw voltage reported for a soil sensor, with its
    timestamp. Returns (volts, ts) or (None, None)."""
    import sqlite3
    con = sqlite3.connect(db.DB_PATH, timeout=5.0)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT value, ts FROM readings WHERE sensor_key = ? "
            "AND metric = 'volts' ORDER BY id DESC LIMIT 1;",
            (sensor_key,),
        ).fetchone()
    finally:
        con.close()
    return (row["value"], row["ts"]) if row else (None, None)


@ingest_bp.route("/api/calibration", methods=["GET"])
def api_calibration():
    """Current calibration plus each sensor's latest raw voltage, so the UI
    can show what a capture would record before committing to it."""
    cal = _load_calibration()
    out = {}
    for key in [f"soil{i}" for i in range(6)]:
        idx = key.replace("soil", "")
        entry = cal.get(idx) or {}
        volts, ts = _latest_volts(key)
        dry, wet = entry.get("dry"), entry.get("wet")

        # What this voltage would read as under the current curve. Shown so a
        # nonsensical percentage makes the stale curve obvious.
        pct = None
        if volts is not None and dry is not None and wet is not None and dry > wet:
            pct = round((dry - volts) / (dry - wet) * 100.0, 1)

        out[key] = {
            "dry": dry,
            "wet": wet,
            "temp_comp": entry.get("temp_comp"),
            "latest_volts": volts,
            "latest_ts": ts,
            "would_read_pct": pct,
            "valid": bool(dry is not None and wet is not None and dry > wet),
        }
    return jsonify({"ok": True, "sensors": out, "path": CALIBRATION_PATH})


@ingest_bp.route("/api/calibration/capture", methods=["POST"])
def api_calibration_capture():
    """Record the latest raw voltage as this sensor's dry or wet point.

    Body: {"sensor_key": "soil3", "point": "wet"}

    Capture the wet point with the planter at field capacity -- watered to
    runoff, then drained about 30 minutes. Capturing while it is still
    draining sets 100% above field capacity, and every later reading then
    looks drier than it is.
    """
    data = request.get_json(silent=True) or {}
    key = data.get("sensor_key")
    point = data.get("point")

    if key not in [f"soil{i}" for i in range(6)]:
        return jsonify({"ok": False, "error": f"unknown sensor {key!r}"}), 400
    if point not in ("dry", "wet"):
        return jsonify({"ok": False, "error": "point must be 'dry' or 'wet'"}), 400

    volts, ts = _latest_volts(key)
    if volts is None:
        return jsonify({"ok": False,
                        "error": f"no raw voltage recorded for {key} yet"}), 400

    cal = _load_calibration()
    idx = key.replace("soil", "")
    entry = cal.setdefault(idx, {})
    entry[point] = round(float(volts), 4)
    entry.setdefault("temp_comp", {"coeff": 0.0, "ref_f": 70.0})

    # HW-390 is inverted: dry must be the HIGHER voltage. Refuse to write a
    # pair that would produce backwards readings -- several sensors on the
    # wired build were calibrated with the two points swapped, and it is not
    # obvious from the dashboard until the numbers move the wrong way.
    dry, wet = entry.get("dry"), entry.get("wet")
    if dry is not None and wet is not None and dry <= wet:
        return jsonify({
            "ok": False,
            "error": (f"dry ({dry}V) must be higher than wet ({wet}V) -- "
                      "the probe reads a higher voltage in dry soil. Check "
                      "which point you are capturing."),
            "captured": None,
        }), 400

    try:
        with open(CALIBRATION_PATH, "w") as f:
            json.dump(cal, f, indent=2, sort_keys=True)
    except OSError as e:
        return jsonify({"ok": False, "error": f"could not write: {e}"}), 500

    _log(f"calibration: {key} {point} = {volts}V (reading from {ts})")
    return jsonify({
        "ok": True,
        "sensor_key": key,
        "point": point,
        "volts": entry[point],
        "reading_ts": ts,
        "dry": dry,
        "wet": wet,
        "complete": bool(dry is not None and wet is not None),
    })


@ingest_bp.route("/api/calibration/temp_comp", methods=["POST"])
def api_calibration_temp_comp():
    """Set a temperature-compensation coefficient.

    Body: {"sensor_key": "soil3", "coeff": 0.12, "ref_f": 70}

    Capacitive probes read wetter as they heat, so the correction subtracts
    with rising temperature and the coefficient must be positive. A plausible
    value is 0.05-0.3 %/degF; anything larger was fit to noise rather than to
    the temperature effect. Zero disables compensation.
    """
    data = request.get_json(silent=True) or {}
    key = data.get("sensor_key")
    if key not in [f"soil{i}" for i in range(6)]:
        return jsonify({"ok": False, "error": f"unknown sensor {key!r}"}), 400
    try:
        coeff = float(data.get("coeff"))
        ref_f = float(data.get("ref_f", 70.0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "coeff must be a number"}), 400

    if coeff < 0:
        return jsonify({
            "ok": False,
            "error": ("coefficient must be positive -- a negative value adds "
                      "moisture as the probe heats, amplifying the error "
                      "instead of removing it"),
        }), 400

    warning = None
    if coeff > TEMP_COMP_MAX:
        warning = (f"{coeff} exceeds the plausible range (0.05-0.3 %/degF) "
                   "and will be ignored when readings are converted")

    cal = _load_calibration()
    idx = key.replace("soil", "")
    cal.setdefault(idx, {})["temp_comp"] = {"coeff": coeff, "ref_f": ref_f}
    try:
        with open(CALIBRATION_PATH, "w") as f:
            json.dump(cal, f, indent=2, sort_keys=True)
    except OSError as e:
        return jsonify({"ok": False, "error": f"could not write: {e}"}), 500

    _log(f"calibration: {key} temp_comp coeff={coeff} ref_f={ref_f}")
    return jsonify({"ok": True, "sensor_key": key,
                    "coeff": coeff, "ref_f": ref_f, "warning": warning})
