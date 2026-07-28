#!/usr/bin/env python3
"""
openGardener database access layer.

Single source of truth for how data is shaped, imported by BOTH the logger
(writer) and the Flask app (reader + command writer). Keeping this in one
module stops the two processes from drifting into different ideas of the
schema.

Nothing here touches hardware. The logger owns the bus and the valve; this
module only reads and writes SQLite.

Concurrency: WAL mode (set by the initializer) lets the Flask app read while
the logger writes. Each call opens a short-lived connection to keep things
simple and avoid cross-thread connection sharing in Flask.
"""

import json
import os
import sqlite3
import statistics
from datetime import datetime, timedelta

DB_PATH = os.path.expanduser("~/opengardener.db")


def _conn():
    c = sqlite3.connect(DB_PATH, timeout=5.0)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON;")
    return c


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ---------- sensors ----------

def get_sensors(kind=None, grp=None):
    q = "SELECT * FROM sensors"
    clauses, args = [], []
    if kind:
        clauses.append("kind = ?")
        args.append(kind)
    if grp:
        clauses.append("grp = ?")
        args.append(grp)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " ORDER BY sensor_key"
    with _conn() as c:
        return [dict(r) for r in c.execute(q, args).fetchall()]


# ---------- readings (writer: logger) ----------

def insert_reading(sensor_key, value, metric="value", ts=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO readings (ts, sensor_key, metric, value) "
            "VALUES (?, ?, ?, ?);",
            (ts or _now(), sensor_key, metric, value),
        )


def insert_readings(rows, ts=None):
    """rows: iterable of (sensor_key, value) or (sensor_key, value, metric)."""
    ts = ts or _now()
    norm = []
    for r in rows:
        if len(r) == 2:
            norm.append((ts, r[0], "value", r[1]))
        else:
            norm.append((ts, r[0], r[2], r[1]))
    with _conn() as c:
        c.executemany(
            "INSERT INTO readings (ts, sensor_key, metric, value) "
            "VALUES (?, ?, ?, ?);",
            norm,
        )


# ---------- readings (reader: dashboard) ----------

def latest_readings():
    """Most recent value per sensor_key/metric. Sensors with no rows are
    absent from the result, which the dashboard renders as 'offline'."""
    q = """
    SELECT r.sensor_key, r.metric, r.value, r.ts
    FROM readings r
    JOIN (
        SELECT sensor_key, metric, MAX(id) AS max_id
        FROM readings GROUP BY sensor_key, metric
    ) m ON r.id = m.max_id
    """
    with _conn() as c:
        return [dict(r) for r in c.execute(q).fetchall()]


def history(sensor_key, metric="value", hours=24):
    since = (datetime.now() - timedelta(hours=hours)).isoformat(
        timespec="seconds"
    )
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, value FROM readings "
            "WHERE sensor_key = ? AND metric = ? AND ts >= ? ORDER BY ts;",
            (sensor_key, metric, since),
        ).fetchall()
    return [dict(r) for r in rows]


def history_downsampled(sensor_key, metric="value", hours=24, max_points=400):
    """History bucketed to at most max_points, averaging within each bucket.
    Keeps long ranges (7d, 30d) fast on the Pi and readable in the browser.
    Short ranges with few rows return effectively raw."""
    since = datetime.now() - timedelta(hours=hours)
    since_s = since.isoformat(timespec="seconds")
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, value FROM readings "
            "WHERE sensor_key = ? AND metric = ? AND ts >= ? "
            "AND value IS NOT NULL ORDER BY ts;",
            (sensor_key, metric, since_s),
        ).fetchall()

    if len(rows) <= max_points:
        return [{"ts": r["ts"], "value": r["value"]} for r in rows]

    # Bucket by time so gaps stay visible rather than being compressed away.
    span = timedelta(hours=hours).total_seconds()
    bucket_s = span / max_points
    buckets = {}
    for r in rows:
        try:
            t = datetime.fromisoformat(r["ts"])
        except ValueError:
            continue
        idx = int((t - since).total_seconds() // bucket_s)
        b = buckets.setdefault(idx, {"sum": 0.0, "n": 0, "ts": r["ts"]})
        b["sum"] += r["value"]
        b["n"] += 1
    out = []
    for idx in sorted(buckets):
        b = buckets[idx]
        out.append({"ts": b["ts"], "value": round(b["sum"] / b["n"], 2)})
    return out


def median_soil_moisture(band=None):
    """Median of the latest soil-moisture readings, optionally filtered to an
    in-band range. Returns (median, n_used). Excludes out-of-band sensors so a
    disconnected/shorted probe can't skew the watering decision.

    band: optional (low, high) in percent; readings outside are dropped.
    """
    latest = {
        r["sensor_key"]: r["value"]
        for r in latest_readings()
        if r["metric"] == "value"
    }
    soil_keys = {s["sensor_key"] for s in get_sensors(kind="soil_moisture")}
    vals = []
    for k, v in latest.items():
        if k not in soil_keys or v is None:
            continue
        if band and not (band[0] <= v <= band[1]):
            continue
        vals.append(v)
    if not vals:
        return None, 0
    return statistics.median(vals), len(vals)


# ---------- config ----------

def get_config():
    with _conn() as c:
        return {r["key"]: r["value"] for r in c.execute(
            "SELECT key, value FROM config;"
        ).fetchall()}


def get_config_value(key, default=None):
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM config WHERE key = ?;", (key,)
        ).fetchone()
    return row["value"] if row else default


def set_config_value(key, value):
    with _conn() as c:
        c.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value;",
            (key, str(value)),
        )


# ---------- watering events ----------

def start_watering_event(trigger, median_at_trigger):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO watering_events "
            "(ts_start, trigger, median_at_trigger) VALUES (?, ?, ?);",
            (_now(), trigger, median_at_trigger),
        )
        return cur.lastrowid


def end_watering_event(event_id, duration_seconds, result):
    with _conn() as c:
        c.execute(
            "UPDATE watering_events "
            "SET ts_end = ?, duration_seconds = ?, result = ? WHERE id = ?;",
            (_now(), duration_seconds, result, event_id),
        )


def recent_watering(limit=20):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM watering_events ORDER BY ts_start DESC LIMIT ?;",
            (limit,),
        ).fetchall()]


# ---- watering effectiveness ----

def record_effectiveness(event_id, minutes_after, median):
    with _conn() as c:
        c.execute(
            "INSERT INTO watering_effectiveness "
            "(event_id, minutes_after, median, ts) VALUES (?, ?, ?, ?);",
            (event_id, minutes_after, median, _now()),
        )


def effectiveness_done(event_id, minutes_after):
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM watering_effectiveness "
            "WHERE event_id = ? AND minutes_after = ? LIMIT 1;",
            (event_id, minutes_after),
        ).fetchone()
    return row is not None


def events_awaiting_samples(offsets, within_minutes=120):
    """Completed events recent enough that a follow-up sample may still be due.
    Returns list of dicts with id, ts_start, median_at_trigger."""
    with _conn() as c:
        rows = c.execute(
            "SELECT id, ts_start, median_at_trigger FROM watering_events "
            "WHERE ts_start >= datetime('now', ?, 'localtime') "
            "ORDER BY ts_start DESC;",
            (f"-{within_minutes} minutes",),
        ).fetchall()
    return [dict(r) for r in rows]


def effectiveness_for(event_id):
    with _conn() as c:
        rows = c.execute(
            "SELECT minutes_after, median FROM watering_effectiveness "
            "WHERE event_id = ? ORDER BY minutes_after;",
            (event_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---- pressure tendency ----

def _pressure_at(sensor_key, hours_ago):
    """Nearest pressure reading to `hours_ago` hours back (within a window)."""
    target = datetime.now() - timedelta(hours=hours_ago)
    lo = (target - timedelta(minutes=45)).isoformat(timespec="seconds")
    hi = (target + timedelta(minutes=45)).isoformat(timespec="seconds")
    with _conn() as c:
        row = c.execute(
            "SELECT value, ts FROM readings "
            "WHERE sensor_key = ? AND metric = 'pressure' "
            "AND value IS NOT NULL AND ts BETWEEN ? AND ? "
            "ORDER BY ABS(strftime('%s', ts) - strftime('%s', ?)) LIMIT 1;",
            (sensor_key, lo, hi, target.isoformat(timespec="seconds")),
        ).fetchone()
    return row["value"] if row else None


def _latest_pressure(sensor_key):
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM readings WHERE sensor_key = ? "
            "AND metric = 'pressure' AND value IS NOT NULL "
            "ORDER BY id DESC LIMIT 1;",
            (sensor_key,),
        ).fetchone()
    return row["value"] if row else None


def _describe_tendency(delta_3h):
    """Map a 3-hour hPa change to a plain-language reading using the
    conventional barometric-tendency thresholds."""
    if delta_3h is None:
        return "unknown", "flat"
    d = delta_3h
    if d <= -6:
        return "storm likely", "down"
    if d <= -3:
        return "deteriorating", "down"
    if d <= -1:
        return "slowly falling", "down"
    if d < 1:
        return "steady", "flat"
    if d < 3:
        return "slowly rising", "up"
    if d < 6:
        return "improving", "up"
    return "rapidly clearing", "up"


def pressure_tendency(sensor_key):
    """3-hour tendency (headline) plus 24-hour context for a bed's BME280.
    Returns a dict, or None if there isn't enough history yet."""
    now = _latest_pressure(sensor_key)
    if now is None:
        return None
    p3 = _pressure_at(sensor_key, 3)
    p24 = _pressure_at(sensor_key, 24)
    d3 = round(now - p3, 1) if p3 is not None else None
    d24 = round(now - p24, 1) if p24 is not None else None
    words, arrow = _describe_tendency(d3)
    return {
        "current": round(now, 1),
        "change_3h": d3,
        "change_24h": d24,
        "rate_3h": round(d3 / 3.0, 2) if d3 is not None else None,  # hPa/hr
        "words": words,
        "arrow": arrow,
    }


def last_watering_time():
    with _conn() as c:
        row = c.execute(
            "SELECT ts_start FROM watering_events ORDER BY ts_start DESC "
            "LIMIT 1;"
        ).fetchone()
    return row["ts_start"] if row else None


def pulses_today():
    start = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")
    with _conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM watering_events WHERE ts_start >= ?;",
            (start,),
        ).fetchone()[0]


# ---------- commands (dashboard -> logger) ----------

def enqueue_command(cmd_type, payload=None):
    with _conn() as c:
        c.execute(
            "INSERT INTO commands (ts, type, payload) VALUES (?, ?, ?);",
            (_now(), cmd_type, json.dumps(payload) if payload else None),
        )


def take_pending_commands():
    """Return unconsumed commands and mark them consumed atomically."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM commands WHERE consumed = 0 ORDER BY ts;"
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.executemany(
                "UPDATE commands SET consumed = 1 WHERE id = ?;",
                [(i,) for i in ids],
            )
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else None
            out.append(d)
        return out


if __name__ == "__main__":
    # Smoke test against the initialized DB.
    print("sensors:", len(get_sensors()))
    print("config:", get_config())
    print("latest readings:", len(latest_readings()))
    print("median soil:", median_soil_moisture())
