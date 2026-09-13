#!/usr/bin/env python3
"""
openGardener logger.

The single hardware owner. Reads sensors, writes the database and CSV,
services dashboard commands, runs auto-water, fires alerts and generates the
daily AI report. The Flask app never touches hardware; it enqueues commands
that this process acts on.

With LOCAL_SENSORS = False this stops reading sensors and becomes purely the
valve owner and decision engine, with readings arriving from wireless ESP32
nodes via /api/ingest instead. Everything downstream of the readings table is
unaffected either way.

Run:
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

# When one or more I2C devices are missing (a flaky long run dropping bed B,
# for example), re-probe the bus this often to pick them up automatically once
# the bus recovers, instead of staying blind until a manual restart.
REPROBE_INTERVAL = 60  # seconds

# When False, the logger stops reading local sensors entirely and becomes
# purely the valve owner and decision engine: commands, auto-water, alerts,
# effectiveness sampling and the AI report all read from the database, which
# the wireless nodes populate via /api/ingest. Flip this only once the nodes
# are actually reporting, or the watering logic goes blind -- with no readings
# arriving, median_soil_moisture() returns None and auto-water has nothing to
# decide on.
LOCAL_SENSORS = True

# Once-a-day AI report. Tracked by date so it fires once per calendar day.
AI_REPORT_STATE = os.path.expanduser("~/.opengardener_ai_daily.txt")
AI_REPORT_HOUR = 7  # local hour to generate the daily report (after sunrise)

# The full set of I2C devices we expect present when everything is wired. Used
# only to decide whether a re-probe is worth attempting.
#
# One ADS (bed A), one BME280, one BH1750. Bed B's air and light sensors were
# dropped as redundant (both beds share one air mass and one sky), and bed B
# is no longer wired at all. This previously read 2 of each, which made
# _any_missing() permanently true and had the logger re-probing the bus every
# 60 seconds forever, chasing devices that do not exist.
EXPECTED_I2C = {"ads": 1, "bme": 1, "bh": 1}

_running = True


def _missing_summary(dev):
    """Return a dict of how many of each expected I2C device are actually
    present (non-None), so we can tell if any are missing and worth
    re-probing for. init_devices pads missing slots with None, so counting
    truthy entries gives the real present-count."""
    return {
        "ads": sum(1 for x in dev.get("ads", []) if x is not None),
        "bme": sum(1 for x in dev.get("bme", []) if x is not None),
        "bh": sum(1 for x in dev.get("bh", []) if x is not None),
    }


def _any_missing(dev):
    have = _missing_summary(dev)
    return any(have[k] < EXPECTED_I2C[k] for k in EXPECTED_I2C)


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


def maybe_daily_ai_report(log):
    """Generate the AI garden report once per day, after AI_REPORT_HOUR local.
    Skips silently if no API key is configured."""
    from datetime import datetime as _dt
    today = _dt.now().strftime("%Y-%m-%d")
    if _dt.now().hour < AI_REPORT_HOUR:
        return
    try:
        with open(AI_REPORT_STATE) as f:
            if f.read().strip() == today:
                return  # already done today
    except Exception:
        pass
    try:
        import opengardener_ai_report as ai
        if not ai.have_key():
            return  # no key, nothing to do
        log("daily AI report: generating")
        r = ai.run_and_store()
        log(f"daily AI report: {'ok' if r.get('ok') else r.get('error')}")
        with open(AI_REPORT_STATE, "w") as f:
            f.write(today)
    except Exception as e:
        log(f"daily AI report failed: {e}")


def service_commands(valve, log):
    """Act on dashboard-issued commands. Manual watering and reboot; config
    changes are written directly by the dashboard and read live."""
    import subprocess
    for cmd in db.take_pending_commands():
        ctype = cmd.get("type")
        if ctype == "water_now":
            median, n = db.median_soil_moisture(band=(-5.0, 105.0))
            # Duration comes from the command payload (dashboard sets it);
            # fall back to the configured max_pulse_seconds if absent.
            payload = cmd.get("payload") or {}
            secs = payload.get("seconds")
            if secs in (None, ""):
                secs = db.get_config_value("max_pulse_seconds", "30")
            started, msg = valve.manual_pulse(secs, median=median)
            log(f"command water_now ({secs}s): {msg}")
        elif ctype == "reboot":
            # Valve owner closes the valve, THEN reboots, so we never reboot
            # with the valve open. Uses systemctl reboot, authorized for this
            # user by the polkit rule.
            log("command reboot: closing valve then rebooting")
            valve.close_now("reboot")
            subprocess.run(["systemctl", "reboot"])
        elif ctype == "ai_report":
            log("command ai_report: generating")
            try:
                import opengardener_ai_report as ai
                r = ai.run_and_store()
                log(f"ai_report: {'ok' if r.get('ok') else r.get('error')}")
            except Exception as e:
                log(f"ai_report failed: {e}")
        else:
            log(f"command ignored (unknown type): {ctype}")


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    def log(msg):
        print(f"{datetime.now().isoformat(timespec='seconds')} {msg}",
              flush=True)

    log("openGardener logger starting")

    # Hardware bring-up (same path the bench tool uses). Skipped entirely when
    # local sensors are disabled -- there is no reason to open the I2C bus or
    # enumerate 1-Wire probes if nothing is going to be read from them. The
    # valve is on GPIO and is unaffected.
    if LOCAL_SENSORS:
        i2c = gb.get_i2c()
        dev, problems = gb.init_devices(i2c)
        for p in problems:
            log(f"! {p}")
        cal = gb.load_cal()
        probes = gb.list_ds18b20()
        log(f"sensors up: {len(probes)} temp probes, cal keys {list(cal.keys())}")
    else:
        i2c = None
        dev = {"ads": [], "bme": [], "bh": []}
        cal = {}
        probes = []
        log("local sensors disabled; readings arrive from wireless nodes")

    # Valve controller: asserts CLOSED on init, inherits safety counters.
    valve = ValveController(logger=log, use_hardware=True)

    # Button handler (optional). Folds in so shutdown/reset close the valve.
    buttons = None
    try:
        from opengardener_buttons import ButtonHandler

        def _triple_press_water():
            # Physical triple-press waters for the dashboard's default duration,
            # via the same unrestricted manual path as the web button.
            secs = db.get_config_value("max_pulse_seconds", "30")
            median, _ = db.median_soil_moisture(band=(-5.0, 105.0))
            started, msg = valve.manual_pulse(secs, median=median)
            log(f"triple-press water ({secs}s): {msg}")

        buttons = ButtonHandler(
            stop_watering=lambda: valve.close_now("shutdown"),
            close_valve=lambda: valve.close_now("shutdown"),
            water_now=_triple_press_water,
            logger=log,
        )
        buttons.start()
    except Exception as e:
        log(f"button handler not started: {e}")

    # CSV setup, preserving the existing soak-file format. Only opened when
    # there is something local to write; wireless readings go to SQLite and
    # are exported through /api/export.csv instead.
    fh = None
    writer = None
    n_soil = len(gb.SOIL_CHANNELS) if LOCAL_SENSORS else 0
    if LOCAL_SENSORS:
        header = build_csv_header(n_soil, len(probes))
        new_file = not os.path.exists(CSV_FILE)
        fh = open(CSV_FILE, "a", newline="")
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(header)

    log(f"logging every {LOG_INTERVAL}s")

    last_reprobe = time.monotonic()

    try:
        while _running:
            cycle_start = time.monotonic()
            ts = datetime.now().isoformat(timespec="seconds")

            if LOCAL_SENSORS:
                # ---- periodically re-probe for missing I2C devices ----
                # A device can be absent at startup or drop mid-run. Rather
                # than stay blind until a manual restart, re-probe when
                # something is missing and adopt whatever comes back.
                if (_any_missing(dev)
                        and time.monotonic() - last_reprobe >= REPROBE_INTERVAL):
                    last_reprobe = time.monotonic()
                    try:
                        new_dev, new_problems = gb.init_devices(i2c)
                        before = _missing_summary(dev)
                        after = _missing_summary(new_dev)
                        if after != before:
                            dev = new_dev
                            log(f"re-probe: I2C devices now {after} (was {before})")
                        # 1-Wire probes can also appear/disappear; refresh list
                        new_probes = gb.list_ds18b20()
                        if len(new_probes) != len(probes):
                            log(f"re-probe: temp probes {len(probes)} -> "
                                f"{len(new_probes)}")
                            probes = new_probes
                    except Exception as e:
                        log(f"re-probe failed: {e}")

                # ---- read all sensors once ----
                raws = [gb.read_soil_raw(dev, a, c) for a, c in gb.SOIL_CHANNELS]
                temps_c = [gb.read_ds18b20(s) for s in probes]
                # Pair each soil channel with its co-located temp probe (same
                # index: soil0/temp0 = San Andreas A, etc.) and temperature-
                # compensate the moisture so it's comparable across the day's
                # heat swing.
                temps_f = [c_to_f(t) if t is not None else None for t in temps_c]
                pcts = [gb.soil_percent_compensated(
                            raws[i], cal, i,
                            temps_f[i] if i < len(temps_f) else None)
                        for i in range(n_soil)]

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
                    soil_temp_f={i: temps_f[i] for i in range(len(temps_f))
                                 if temps_f[i] is not None},
                    bme={i: {"temp_f": c_to_f(bme_vals[i][0]),
                             "humidity": bme_vals[i][1],
                             "pressure": bme_vals[i][2]}
                         for i in range(len(bme_vals))
                         if bme_vals[i] is not None},
                    lux={i: lux_vals[i] for i in range(len(lux_vals))
                         if lux_vals[i] is not None},
                    ts=ts,
                )

                # ---- raw voltages, for calibration ----
                # A percentage computed against a stale curve cannot be
                # recovered afterwards, and capturing a dry/wet point needs the
                # voltage the probe actually produced. Storing it under a
                # separate metric lets /api/calibration/capture work for wired
                # sensors exactly as it does for the wireless nodes, so
                # recalibrating does not mean an SSH session and a hand-edited
                # JSON file.
                try:
                    volt_rows = [(f"soil{i}", round(raws[i], 4), "volts")
                                 for i in range(n_soil) if raws[i] is not None]
                    if volt_rows:
                        db.insert_readings(volt_rows, ts=ts)
                except Exception as e:
                    log(f"raw voltage write failed: {e}")

            # ---- service dashboard commands, then evaluate auto-water ----
            # Everything below reads from the database, not from hardware, so
            # it runs identically whether readings came from local sensors or
            # from wireless nodes.
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

            # ---- daily AI garden report (once per day, if key present) ----
            try:
                maybe_daily_ai_report(log)
            except Exception as e:
                log(f"ai report check failed: {e}")

            # ---- pace the loop ----
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, LOG_INTERVAL - elapsed)
            # Sleep in short slices so a shutdown signal is honored promptly,
            # AND so a manual "water now" command is acted on within ~2s rather
            # than waiting for the next full cycle.
            since_cmd_check = 0.0
            while sleep_for > 0 and _running:
                nap = min(1.0, sleep_for)
                time.sleep(nap)
                sleep_for -= nap
                since_cmd_check += nap
                if since_cmd_check >= 2.0:
                    since_cmd_check = 0.0
                    try:
                        service_commands(valve, log)
                    except Exception as e:
                        log(f"command check failed: {e}")

    finally:
        log("logger stopping, asserting valve closed")
        valve.close_now("shutdown")
        if buttons:
            try:
                buttons.stop()
            except Exception:
                pass
        if fh:
            fh.close()
        log("logger stopped")


if __name__ == "__main__":
    main()
