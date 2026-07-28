#!/usr/bin/env python3
"""
openGardener auto-water decision logic.

Evaluated once per logger cycle. Decides whether to fire a watering pulse based
on the median in-band soil moisture versus the configured threshold, gated by
every safety limit in the valve controller.

Ships DISABLED: when auto_water_enabled is "0" the evaluator still computes and
logs what it *would* do ("would water: median 47% <= 50%") without touching the
valve. That lets you watch the decision against real dry-down for a few days
before arming it, and confirm the threshold is right.

The threshold is field-capacity anchored (100% = saturated planter soil), so
the strawberry rule of watering at ~50% of field capacity maps directly to a
threshold of 50 on this scale.
"""

from datetime import datetime

import opengardener_db as db

# Same in-band range the dashboard uses: readings outside this are treated as a
# disconnected/faulty sensor and excluded from the median so one bad probe
# can't force (or suppress) watering.
SOIL_BAND = (-5.0, 105.0)


def evaluate(valve, logger=None):
    """Decide and possibly act. `valve` is a ValveController. Returns a dict
    describing the decision (also useful for the dashboard/status)."""
    log = logger or print

    median, n = db.median_soil_moisture(band=SOIL_BAND)
    enabled = db.get_config_value("auto_water_enabled", "0") == "1"
    thr_raw = db.get_config_value("threshold_pct", "")
    threshold = float(thr_raw) if thr_raw not in (None, "") else None

    decision = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "median": median,
        "n_sensors": n,
        "threshold": threshold,
        "auto_enabled": enabled,
        "action": "none",
        "reason": "",
    }

    # No data or no threshold: nothing to decide on.
    if median is None:
        decision["reason"] = "no in-band soil readings"
        return decision
    if threshold is None:
        decision["reason"] = "no threshold set"
        return decision

    # Is the soil dry enough to want water?
    wants_water = median <= threshold
    if not wants_water:
        decision["reason"] = f"median {median:.0f}% above threshold {threshold:.0f}%"
        return decision

    # It's dry enough. Would we be allowed to water right now?
    ok, gate_reason = valve.can_water()

    if not enabled:
        # Disabled: log the counterfactual, don't act.
        decision["action"] = "would_water" if ok else "would_water_but_gated"
        decision["reason"] = (
            f"median {median:.0f}% <= {threshold:.0f}% "
            f"({'clear' if ok else gate_reason}); auto-water OFF"
        )
        log(f"auto-water OFF: {decision['reason']}")
        return decision

    # Enabled and dry: check weather before firing.
    import opengardener_weather as weather
    skip, wreason = weather.should_skip()
    if skip:
        decision["action"] = "skipped_rain"
        decision["reason"] = wreason
        log(f"auto-water skipped: {wreason}")
        # Optional Discord note so you know why it didn't water.
        try:
            import discord_alert
            discord_alert.send("Watering skipped",
                               f"Auto-water held: {wreason}. "
                               f"Median soil {median:.0f}%.",
                               level="info")
        except Exception:
            pass
        return decision

    # Fire if the valve gates allow.
    if not ok:
        decision["action"] = "gated"
        decision["reason"] = gate_reason
        log(f"auto-water gated: {gate_reason}")
        return decision

    started, msg = valve.pulse("auto", median=median)
    decision["action"] = "watered" if started else "gated"
    decision["reason"] = msg
    log(f"auto-water: {msg} (median {median:.0f}% <= {threshold:.0f}%)")
    return decision


if __name__ == "__main__":
    # Dry-run against the DB with a stub valve that never actually waters,
    # so you can see what the evaluator decides right now.
    class StubValve:
        def can_water(self):
            return True, "ok (stub)"
        def pulse(self, trigger, median=None):
            return False, "stub valve, no action"

    d = evaluate(StubValve())
    for k, v in d.items():
        print(f"  {k}: {v}")
