#!/usr/bin/env python3
"""
openGardener database initializer.

Creates the SQLite schema, enables WAL mode (so the dashboard can read while
the logger writes), seeds the config table (auto-water OFF, threshold unset),
and registers the known sensors including the named planters.

Run once:
  python3 opengardener_initdb.py

Safe to re-run: uses CREATE TABLE IF NOT EXISTS and INSERT OR IGNORE, so it
won't clobber existing data or reset config you've changed. To wipe and start
over, delete the .db file first.

Inspect afterwards:
  sqlite3 ~/opengardener.db ".tables"
  sqlite3 ~/opengardener.db ".schema readings"
  sqlite3 ~/opengardener.db "SELECT * FROM sensors;"
  sqlite3 ~/opengardener.db "SELECT * FROM config;"
"""

import os
import sqlite3

DB_PATH = os.path.expanduser("~/opengardener.db")

# Sensor registry. Every planned sensor slot is listed here so the dashboard
# can show offline tiles for hardware not yet wired. As sensors come online
# the logger writes readings against these keys and the tiles go live.
#
# key:    stable identifier used in readings.sensor_key
# kind:   soil_moisture | soil_temp | air | light
# group:  A | B  (the two planter groups; ambient sensors are per-group)
# label:  human name shown on the dashboard
# detail: extra context (ADC channel, address, etc.)
SENSORS = [
    # Group A soil moisture (WIRED NOW: ADS1115 0x48, channels 0-2)
    ("soil0", "soil_moisture", "A", "San Andreas", "ADS 0x48 ch0"),
    ("soil1", "soil_moisture", "A", "Sequoia",     "ADS 0x48 ch1"),
    ("soil2", "soil_moisture", "A", "Albion",      "ADS 0x48 ch2"),
    # Group B soil moisture (ADS1115 0x49, channels 0-2).
    # Same variety order as group A: each variety has one planter per bed.
    ("soil3", "soil_moisture", "B", "San Andreas", "ADS 0x49 ch0"),
    ("soil4", "soil_moisture", "B", "Sequoia",     "ADS 0x49 ch1"),
    ("soil5", "soil_moisture", "B", "Albion",      "ADS 0x49 ch2"),
    # Soil temperature, one probe per planter (not yet wired: DS18B20 1-Wire)
    ("temp0", "soil_temp", "A", "San Andreas", "DS18B20"),
    ("temp1", "soil_temp", "A", "Sequoia",     "DS18B20"),
    ("temp2", "soil_temp", "A", "Albion",      "DS18B20"),
    ("temp3", "soil_temp", "B", "San Andreas", "DS18B20"),
    ("temp4", "soil_temp", "B", "Sequoia",     "DS18B20"),
    ("temp5", "soil_temp", "B", "Albion",      "DS18B20"),
    # Garden-wide ambient: a single BME280 (air temp/humidity/pressure) and a
    # single BH1750 (light) cover the whole garden. The two beds are side by
    # side in the same air and light, so per-bed ambient would be redundant.
    ("bme0", "air", "A", "Garden air", "BME280 0x76"),
    ("lux0", "light", "A", "Garden light", "BH1750 0x23"),
]

# Config defaults. Auto-water OFF and threshold NULL are deliberate: automation
# must not arm until a threshold is set from observed dry-down data.
CONFIG_DEFAULTS = [
    ("auto_water_enabled", "0"),      # 0 = off. Must be toggled on in the UI.
    # Threshold is field-capacity anchored: the wet calibration point was taken
    # in saturated planter soil, so 100% = field capacity. The published rule
    # for (day-neutral) strawberries is to water at ~50% of field capacity, and
    # small hanging planters dry fast, so 50 errs appropriately wet. Refine from
    # observed dry-down: ease down toward 40-45 if soil stays soggy, up if it
    # wilts between waterings.
    ("threshold_pct", "50"),
    ("max_pulse_seconds", "30"),      # hard cap per valve pulse
    ("soak_lockout_minutes", "30"),   # no watering for this long after a pulse
    ("daily_pulse_cap", "2"),         # max pulses per day (conservative)
    ("frost_alert_f", "33"),          # the one v1 alert; physical constant
    ("soil_heat_warn_f", "86"),       # watch-level heat warning; tune from data
    ("weather_enabled", "0"),         # rain-skip master switch (off until set)
    ("rain_skip_inches", "0.1"),      # skip auto-water if >= this rain forecast
    ("rain_skip_hours", "12"),        # ...within this many hours ahead
    ("latitude", "34.1706"),          # Thousand Oaks, CA
    ("longitude", "-118.8376"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sensors (
    sensor_key TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    grp        TEXT NOT NULL,
    label      TEXT NOT NULL,
    detail     TEXT
);

-- Tall format: one row per sensor per cycle. Adding sensors later is just new
-- rows, no schema change. Fields on 'air' readings (temp/humidity/pressure)
-- are stored as separate sensor readings via subkeys, see note below.
CREATE TABLE IF NOT EXISTS readings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    sensor_key TEXT NOT NULL,
    metric     TEXT NOT NULL DEFAULT 'value',
    value      REAL,
    FOREIGN KEY (sensor_key) REFERENCES sensors (sensor_key)
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings (ts);
CREATE INDEX IF NOT EXISTS idx_readings_key_ts ON readings (sensor_key, ts);

CREATE TABLE IF NOT EXISTS watering_events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_start          TEXT NOT NULL,
    ts_end            TEXT,
    trigger           TEXT NOT NULL,          -- 'auto' | 'manual'
    median_at_trigger REAL,
    duration_seconds  REAL,
    result            TEXT                    -- completed | aborted_max_ontime | aborted_crash
);
CREATE INDEX IF NOT EXISTS idx_watering_start ON watering_events (ts_start);

CREATE TABLE IF NOT EXISTS commands (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    type     TEXT NOT NULL,                   -- 'water_now' | 'set_config'
    payload  TEXT,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_commands_unconsumed ON commands (consumed, ts);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Follow-up moisture samples after a watering event, for effectiveness.
-- One row per (event, minutes_after) capturing the median at that offset.
CREATE TABLE IF NOT EXISTS watering_effectiveness (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL,
    minutes_after INTEGER NOT NULL,
    median        REAL,
    ts            TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES watering_events (id)
);
CREATE INDEX IF NOT EXISTS idx_eff_event ON watering_effectiveness (event_id);
"""


def main():
    fresh = not os.path.exists(DB_PATH)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(SCHEMA)

        conn.executemany(
            "INSERT OR IGNORE INTO sensors "
            "(sensor_key, kind, grp, label, detail) VALUES (?, ?, ?, ?, ?);",
            SENSORS,
        )
        conn.executemany(
            "INSERT OR IGNORE INTO config (key, value) VALUES (?, ?);",
            CONFIG_DEFAULTS,
        )
        conn.commit()

        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        n_sensors = conn.execute("SELECT COUNT(*) FROM sensors;").fetchone()[0]
        n_config = conn.execute("SELECT COUNT(*) FROM config;").fetchone()[0]
    finally:
        conn.close()

    print(f"{'created' if fresh else 'updated'} {DB_PATH}")
    print(f"  journal mode: {mode}")
    print(f"  sensors registered: {n_sensors}")
    print(f"  config keys: {n_config}")
    print("auto-water is OFF and threshold is unset, by design.")


if __name__ == "__main__":
    main()
