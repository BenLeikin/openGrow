#!/usr/bin/env python3
"""
openGardener integrated logger.

The single owner of the hardware: the I2C bus, the 1-Wire bus, and the valve.
Nothing else touches GPIO. The dashboard writes commands/config to SQLite; this
loop reads and acts on them.

Each cycle:
  1. Read all sensors (via garden_bringup's proven read functions)
  2. Write readings to SQLite (and the soak CSV)
  3. Service pending dashboard commands (manual water)
  4. Evaluate auto-water (acts only if enabled; logs the decision either way)

Startup asserts the valve CLOSED. Shutdown/reset (via the button handler) and
any crash leave the valve closed. Safety counters (lockout, daily cap) survive
a restart because they're read from the DB, but the valve never resumes an open
state.

Run under systemd (opengardener-logger.service). For a manual run:
  sudo ~/garden/bin/python ~/opengardener_logger.py
(sudo because GPIO edge detection for the buttons needs it; if you drop the
button handler it can run as your normal user.)
"""

import csv
import os
import signal
import sys
import time
from datetime import datetime

# Sensor reading comes from the proven bench tool. Importing it also runs its
# hardware setup imports, which is exactly what we want here.
import garden_bringup as gb
import opengardener_db as db
from opengardener_log import log_cycle, c_to_f
from opengardener_valve import ValveController
from opengardener_autowater import evaluate as evaluate_autowater
import opengardener_alerts as alerts

LOG_INTERVAL = gb.LOG_INTERVAL       # 60s
CSV_FILE = os.path.expanduser("~/garden_soak.csv")

_running = True


def _handle_signal(signum, frame):
    global _running
    _running = False


def build_csv_header(n_soil, n_probes):
    header = ["timestamp"]
    header += [f"soil{i}_v" for i in range(n_soil)]
    header += [f"soil{i}_pct" for i in range(n_soil)]
    header += [f"soiltemp{i}" for i in range(n_probes)]
    header += ["lux0", "lux1"]
    header += ["temp0", "rh0", "hpa0", "temp1", "rh1", "hpa1"]
    return header


EFFECTIVENESS_OFFSETS = [15, 30, 60]  # minutes after a pulse to sample


def sample_effectiveness(log):
    """For each recent watering event, record the median soil moisture at
    fixed offsets (15/30/60 min) after it fired. Compared against the
    median_at_trigger stored on the event, this shows how much a pulse
    actually raised soil moisture once the water registered."""
    from datetime import datetime as _dt
    events = db.events_awaiting_samples(EFFECTIVENESS_OFFSETS, within_minutes=90)
    if not events:
        return
    median, _ = db.median_soil_moisture(band=(-5.0, 105.0))
    if median is None:
        return
    now = _dt.now()
    for e in events:
        try:
            start = _dt.fromisoformat(e["ts_start"])
        except Exception:
            continue
        mins = (now - start).total_seconds() / 60.0
        for off in EFFECTIVENESS_OFFSETS:
            # record once we're at/just past the offset and haven't yet
            if mins >= off and not db.effectiveness_done(e["id"], off):
                db.record_effectiveness(e["id"], off, median)
                before = e.get("median_at_trigger")
                delta = (f"{median - before:+.0f}"
                         if before is not None else "?")
                log(f"effectiveness: event {e['id']} +{off}min "
                    f"median {median:.0f}% (delta {delta})")


def service_commands(valve, log):
    """Act on dashboard-issued commands. Manual watering and reboot; config
    changes are written directly by the dashboard and read live."""
    import subprocess
    for cmd in db.take_pending_commands():
        ctype = cmd.get("type")
        if ctype == "water_now":
            median, n = db.median_soil_moisture(band=(-5.0, 105.0))
            started, msg = valve.pulse("manual", median=median)
            log(f"command water_now: {msg}")
        elif ctype == "reboot":
            # Valve owner closes the valve, THEN reboots, so we never reboot
            # with the valve open. Uses systemctl reboot, authorized for this
            # user by the polkit rule.
            log("command reboot: closing valve then rebooting")
            valve.close_now("reboot")
            subprocess.run(["systemctl", "reboot"])
        else:
            log(f"command ignored (unknown type): {ctype}")


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    def log(msg):
        print(f"{datetime.now().isoformat(timespec='seconds')} {msg}",
              flush=True)

    log("openGardener logger starting")

    # Hardware bring-up (same path the bench tool uses).
    i2c = gb.get_i2c()
    dev, problems = gb.init_devices(i2c)
    for p in problems:
        log(f"! {p}")
    cal = gb.load_cal()
    probes = gb.list_ds18b20()
    log(f"sensors up: {len(probes)} temp probes, cal keys {list(cal.keys())}")

    # Valve controller: asserts CLOSED on init, inherits safety counters.
    valve = ValveController(logger=log, use_hardware=True)

    # Button handler (optional). Folds in so shutdown/reset close the valve.
    buttons = None
    try:
        from opengardener_buttons import ButtonHandler
        buttons = ButtonHandler(
            stop_watering=lambda: valve.close_now("shutdown"),
            close_valve=lambda: valve.close_now("shutdown"),
            logger=log,
        )
        buttons.start()
    except Exception as e:
        log(f"button handler not started: {e}")

    # CSV setup, preserving the existing soak-file format.
    n_soil = len(gb.SOIL_CHANNELS)
    header = build_csv_header(n_soil, len(probes))
    new_file = not os.path.exists(CSV_FILE)
    fh = open(CSV_FILE, "a", newline="")
    writer = csv.writer(fh)
    if new_file:
        writer.writerow(header)

    log(f"logging every {LOG_INTERVAL}s")

    try:
        while _running:
            cycle_start = time.monotonic()
            ts = datetime.now().isoformat(timespec="seconds")

            # ---- read all sensors once ----
            raws = [gb.read_soil_raw(dev, a, c) for a, c in gb.SOIL_CHANNELS]
            pcts = [gb.soil_percent(raws[i], cal, i) for i in range(n_soil)]
            temps_c = [gb.read_ds18b20(s) for s in probes]

            lux_vals = []
            for bh in dev["bh"]:
                try:
                    lux_vals.append(round(bh.lux, 1))
                except Exception:
                    lux_vals.append(None)

            bme_vals = []
            for bme in dev["bme"]:
                try:
                    bme_vals.append((round(bme.temperature, 2),
                                     round(bme.relative_humidity, 1),
                                     round(bme.pressure, 1)))
                except Exception:
                    bme_vals.append(None)

            # ---- CSV row (unchanged format, Celsius) ----
            row = [ts]
            for raw in raws:
                row.append(round(raw, 4) if raw is not None else "")
            for pct in pcts:
                row.append(pct if pct is not None else "")
            for t in temps_c:
                row.append(t if t is not None else "")
            for v in lux_vals:
                row.append(v if v is not None else "")
            for b in bme_vals:
                row.extend(list(b) if b else ["", "", ""])
            writer.writerow(row)
            fh.flush()

            # ---- DB write (Fahrenheit) ----
            log_cycle(
                soil_pct={i: pcts[i] for i in range(n_soil)
                          if pcts[i] is not None},
                soil_temp_f={i: c_to_f(temps_c[i]) for i in range(len(temps_c))
                             if temps_c[i] is not None},
                bme={i: {"temp_f": c_to_f(bme_vals[i][0]),
                         "humidity": bme_vals[i][1],
                         "pressure": bme_vals[i][2]}
                     for i in range(len(bme_vals)) if bme_vals[i] is not None},
                lux={i: lux_vals[i] for i in range(len(lux_vals))
                     if lux_vals[i] is not None},
                ts=ts,
            )

            # ---- service dashboard commands, then evaluate auto-water ----
            service_commands(valve, log)
            evaluate_autowater(valve, logger=log)

            # ---- watering effectiveness follow-up samples ----
            try:
                sample_effectiveness(log)
            except Exception as e:
                log(f"effectiveness sampling failed: {e}")

            # ---- evaluate alerts (Discord), best-effort ----
            try:
                alerts.check(logger=log)
            except Exception as e:
                log(f"alert check failed: {e}")

            # ---- pace the loop ----
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, LOG_INTERVAL - elapsed)
            # Sleep in small slices so a shutdown signal is honored promptly.
            while sleep_for > 0 and _running:
                nap = min(1.0, sleep_for)
                time.sleep(nap)
                sleep_for -= nap

    finally:
        log("logger stopping, asserting valve closed")
        valve.close_now("shutdown")
        if buttons:
            try:
                buttons.stop()
            except Exception:
                pass
        fh.close()
        log("logger stopped")


if __name__ == "__main__":
    main()
