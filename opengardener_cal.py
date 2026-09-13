#!/usr/bin/env python3
"""
openGardener calibration file access.

garden_calibration.json is read by the logger (and by the ingest path for the
wireless nodes) to turn a raw probe voltage into a moisture percentage. Before
this module the only way to change it was SSH and a text editor, and the only
writer was garden_bringup.py, which cannot be imported by the web app because
it pulls in board/busio at import time.

This module is deliberately hardware-free so the web app, the logger and
garden_bringup can all import it. It does no I2C, no ADC reads, and no
database work: it owns the JSON file and the rules about what a valid
two-point calibration looks like, nothing else.

File shape (unchanged, so existing calibrations keep working):

    {
      "0": {"dry": 2.61, "wet": 1.18, "coeff": 0.0, "ref_f": 70.0},
      "1": {"dry": 2.58, "wet": 1.21}
    }

The key is the numeric suffix of the soil sensor key: soil0 -> "0", soil7 ->
"7". That is the same index garden_bringup.soil_percent() already uses, so the
planters table and the calibration file stay joinable without a second mapping
table to keep in sync.

HW-390 probes are inverted: MORE water means LOWER voltage. So a valid pair has
dry > wet. A pair that is inverted or nearly equal is not silently accepted --
the caller gets told, and soil_percent() already returns None when the two
points are within 0.05V of each other.
"""

import json
import os
import tempfile

CAL_FILE = os.path.expanduser("~/garden_calibration.json")

# Two anchors closer together than this cannot describe a real dry-to-wet
# range; garden_bringup.soil_percent() uses the same number to decide a
# calibration is unusable, so keep them in step.
MIN_SPREAD_V = 0.05


def cal_index(soil_key):
    """'soil5' -> '5'. Returns None for anything that is not a soil key, which
    keeps a malformed planter row from writing a junk entry into the file."""
    if not soil_key or not str(soil_key).startswith("soil"):
        return None
    suffix = str(soil_key)[4:]
    if not suffix.isdigit():
        return None
    return suffix


def load():
    """Whole calibration file as a dict. A missing or corrupt file reads as
    empty rather than raising: an uncalibrated garden still has to render."""
    try:
        with open(CAL_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(cal):
    """Atomic write. The logger re-reads this file on mtime change while the
    web app writes it, so a half-written file would be read as corrupt and
    blank every probe's calibration until the next save."""
    d = os.path.dirname(CAL_FILE) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".garden_cal.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cal, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CAL_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def entry_status(entry):
    """Classify one probe's calibration pair.

    ok           both anchors present and sanely separated
    partial      one anchor captured, the other still missing
    inverted     wet is at or above dry, so percentages would run backwards
    too_close    both present but within MIN_SPREAD_V, so unusable
    missing      nothing captured yet
    """
    if not entry:
        return "missing"
    dry, wet = entry.get("dry"), entry.get("wet")
    if dry is None and wet is None:
        return "missing"
    if dry is None or wet is None:
        return "partial"
    if dry <= wet:
        return "inverted"
    if dry - wet < MIN_SPREAD_V:
        return "too_close"
    return "ok"


def percent(volts, entry):
    """Raw (uncompensated) moisture percent, same formula the logger uses.
    Returns None unless the pair is usable, so a broken calibration shows as
    'uncalibrated' rather than as a confident wrong number."""
    if volts is None or entry_status(entry) != "ok":
        return None
    dry, wet = entry["dry"], entry["wet"]
    pct = (dry - volts) / (dry - wet) * 100.0
    return round(max(-10.0, min(110.0, pct)), 1)


def capture(soil_key, point, volts):
    """Write one anchor for one probe and return (result_dict, http_ok).

    The point is stored even when it inverts the pair, because refusing would
    trap a probe whose OTHER anchor is the bad one: capture dry wrong, and you
    could never capture a correct wet to fix it. The caller gets status
    'inverted' back and the dashboard shows it in red; percentages stay None
    while it is inverted, so a bad anchor cannot quietly drive watering.
    """
    if point not in ("dry", "wet"):
        return {"ok": False, "error": "point must be dry or wet"}, False
    idx = cal_index(soil_key)
    if idx is None:
        return {"ok": False, "error": f"bad soil key {soil_key!r}"}, False
    try:
        v = round(float(volts), 4)
    except (TypeError, ValueError):
        return {"ok": False, "error": "no usable voltage"}, False

    cal = load()
    entry = dict(cal.get(idx) or {})
    entry[point] = v
    cal[idx] = entry
    save(cal)

    status = entry_status(entry)
    out = {
        "ok": True,
        "soil_key": soil_key,
        "index": idx,
        "point": point,
        "volts": v,
        "dry": entry.get("dry"),
        "wet": entry.get("wet"),
        "status": status,
    }
    if status == "inverted":
        out["warning"] = ("dry must read HIGHER than wet on these probes; "
                          "recapture the other point")
    elif status == "too_close":
        out["warning"] = (f"dry and wet are within {MIN_SPREAD_V}V; "
                          "moisture stays uncalibrated until they separate")
    return out, True


def clear(soil_key):
    """Drop both anchors for one probe, keeping any temperature-compensation
    coefficients. Recovery path for a pair that got captured backwards."""
    idx = cal_index(soil_key)
    if idx is None:
        return {"ok": False, "error": f"bad soil key {soil_key!r}"}, False
    cal = load()
    entry = dict(cal.get(idx) or {})
    entry.pop("dry", None)
    entry.pop("wet", None)
    if entry:
        cal[idx] = entry
    else:
        cal.pop(idx, None)
    save(cal)
    return {"ok": True, "soil_key": soil_key, "index": idx,
            "status": "missing"}, True


def mtime():
    """Last-modified epoch seconds, or 0 if the file is absent. The logger
    watches this to pick up a calibration captured from the dashboard without
    needing a restart."""
    try:
        return os.path.getmtime(CAL_FILE)
    except OSError:
        return 0.0


if __name__ == "__main__":
    cal = load()
    print(f"{CAL_FILE}: {len(cal)} entries")
    for k in sorted(cal, key=lambda s: int(s) if s.isdigit() else 99):
        print(f"  soil{k}: {cal[k]}  -> {entry_status(cal[k])}")
