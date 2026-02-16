"""Weather module — Open-Meteo API.

No API key needed. Caches results for 15 minutes.
Set STREAMDECK_LAT / STREAMDECK_LON env vars for your location.
"""

import json
import os
import ssl
import time
import urllib.request

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

LAT = float(os.environ.get("STREAMDECK_LAT", "40.71"))
LON = float(os.environ.get("STREAMDECK_LON", "-74.00"))
CACHE_TTL = 900  # 15 minutes

_cache: dict | None = None
_cache_time: float = 0

# WMO weather code → icon label
_WMO_ICONS = {
    0: "SUN", 1: "SUN",
    2: "CLOUD", 3: "CLOUD",
    45: "FOG", 48: "FOG",
    51: "RAIN", 53: "RAIN", 55: "RAIN",
    56: "RAIN", 57: "RAIN",
    61: "RAIN", 63: "RAIN", 65: "RAIN",
    66: "RAIN", 67: "RAIN",
    71: "SNOW", 73: "SNOW", 75: "SNOW", 77: "SNOW",
    80: "SHOWER", 81: "SHOWER", 82: "SHOWER",
    85: "SNOW", 86: "SNOW",
    95: "STORM", 96: "STORM", 99: "STORM",
}

API_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,weather_code"
    "&timezone=Europe/Istanbul"
)


def get_weather() -> dict:
    """Return current weather for Gazipasa.

    Returns dict with keys: temp, humidity, wind, code, icon.
    Caches for 15 min. Returns stale data on failure.
    """
    global _cache, _cache_time

    now = time.time()
    if _cache and (now - _cache_time) < CACHE_TTL:
        return _cache

    try:
        req = urllib.request.Request(API_URL, headers={"User-Agent": "StreamDeck/1.0"})
        with urllib.request.urlopen(req, timeout=5, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
        cur = data["current"]
        code = int(cur.get("weather_code", 0))
        result = {
            "temp": cur.get("temperature_2m", 0),
            "humidity": cur.get("relative_humidity_2m", 0),
            "wind": cur.get("wind_speed_10m", 0),
            "code": code,
            "icon": _WMO_ICONS.get(code, "?"),
        }
        _cache = result
        _cache_time = now
        return result
    except Exception:
        # Return stale cache or defaults
        if _cache:
            return _cache
        return {"temp": 0, "humidity": 0, "wind": 0, "code": 0, "icon": "?"}
