"""Air quality & environment module — local sensor + Open-Meteo APIs.

Fetches:
- Local: PM2.5, PM10, temperature, humidity, pressure from Luftdaten sensor
- Remote: UV index, European AQI, sea waves from Open-Meteo

Caches results for 5 min (local) / 15 min (remote).
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

# ── config ────────────────────────────────────────────────────────────

LOCAL_SENSOR_MDNS = os.environ.get("STREAMDECK_SENSOR_MDNS", "")
LOCAL_SENSOR_FALLBACK_IP = os.environ.get("STREAMDECK_SENSOR_IP", "")
LOCAL_CACHE_TTL = 300  # 5 minutes
REMOTE_CACHE_TTL = 900  # 15 minutes

LAT = float(os.environ.get("STREAMDECK_LAT", "40.71"))
LON = float(os.environ.get("STREAMDECK_LON", "-74.00"))

AIR_QUALITY_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    f"?latitude={LAT}&longitude={LON}"
    "&current=pm2_5,pm10,european_aqi,uv_index"
    "&timezone=Europe/Istanbul"
)

MARINE_URL = (
    "https://marine-api.open-meteo.com/v1/marine"
    f"?latitude={LAT}&longitude={LON}"
    "&current=wave_height,wave_period,wave_direction,swell_wave_height"
    "&timezone=Europe/Istanbul"
)

# ── color scales ──────────────────────────────────────────────────────

def pm25_color(val: float) -> tuple[str, str, str]:
    """Return (bg, value_color, label) based on PM2.5 level (US AQI breakpoints)."""
    if val <= 12:
        return "#052e16", "#4ade80", "GOOD"
    if val <= 35:
        return "#422006", "#fbbf24", "MODERATE"
    if val <= 55:
        return "#431407", "#fb923c", "SENSITIVE"
    if val <= 150:
        return "#450a0a", "#f87171", "UNHEALTHY"
    if val <= 250:
        return "#3b0764", "#c084fc", "VERY BAD"
    return "#4a0404", "#ff4444", "HAZARD"


def pm10_color(val: float) -> tuple[str, str, str]:
    """Return (bg, value_color, label) based on PM10 level."""
    if val <= 54:
        return "#052e16", "#4ade80", "GOOD"
    if val <= 154:
        return "#422006", "#fbbf24", "MODERATE"
    if val <= 254:
        return "#431407", "#fb923c", "SENSITIVE"
    if val <= 354:
        return "#450a0a", "#f87171", "UNHEALTHY"
    return "#3b0764", "#c084fc", "VERY BAD"


def uv_color(val: float) -> tuple[str, str, str]:
    """Return (bg, value_color, label) based on UV index."""
    if val <= 2:
        return "#052e16", "#4ade80", "LOW"
    if val <= 5:
        return "#422006", "#fbbf24", "MODERATE"
    if val <= 7:
        return "#431407", "#fb923c", "HIGH"
    if val <= 10:
        return "#450a0a", "#f87171", "VERY HIGH"
    return "#3b0764", "#c084fc", "EXTREME"


def aqi_color(val: int) -> tuple[str, str, str]:
    """Return (bg, value_color, label) based on European AQI."""
    if val <= 20:
        return "#052e16", "#4ade80", "GOOD"
    if val <= 40:
        return "#0a3622", "#86efac", "FAIR"
    if val <= 60:
        return "#422006", "#fbbf24", "MODERATE"
    if val <= 80:
        return "#431407", "#fb923c", "POOR"
    if val <= 100:
        return "#450a0a", "#f87171", "V.POOR"
    return "#3b0764", "#c084fc", "HAZARD"


def wave_color(height: float) -> str:
    """Wave height → color."""
    if height <= 0.5:
        return "#38bdf8"  # calm - light blue
    if height <= 1.0:
        return "#4ade80"  # mild - green
    if height <= 2.0:
        return "#fbbf24"  # moderate - yellow
    if height <= 3.0:
        return "#fb923c"  # rough - orange
    return "#f87171"  # storm - red


# ── wave direction ────────────────────────────────────────────────────

def _deg_to_arrow(deg: float) -> str:
    """Convert degrees to arrow character."""
    arrows = ["↓", "↙", "←", "↖", "↑", "↗", "→", "↘"]
    idx = int((deg + 22.5) / 45) % 8
    return arrows[idx]


# ── data fetching ─────────────────────────────────────────────────────

_local_cache: dict | None = None
_local_cache_time: float = 0
_local_resolved_ip: str | None = None  # cached resolved IP
_remote_cache: dict | None = None
_remote_cache_time: float = 0


def _fetch_json(url: str, timeout: int = 5) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "StreamDeck/1.0"})
    ctx = _SSL_CTX if url.startswith("https") else None
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return json.loads(resp.read())


def _resolve_sensor() -> str | None:
    """Resolve sensor IP: use cached IP, or mDNS, or fallback."""
    global _local_resolved_ip
    import socket

    if _local_resolved_ip:
        return _local_resolved_ip

    # Resolve via mDNS
    if LOCAL_SENSOR_MDNS:
        try:
            ip = socket.gethostbyname(LOCAL_SENSOR_MDNS)
            _local_resolved_ip = ip
            return ip
        except Exception:
            pass

    # Fallback to configured IP
    if LOCAL_SENSOR_FALLBACK_IP:
        return LOCAL_SENSOR_FALLBACK_IP

    return None


def fetch_local() -> dict:
    """Fetch data from local Luftdaten sensor (auto-discovers IP).

    Returns dict with: pm25, pm10, temp, humidity, pressure.
    """
    global _local_cache, _local_cache_time, _local_resolved_ip

    now = time.time()
    if _local_cache and (now - _local_cache_time) < LOCAL_CACHE_TTL:
        return _local_cache

    try:
        ip = _resolve_sensor()
        if not ip:
            raise ConnectionError("No sensor configured")
        data = _fetch_json(f"http://{ip}/data.json", timeout=3)
        values = {v["value_type"]: float(v["value"]) for v in data.get("sensordatavalues", [])}
        result = {
            "pm25": values.get("SDS_P2", 0),
            "pm10": values.get("SDS_P1", 0),
            "temp": values.get("BME280_temperature", 0),
            "humidity": values.get("BME280_humidity", 0),
            "pressure": values.get("BME280_pressure", 0) / 100,  # Pa → hPa
            "online": True,
        }
        _local_cache = result
        _local_cache_time = now
        return result
    except Exception:
        _local_resolved_ip = None  # re-resolve on next attempt
        if _local_cache:
            return {**_local_cache, "online": False}
        return {"pm25": 0, "pm10": 0, "temp": 0, "humidity": 0, "pressure": 0, "online": False}


def fetch_remote() -> dict:
    """Fetch UV, AQI, and marine data from Open-Meteo.

    Returns dict with: uv_index, aqi, pm25_out, pm10_out,
                       wave_height, wave_period, wave_dir, swell_height.
    """
    global _remote_cache, _remote_cache_time

    now = time.time()
    if _remote_cache and (now - _remote_cache_time) < REMOTE_CACHE_TTL:
        return _remote_cache

    result = {
        "uv_index": 0, "aqi": 0, "pm25_out": 0, "pm10_out": 0,
        "wave_height": 0, "wave_period": 0, "wave_dir": 0, "swell_height": 0,
        "online": False,
    }

    try:
        aq = _fetch_json(AIR_QUALITY_URL)
        cur = aq.get("current", {})
        result["uv_index"] = cur.get("uv_index", 0) or 0
        result["aqi"] = int(cur.get("european_aqi", 0) or 0)
        result["pm25_out"] = cur.get("pm2_5", 0) or 0
        result["pm10_out"] = cur.get("pm10", 0) or 0
        result["online"] = True
    except Exception:
        pass

    try:
        marine = _fetch_json(MARINE_URL)
        cur = marine.get("current", {})
        result["wave_height"] = cur.get("wave_height", 0) or 0
        result["wave_period"] = cur.get("wave_period", 0) or 0
        result["wave_dir"] = cur.get("wave_direction", 0) or 0
        result["swell_height"] = cur.get("swell_wave_height", 0) or 0
        result["online"] = True
    except Exception:
        pass

    if result["online"]:
        _remote_cache = result
        _remote_cache_time = now

    return result
