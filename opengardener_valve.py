#!/usr/bin/env python3
"""
openGardener valve controller.

Owns the watering valve on GPIO18. ACTIVE-HIGH: GPIO high energizes the relay
and opens the valve; GPIO low closes it. The valve is normally-closed hardware,
so power loss or a crashed process leaves it shut.

Safety model (all enforced HERE, in the logger's process, so nothing the
dashboard sends can bypass them):

  1. Valve ALWAYS starts closed on init, unconditionally. A crash/power-loss
     mid-pulse must never resume an open valve into an unknown state.
  2. But the safety *counters* survive a restart: last watering time and
     today's pulse count are read from the DB, so a restart can't be used to
     escape the soak lockout or the daily cap.
  3. Max pulse seconds: a single open is capped; the valve auto-closes even if
     the caller never asks it to.
  4. Soak lockout: no watering for N minutes after any pulse (auto or manual).
  5. Daily pulse cap: at most N pulses per calendar day.
  6. Pulse-only: the valve is only ever opened for a bounded pulse in a
     background timer, never held open indefinitely, so a logic bug can't
     leave it commanded open.

Limits are read fresh from the DB config each time, so changing them on the
dashboard takes effect immediately.

Standalone test (valve wired, out of water):
  sudo ~/garden/bin/python opengardener_valve.py test
"""

import threading
import time
from datetime import datetime, timedelta

import opengardener_db as db

VALVE_GPIO = 18  # active-high; low = closed (safe)

# Fallback defaults if config is missing; the DB config normally supplies these.
DEFAULTS = {
    "max_pulse_seconds": 30.0,
    "soak_lockout_minutes": 30.0,
    "daily_pulse_cap": 2,
}

# Absolute hard ceiling for a MANUAL pulse. Manual watering bypasses the soak
# lockout and daily cap by design, but never this: it's the failsafe that
# guarantees the valve closes even if everything else fails (e.g. the process
# hangs while the valve is open). It bounds a worst-case stuck-open flood to a
# finite duration. Not a limit on normal use -- it's far longer than any real
# watering -- just a catastrophe bound.
MANUAL_FAILSAFE_MAX_S = 900.0  # 15 minutes


class ValveController:
    def __init__(self, logger=None, use_hardware=True):
        self._log = logger or print
        self._use_hw = use_hardware
        self._lock = threading.Lock()
        self._open = False
        self._pulse_timer = None
        self._current_event_id = None
        self._pulse_started = None

        if self._use_hw:
            from gpiozero import LED
            # LED is a clean on/off GPIO abstraction. active_high=True means
            # .on() drives the pin high (opens valve), .off() drives it low.
            # initial_value=False guarantees the pin starts LOW = valve closed.
            self._pin = LED(VALVE_GPIO, active_high=True, initial_value=False)
        else:
            self._pin = None

        # Assert closed, unconditionally, right now.
        self._drive_closed()
        self._log("valve controller init: valve asserted CLOSED")

        # Report the safety state we inherited from the DB (does NOT reopen
        # anything; just informs the caller the counters are intact).
        last = db.last_watering_time()
        today = db.pulses_today()
        self._log(f"  inherited: last watering {last or 'never'}, "
                  f"{today} pulses today")

    # ---- low-level pin ----
    def _drive_open(self):
        if self._pin:
            self._pin.on()
        self._open = True

    def _drive_closed(self):
        if self._pin:
            self._pin.off()
        self._open = False

    # ---- limit checks ----
    def _cfg(self, key, cast=float):
        v = db.get_config_value(key)
        if v in (None, ""):
            return DEFAULTS.get(key)
        try:
            return cast(v)
        except (ValueError, TypeError):
            return DEFAULTS.get(key)

    def in_lockout(self):
        """True if we're inside the soak-in window after the last pulse."""
        last = db.last_watering_time()
        if not last:
            return False, 0.0
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return False, 0.0
        lockout = timedelta(minutes=self._cfg("soak_lockout_minutes"))
        remaining = (last_dt + lockout) - datetime.now()
        secs = remaining.total_seconds()
        return (secs > 0), max(0.0, secs)

    def cap_reached(self):
        cap = int(self._cfg("daily_pulse_cap", int))
        return db.pulses_today() >= cap

    def can_water(self):
        """Return (ok, reason). Checks all gates without acting."""
        with self._lock:
            if self._open:
                return False, "valve already open"
        locked, secs = self.in_lockout()
        if locked:
            return False, f"soak lockout, {int(secs//60)}m {int(secs%60)}s left"
        if self.cap_reached():
            cap = int(self._cfg("daily_pulse_cap", int))
            return False, f"daily cap reached ({cap})"
        return True, "ok"

    # ---- the only public way to water ----
    def pulse(self, trigger, median=None):
        """Open the valve for one bounded pulse, if all gates pass.
        Returns (started: bool, reason: str). Non-blocking: the close is
        handled by a background timer, so the valve can't be left open by a
        caller that forgets to close it."""
        ok, reason = self.can_water()
        if not ok:
            self._log(f"pulse denied ({trigger}): {reason}")
            return False, reason

        max_s = self._cfg("max_pulse_seconds")
        with self._lock:
            if self._open:
                return False, "valve already open"
            self._current_event_id = db.start_watering_event(trigger, median)
            self._pulse_started = time.monotonic()
            self._drive_open()
            self._pulse_timer = threading.Timer(max_s, self._end_pulse,
                                                 kwargs={"reason": "completed"})
            self._pulse_timer.daemon = True
            self._pulse_timer.start()
        self._log(f"valve OPEN ({trigger}), max {max_s:.0f}s, "
                  f"median={median}")
        return True, "watering"

    def manual_pulse(self, seconds, median=None):
        """Manual watering: bypasses the soak lockout and daily cap (a manual
        press is a deliberate act), using the caller's requested duration. The
        ONLY limit is MANUAL_FAILSAFE_MAX_S, which guarantees the valve cannot
        stay open indefinitely if the process fails mid-pulse.
        Returns (started, reason)."""
        try:
            dur = float(seconds)
        except (TypeError, ValueError):
            dur = self._cfg("max_pulse_seconds")
        if dur <= 0:
            return False, "duration must be positive"
        capped = min(dur, MANUAL_FAILSAFE_MAX_S)

        with self._lock:
            if self._open:
                return False, "valve already open"
            self._current_event_id = db.start_watering_event("manual", median)
            self._pulse_started = time.monotonic()
            self._drive_open()
            self._pulse_timer = threading.Timer(capped, self._end_pulse,
                                                 kwargs={"reason": "completed"})
            self._pulse_timer.daemon = True
            self._pulse_timer.start()
        if capped < dur:
            self._log(f"valve OPEN (manual), requested {dur:.0f}s capped to "
                      f"failsafe {capped:.0f}s")
        else:
            self._log(f"valve OPEN (manual), {capped:.0f}s")
        return True, f"watering {capped:.0f}s"

    def _end_pulse(self, reason="completed"):
        with self._lock:
            if not self._open:
                return
            self._drive_closed()
            dur = (time.monotonic() - self._pulse_started
                   if self._pulse_started else None)
            if self._current_event_id is not None:
                db.end_watering_event(
                    self._current_event_id,
                    round(dur, 1) if dur else None,
                    reason,
                )
            self._current_event_id = None
            self._pulse_started = None
        self._log(f"valve CLOSED ({reason}, {dur:.1f}s)" if dur
                  else f"valve CLOSED ({reason})")

    def close_now(self, reason="manual_close"):
        """Force-close immediately (used by shutdown/reset and as a failsafe)."""
        if self._pulse_timer:
            self._pulse_timer.cancel()
        self._end_pulse(reason=reason)

    def is_open(self):
        with self._lock:
            return self._open

    def status(self):
        locked, secs = self.in_lockout()
        return {
            "open": self.is_open(),
            "in_lockout": locked,
            "lockout_remaining_s": round(secs),
            "pulses_today": db.pulses_today(),
            "daily_cap": int(self._cfg("daily_pulse_cap", int)),
            "max_pulse_seconds": self._cfg("max_pulse_seconds"),
        }


def _test():
    """Bench test with the valve wired but out of water."""
    print("valve controller standalone test")
    vc = ValveController(use_hardware=True)
    print("status:", vc.status())

    ok, reason = vc.can_water()
    print(f"can_water: {ok} ({reason})")
    if not ok:
        print("gated, not pulsing. clear lockout/cap to test a pulse.")
        return

    print("firing a 5s test pulse (overriding max to 5s via config)...")
    db.set_config_value("max_pulse_seconds", "5")
    started, msg = vc.pulse("manual", median=None)
    print(f"pulse: {started} ({msg})")
    if started:
        for i in range(7):
            print(f"  t+{i}s open={vc.is_open()}")
            time.sleep(1)
    print("final status:", vc.status())
    db.set_config_value("max_pulse_seconds", "30")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        _test()
    else:
        print(__doc__)
