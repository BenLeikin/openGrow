#!/usr/bin/env python3
"""
openGardener button handling.

Two momentary buttons to ground, internal pull-ups:
  GPIO17 (pin 11)  shutdown - hold 2s -> stop watering, close valve, power off
  GPIO27 (pin 13)  reset    - hold 2s -> stop watering, close valve, reboot

Two status LEDs (each with its own series resistor):
  GPIO22 (pin 15)  heartbeat - slow blink means the handler is alive
  GPIO23 (pin 16)  activity  - lights while a button is held

Both actions close the valve BEFORE going down. Normally-closed hardware
covers full power loss, but a clean reboot/halt should still explicitly stop
watering and close the valve so the system never transitions through an
undefined state mid-pulse. This is why buttons live with the logger (which
owns the valve) rather than in a standalone script.

Power actions use `systemctl poweroff` / `systemctl reboot`, authorized for
user 'ben' by the scoped polkit rule (49-opengardener-power.rules). No sudo,
no root process required.

Designed to be driven by the logger. The logger passes in two callables:
  stop_watering()  - halt any in-progress pulse, mark valve closed in state
  close_valve()    - drive the relay to the closed (de-energized) position
If run standalone (no callables), it uses safe no-op stubs and just logs.
"""

import subprocess
import threading
import time

from gpiozero import Button, LED

SHUTDOWN_PIN = 17
RESET_PIN = 27
HEARTBEAT_LED_PIN = 22
ACTIVITY_LED_PIN = 23
HOLD_SECONDS = 2.0


class ButtonHandler:
    def __init__(self, stop_watering=None, close_valve=None, logger=None):
        # Hooks the logger provides. Safe stubs if run standalone.
        self._stop_watering = stop_watering or (lambda: None)
        self._close_valve = close_valve or (lambda: None)
        self._log = logger or print

        self.shutdown_btn = Button(
            SHUTDOWN_PIN, pull_up=True, hold_time=HOLD_SECONDS
        )
        self.reset_btn = Button(
            RESET_PIN, pull_up=True, hold_time=HOLD_SECONDS
        )
        self.heartbeat = LED(HEARTBEAT_LED_PIN)
        self.activity = LED(ACTIVITY_LED_PIN)

        self.shutdown_btn.when_pressed = self.activity.on
        self.shutdown_btn.when_released = self.activity.off
        self.reset_btn.when_pressed = self.activity.on
        self.reset_btn.when_released = self.activity.off

        self.shutdown_btn.when_held = self._on_shutdown
        self.reset_btn.when_held = self._on_reset

        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True
        )

    def _make_safe(self):
        """Stop watering and close the valve before any power transition."""
        try:
            self._stop_watering()
            self._close_valve()
            self._log("valve closed, watering stopped ahead of power action")
        except Exception as e:
            # Never let a hook error block the power action; the NC valve
            # closes on power loss regardless.
            self._log(f"warning: pre-power safe step failed: {e}")

    def _on_shutdown(self):
        self.activity.on()
        self._log("shutdown held, making safe then powering off")
        self._make_safe()
        subprocess.run(["systemctl", "poweroff"])

    def _on_reset(self):
        self.activity.on()
        self._log("reset held, making safe then rebooting")
        self._make_safe()
        subprocess.run(["systemctl", "reboot"])

    def _heartbeat_loop(self):
        while not self._hb_stop.is_set():
            self.heartbeat.on()
            time.sleep(0.1)
            self.heartbeat.off()
            self._hb_stop.wait(1.9)

    def start(self):
        """Begin the heartbeat. Button callbacks fire on gpiozero's threads."""
        self._hb_thread.start()
        self._log("button handler running (shutdown GPIO17, reset GPIO27)")

    def stop(self):
        self._hb_stop.set()
        self.heartbeat.off()
        self.activity.off()


def main():
    """Standalone mode for testing the buttons and LEDs (no valve hooks)."""
    handler = ButtonHandler()
    handler.start()
    print("standalone test mode: hold a button 2s to see it fire")
    print("(power actions will run for real if held; ctrl-c to exit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        handler.stop()
        print("\nstopped")


if __name__ == "__main__":
    main()
