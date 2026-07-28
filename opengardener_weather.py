#!/usr/bin/env python3
"""openGardener weather / rain-skip.

Uses the US National Weather Service API (api.weather.gov) -- free, no key,
US-only. Two steps: lat/lon -> gridpoint (cached to disk, rarely changes),
then gridpoint -> hourly forecast (cached ~30 min).

The auto-water evaluator calls should_skip() before firing: if enough rain is
forecast soon, watering is skipped so we don't water right before rain.

Config keys (editable on the dashboard):
  weather_enabled       "1"/"0"  -- master switch for rain-skip
  rain_skip_inches      float    -- skip if >= this much rain forecast
  rain_skip_hours       int      -- ...within this many hours ahead
  latitude, longitude   float    -- location (defaults to Thousand Oaks, CA)

Location default is Thousand Oaks, CA. Change via config if needed.
"""

import json
import os
import time
import urllib.request
from datetime import datetime, timezone

import opengardener_db as db

GRID_CACHE = os.path.expanduser("~/.opengardener_nws_grid.json")
FCAST_CACHE = os.path.expanduser("~/.opengardener_nws_forecast.json")
FCAST_TTL = 1800  # seconds; NWS hourly updates roughly every hour

DEFAULT_LAT = 34.1706
DEFAULT_LON = -118.8376  # Thousand Oaks, CA

UA = "openGardener/1.0 (opengardener.pilg0re.net)"


def _get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/geo+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def _latlon():
    lat = db.get_config_value("latitude")
    lon = db.get_config_value("longitude")
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return DEFAULT_LAT, DEFAULT_LON


def _grid(lat, lon):
    """Resolve lat/lon to an NWS hourly-forecast URL, cached to disk."""
    try:
        with open(GRID_CACHE) as f:
            c = json.load(f)
        if c.get("lat") == lat and c.get("lon") == lon and c.get("url"):
            return c["url"]
    except Exception:
        pass
    meta = _get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
    url = meta["properties"]["forecastHourly"]
    try:
        with open(GRID_CACHE, "w") as f:
            json.dump({"lat": lat, "lon": lon, "url": url}, f)
    except Exception:
        pass
    return url


def _forecast():
    """Hourly forecast periods, cached ~30 min. Returns list or None."""
    try:
        with open(FCAST_CACHE) as f:
            c = json.load(f)
        if time.time() - c.get("fetched", 0) < FCAST_TTL:
            return c["periods"]
    except Exception:
        pass
    try:
        lat, lon = _latlon()
        url = _grid(lat, lon)
        data = _get_json(url)
        periods = data["properties"]["periods"]
        with open(FCAST_CACHE, "w") as f:
            json.dump({"fetched": time.time(), "periods": periods}, f)
        return periods
    except Exception as e:
        print(f"weather fetch failed: {e}")
        return None


def rain_outlook(hours):
    """Total forecast precip (inches) and max probability over the next N
    hours. Returns (inches, max_prob_pct, summary) or (None, None, None)."""
    periods = _forecast()
    if not periods:
        return None, None, None
    now = datetime.now(timezone.utc)
    total_in = 0.0
    max_prob = 0
    used = 0
    for p in periods:
        try:
            start = datetime.fromisoformat(p["startTime"])
        except Exception:
            continue
        ahead = (start - now).total_seconds() / 3600.0
        if ahead < -1 or ahead > hours:
            continue
        used += 1
        # NWS gives probabilityOfPrecipitation (%) and, in newer feeds,
        # quantitativePrecipitation. Amount isn't always present in the hourly
        # feed, so fall back to probability-only if needed.
        prob = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        max_prob = max(max_prob, prob)
        qpf = (p.get("quantitativePrecipitation") or {}).get("value")
        if qpf is not None:
            total_in += qpf / 25.4  # mm -> inches
    if used == 0:
        return None, None, None
    summary = f"{total_in:.2f}in, up to {max_prob}% chance, next {hours}h"
    return round(total_in, 2), max_prob, summary


def should_skip():
    """Decide whether to skip an auto-water pulse due to forecast rain.
    Returns (skip: bool, reason: str)."""
    if db.get_config_value("weather_enabled", "0") != "1":
        return False, "weather-skip disabled"

    hours = int(float(db.get_config_value("rain_skip_hours") or 12))
    thresh_in = float(db.get_config_value("rain_skip_inches") or 0.1)

    inches, prob, summary = rain_outlook(hours)
    if inches is None:
        # No forecast available -> don't skip; better to water than to withhold
        # on missing data.
        return False, "no forecast available"

    if inches >= thresh_in:
        return True, f"rain expected ({summary})"
    # If amount data is absent but probability is very high, skip on that too.
    if inches == 0.0 and prob is not None and prob >= 70:
        return True, f"high rain chance ({prob}%, next {hours}h)"
    return False, f"insufficient rain forecast ({summary})"


def daily_forecast():
    """Twice-daily (day/night) forecast periods for the week-ahead strip.
    Returns list of period dicts or None. Cached alongside the hourly feed."""
    cache = os.path.expanduser("~/.opengardener_nws_daily.json")
    try:
        with open(cache) as f:
            c = json.load(f)
        if time.time() - c.get("fetched", 0) < FCAST_TTL:
            return c["periods"]
    except Exception:
        pass
    try:
        lat, lon = _latlon()
        # the daily forecast URL is a sibling of the hourly one
        meta = _get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
        url = meta["properties"]["forecast"]
        data = _get_json(url)
        periods = data["properties"]["periods"]
        with open(cache, "w") as f:
            json.dump({"fetched": time.time(), "periods": periods}, f)
        return periods
    except Exception as e:
        print(f"daily forecast fetch failed: {e}")
        return None


if __name__ == "__main__":
    lat, lon = _latlon()
    print(f"location: {lat}, {lon}")
    inches, prob, summary = rain_outlook(
        int(float(db.get_config_value("rain_skip_hours") or 12)))
    print(f"outlook: {summary}")
    skip, reason = should_skip()
    print(f"should_skip: {skip} ({reason})")
