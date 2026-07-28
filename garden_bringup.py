#!/usr/bin/env python3
"""
Bench bring-up and soak test for the strawberry planter monitor.

Hardware assumed:
  0x48  ADS1115 #1   soil channels 0-3   (planters 1-4)
  0x49  ADS1115 #2   soil channels 0-1   (planters 5-6)
  0x23  BH1750 #1    group A light
  0x5C  BH1750 #2    group B light
  0x76  BME280 #1    group A air
  0x77  BME280 #2    group B air
  GPIO4 1-Wire       6x DS18B20 soil temp

Install (Bookworm needs a venv or --break-system-packages):
  python3 -m venv ~/garden && source ~/garden/bin/activate
  pip install adafruit-circuitpython-ads1x15 adafruit-circuitpython-bme280 \
              adafruit-circuitpython-bh1750 adafruit-blinka

Usage:
  ./garden_bringup.py scan               one-shot inventory of everything found
  ./garden_bringup.py calibrate 0 dry    record dry point for soil channel 0
  ./garden_bringup.py calibrate 0 wet    record wet point for soil channel 0
  ./garden_bringup.py log                continuous CSV logging for the soak test
"""

import csv
import glob
import json
import os
import statistics
import sys
import time
from datetime import datetime

CAL_FILE = os.path.expanduser("~/garden_calibration.json")
LOG_FILE = os.path.expanduser("~/garden_soak.csv")
LOG_INTERVAL = 60  # seconds

# Soil channels as (ads_index, channel). ads_index 0 = 0x48, 1 = 0x49.
# Three soil sensors per ADS on channels 0-2. Channel 3 on each ADS is the
# local 3.3V supply monitor, not a soil sensor, so it's excluded here.
#   soil0-2 -> 0x48 ch0-2 (group A: San Andreas, Sequoia, Albion)
#   soil3-5 -> 0x49 ch0-2 (group B: Albion, Sequoia, San Andreas)
SOIL_CHANNELS = [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2)]

# Fill these in from the labelling step so the CSV columns mean something.
# Order must match physical planter numbering.
DS18B20_ORDER = [
    "28-000000224625",   # temp0 San Andreas (group A)
    "28-000000ca0e41",   # temp1 Sequoia     (group A)
    "28-000000cb11bc",   # temp2 Albion      (group A)
    # group B to add later: temp3 Albion, temp4 Sequoia, temp5 San Andreas
]

import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_bh1750 import BH1750
from adafruit_bme280 import basic as adafruit_bme280

# openGardener database bridge. CSV logging stays as-is; the DB write is added
# alongside so the dashboard has a data source. c_to_f converts probe/BME
# Celsius to the Fahrenheit the dashboard expects.
from opengardener_log import log_cycle, c_to_f


def get_i2c():
    return busio.I2C(board.SCL, board.SDA)


def init_devices(i2c):
    """Probe every expected device. Missing ones are reported, not fatal."""
    dev = {"ads": [], "bh": [], "bme": []}
    problems = []

    for addr in (0x48, 0x49):
        try:
            a = ADS.ADS1115(i2c, address=addr)
            a.gain = 1  # +/-4.096V, matches 3.3V-powered HW-390 output swing
            dev["ads"].append(a)
        except Exception as e:
            dev["ads"].append(None)
            problems.append(f"ADS1115 at {hex(addr)}: {e}")

    for addr in (0x23, 0x5C):
        try:
            dev["bh"].append(BH1750(i2c, address=addr))
        except Exception as e:
            dev["bh"].append(None)
            problems.append(f"BH1750 at {hex(addr)}: {e}")

    for addr in (0x76, 0x77):
        try:
            dev["bme"].append(
                adafruit_bme280.Adafruit_BME280_I2C(i2c, address=addr)
            )
        except Exception as e:
            dev["bme"].append(None)
            problems.append(f"BME280 at {hex(addr)}: {e}")

    return dev, problems


def read_soil_raw(dev, ads_index, channel, samples=8):
    """Median of several samples. Single reads pick up switching noise."""
    ads = dev["ads"][ads_index]
    if ads is None:
        return None
    try:
        chan = AnalogIn(ads, channel)
        return statistics.median(chan.voltage for _ in range(samples))
    except Exception:
        return None


def load_cal():
    if os.path.exists(CAL_FILE):
        with open(CAL_FILE) as f:
            return json.load(f)
    return {}


def save_cal(cal):
    with open(CAL_FILE, "w") as f:
        json.dump(cal, f, indent=2)


def soil_percent(raw, cal, index):
    """HW-390 is inverted: higher voltage means drier."""
    key = str(index)
    if raw is None or key not in cal:
        return None
    entry = cal[key]
    if "dry" not in entry or "wet" not in entry:
        return None
    dry, wet = entry["dry"], entry["wet"]
    if abs(dry - wet) < 0.05:
        return None  # calibration points too close to be meaningful
    pct = (dry - raw) / (dry - wet) * 100.0
    return round(max(-10.0, min(110.0, pct)), 1)


def list_ds18b20():
    found = sorted(
        os.path.basename(p) for p in glob.glob("/sys/bus/w1/devices/28-*")
    )
    if not DS18B20_ORDER:
        return found
    ordered = [s for s in DS18B20_ORDER if s in found]
    ordered += [s for s in found if s not in DS18B20_ORDER]
    return ordered


def read_ds18b20(serial):
    path = f"/sys/bus/w1/devices/{serial}/w1_slave"
    try:
        with open(path) as f:
            lines = f.readlines()
        if not lines[0].strip().endswith("YES"):
            return None  # CRC failed, usually a wiring or pull-up problem
        return round(int(lines[1].split("t=")[1]) / 1000.0, 2)
    except Exception:
        return None


def cmd_scan():
    i2c = get_i2c()
    while not i2c.try_lock():
        pass
    addrs = [hex(a) for a in i2c.scan()]
    i2c.unlock()
    print(f"I2C addresses present: {', '.join(addrs) if addrs else 'none'}")

    expected = {0x23, 0x48, 0x49, 0x5C, 0x76, 0x77}
    missing = expected - set(int(a, 16) for a in addrs)
    if missing:
        print(f"MISSING: {', '.join(hex(m) for m in sorted(missing))}")

    dev, problems = init_devices(i2c)
    for p in problems:
        print(f"  ! {p}")

    cal = load_cal()
    print("\nSoil channels:")
    for i, (ads_i, ch) in enumerate(SOIL_CHANNELS):
        raw = read_soil_raw(dev, ads_i, ch)
        pct = soil_percent(raw, cal, i)
        raw_s = f"{raw:.3f}V" if raw is not None else "FAIL"
        pct_s = f"{pct}%" if pct is not None else "uncalibrated"
        print(f"  soil{i}  {raw_s:>8}  {pct_s}")

    print("\nLight:")
    for i, bh in enumerate(dev["bh"]):
        try:
            print(f"  lux{i}   {bh.lux:.1f}")
        except Exception:
            print(f"  lux{i}   FAIL")

    print("\nAir:")
    for i, bme in enumerate(dev["bme"]):
        try:
            print(
                f"  bme{i}   {bme.temperature:.2f}C  "
                f"{bme.relative_humidity:.1f}%RH  {bme.pressure:.1f}hPa"
            )
        except Exception:
            print(f"  bme{i}   FAIL")

    probes = list_ds18b20()
    print(f"\n1-Wire probes found: {len(probes)}")
    for i, s in enumerate(probes):
        t = read_ds18b20(s)
        print(f"  {s}  {'CRC FAIL' if t is None else f'{t:.2f}C'}")
    if len(probes) != 6:
        print("  ! expected 6 probes")


def cmd_calibrate(index, point):
    if point not in ("dry", "wet"):
        sys.exit("point must be 'dry' or 'wet'")
    index = int(index)
    if not 0 <= index < len(SOIL_CHANNELS):
        sys.exit(f"index must be 0-{len(SOIL_CHANNELS) - 1}")

    i2c = get_i2c()
    dev, _ = init_devices(i2c)
    ads_i, ch = SOIL_CHANNELS[index]

    print(f"Reading soil{index} for 10s, hold steady...")
    samples = []
    for _ in range(10):
        v = read_soil_raw(dev, ads_i, ch)
        if v is not None:
            samples.append(v)
        time.sleep(1)

    if len(samples) < 5:
        sys.exit("too many failed reads, check wiring")

    value = statistics.median(samples)
    spread = max(samples) - min(samples)
    print(f"median {value:.4f}V, spread {spread:.4f}V")
    if spread > 0.05:
        print("! spread is high, suggests noise on the analog run")

    cal = load_cal()
    cal.setdefault(str(index), {})[point] = round(value, 4)
    save_cal(cal)
    print(f"saved {point} for soil{index} to {CAL_FILE}")


def cmd_log():
    i2c = get_i2c()
    dev, problems = init_devices(i2c)
    for p in problems:
        print(f"! {p}")
    cal = load_cal()
    probes = list_ds18b20()

    header = ["timestamp"]
    header += [f"soil{i}_v" for i in range(len(SOIL_CHANNELS))]
    header += [f"soil{i}_pct" for i in range(len(SOIL_CHANNELS))]
    header += [f"soiltemp{i}" for i in range(len(probes))]
    header += ["lux0", "lux1"]
    header += ["temp0", "rh0", "hpa0", "temp1", "rh1", "hpa1"]

    new_file = not os.path.exists(LOG_FILE)
    fh = open(LOG_FILE, "a", newline="")
    writer = csv.writer(fh)
    if new_file:
        writer.writerow(header)

    print(f"logging to {LOG_FILE} every {LOG_INTERVAL}s, ctrl-c to stop")
    fails = 0
    reads = 0

    try:
        while True:
            ts = datetime.now().isoformat(timespec="seconds")
            row = [ts]

            # Read each sensor once, hold the values, then write both CSV and
            # DB from the same reads (no double-reading the slow 1-Wire bus).
            raws = [read_soil_raw(dev, ads_i, ch) for ads_i, ch in SOIL_CHANNELS]
            pcts = [soil_percent(raws[i], cal, i) for i in range(len(raws))]
            temps_c = [read_ds18b20(s) for s in probes]

            lux_vals = []
            for bh in dev["bh"]:
                try:
                    lux_vals.append(round(bh.lux, 1))
                except Exception:
                    lux_vals.append(None)

            bme_vals = []
            for bme in dev["bme"]:
                try:
                    bme_vals.append((
                        round(bme.temperature, 2),
                        round(bme.relative_humidity, 1),
                        round(bme.pressure, 1),
                    ))
                except Exception:
                    bme_vals.append(None)

            # CSV row, unchanged format (soil temps + BME temp stay Celsius here
            # to preserve the existing soak-file schema).
            for raw in raws:
                row.append(round(raw, 4) if raw is not None else "")
            for pct in pcts:
                row.append(pct if pct is not None else "")
            for t in temps_c:
                row.append(t if t is not None else "")
            for v in lux_vals:
                row.append(v if v is not None else "")
            for b in bme_vals:
                if b is None:
                    row.extend(["", "", ""])
                else:
                    row.extend(list(b))

            writer.writerow(row)
            fh.flush()

            # DB write. Fahrenheit for temps, only non-None values sent so
            # unwired sensors stay offline on the dashboard.
            log_cycle(
                soil_pct={i: pcts[i] for i in range(len(pcts))
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

            reads += 1
            # Only count blanks among wired soil channels; offline sensors
            # during partial wiring shouldn't inflate the soak fail rate.
            n_soil = len(SOIL_CHANNELS)
            wired_blanks = sum(1 for v in row[1:1 + n_soil] if v == "")
            fails += wired_blanks
            if wired_blanks:
                print(f"{ts}  {wired_blanks} failed soil reads this cycle")

            time.sleep(LOG_INTERVAL)
    except KeyboardInterrupt:
        rate = fails / max(reads * len(SOIL_CHANNELS), 1) * 100
        print(f"\n{reads} cycles, {fails} failed soil reads ({rate:.2f}%)")
        print("anything above ~0.1% means the bus needs attention before sealing")
    finally:
        fh.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    if cmd == "scan":
        cmd_scan()
    elif cmd == "calibrate":
        if len(sys.argv) != 4:
            sys.exit("usage: calibrate <channel> <dry|wet>")
        cmd_calibrate(sys.argv[2], sys.argv[3])
    elif cmd == "log":
        cmd_log()
    else:
        sys.exit(__doc__)
