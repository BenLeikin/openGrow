# openGardener

Outdoor Raspberry Pi strawberry-garden monitor and automated watering system.
Two beds of three day-neutral varieties each (San Andreas, Sequoia, Albion),
with per-planter soil moisture and temperature, per-bed air and light, a
solenoid-valve drip system with safety-limited automatic watering, a web
dashboard, weather-based rain-skip, and Discord alerts.

## Hardware

- Raspberry Pi 3B (`Strawberry`), running the logger + Flask dashboard
- 6x HW-390 capacitive soil moisture sensors via 2x ADS1115 ADC (0x48, 0x49)
- 6x DS18B20 soil temperature probes on a shared 1-Wire bus (GPIO4)
- 2x BME280 air temp/humidity/pressure (0x76, 0x77), one per bed
- 2x BH1750 light (0x23, 0x5C), one per bed
- 12V NC solenoid valve via relay on GPIO18 (active-high, fail-closed)
- Shutdown/reset buttons + status LEDs

## Software layout

Hardware is owned by a single process (the logger); the web app never touches
GPIO -- it writes commands/config to SQLite and the logger acts on them.

- `garden_bringup.py` -- bench tool: scan, calibrate, log
- `opengardener_initdb.py` -- SQLite schema init (safe to re-run)
- `opengardener_db.py` -- shared DB access layer
- `opengardener_log.py` -- reading -> DB bridge
- `opengardener_valve.py` -- valve controller + all safety limits
- `opengardener_autowater.py` -- auto-water decision logic
- `opengardener_weather.py` -- NWS forecast + rain-skip
- `opengardener_alerts.py` -- Discord alert conditions + debounce
- `discord_alert.py` -- Discord webhook transport
- `opengardener_logger.py` -- integrated hardware owner (main loop)
- `opengardener_web.py` -- Flask dashboard + API
- `opengardener_buttons.py` -- shutdown/reset button handler
- `static/`, `templates/` -- dashboard front end

## Services (systemd)

- `opengardener-logger.service` -- the hardware owner / data collector
- `opengardener-web.service` -- gunicorn-served Flask dashboard
- `opengardener-backup.service` + `.timer` -- nightly SQLite backup to NAS

## Setup notes

- Python venv at `~/garden` (created `--system-site-packages` for gpiozero/lgpio)
- Discord webhook goes in `~/.discord_webhook` (chmod 600), NOT in git
- Run `fetch_vendor.sh` to self-host chart libraries + fonts
- Dashboard is exposed via nginx with HTTP Basic Auth over TLS

## Safety model

The valve controller enforces, in the logger process where nothing can bypass
it: a max pulse length, a soak lockout after each pulse, and a daily pulse cap.
The valve always asserts closed on startup; safety counters survive restarts.
Auto-water ships disabled and logs its decisions until armed.
