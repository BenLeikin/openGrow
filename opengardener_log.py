#!/usr/bin/env python3
"""
openGardener logging bridge.

Drop-in that takes the values your existing read loop already computes and
writes them to the database with the correct sensor keys and metrics. This
does NOT change how sensors are read; it only adds a database destination
alongside your current CSV.

Mapping (matches the sensor registry in opengardener_initdb.py):
  soil channel i -> key f"soil{i}", metric "value"  (calibrated percent)
  temp probe i   -> key f"temp{i}", metric "value"  (Fahrenheit)
  BME280 group   -> key "bme0"/"bme1", metrics "temp" (F), "humidity", "pressure"
  BH1750 group   -> key "lux0"/"lux1", metric "value"

Only pass what you have. Missing/None values are skipped, so during the
current 3-sensor bench phase you just pass the three soil percentages and
everything else stays offline on the dashboard.

Usage inside your log loop, once per cycle:

    from opengardener_log import log_cycle
    log_cycle(
        soil_pct={0: p0, 1: p1, 2: p2},      # channel -> percent
        soil_temp_f={},                      # channel -> F  (none yet)
        bme={},                              # 0/1 -> {"temp_f","humidity","pressure"}
        lux={},                              # 0/1 -> lux
    )

Temps are Fahrenheit. If your probes read Celsius, convert before calling
(c_to_f below is provided).
"""

from datetime import datetime

import opengardener_db as db


def c_to_f(c):
    return c * 9.0 / 5.0 + 32.0


def log_cycle(soil_pct=None, soil_temp_f=None, bme=None, lux=None, ts=None):
    """Write one cycle of readings. All args optional; None/absent are skipped.

    soil_pct:    dict channel(int) -> calibrated percent(float)
    soil_temp_f: dict channel(int) -> temperature in Fahrenheit(float)
    bme:         dict group(0|1) -> {"temp_f", "humidity", "pressure"}
    lux:         dict group(0|1) -> lux(float)
    """
    ts = ts or datetime.now().isoformat(timespec="seconds")
    rows = []  # (sensor_key, value, metric)

    if soil_pct:
        for ch, pct in soil_pct.items():
            if pct is not None:
                rows.append((f"soil{ch}", pct, "value"))

    if soil_temp_f:
        for ch, t in soil_temp_f.items():
            if t is not None:
                rows.append((f"temp{ch}", t, "value"))

    if bme:
        for grp, d in bme.items():
            if not d:
                continue
            if d.get("temp_f") is not None:
                rows.append((f"bme{grp}", d["temp_f"], "temp"))
            if d.get("humidity") is not None:
                rows.append((f"bme{grp}", d["humidity"], "humidity"))
            if d.get("pressure") is not None:
                rows.append((f"bme{grp}", d["pressure"], "pressure"))

    if lux:
        for grp, v in lux.items():
            if v is not None:
                rows.append((f"lux{grp}", v, "value"))

    if rows:
        db.insert_readings(rows, ts=ts)
    return len(rows)


if __name__ == "__main__":
    # Smoke test: write one fake cycle of just the three live soil sensors.
    n = log_cycle(soil_pct={0: 41.2, 1: 38.7, 2: 44.0})
    print(f"wrote {n} readings")
    print("latest:", db.latest_readings())
    print("median soil:", db.median_soil_moisture())
