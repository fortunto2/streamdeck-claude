"""Stream Deck Home Dashboard — personal assistant with live info.

Home screen with clock, weather, crypto prices, section navigation,
and favorite game launchers. Auto-detects USB device on plug-in.

Usage:
    uv run python scripts/dashboard.py
"""

import importlib
import os
import sys
import threading
import time

# Load .env from project root before other imports
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass

_load_dotenv()

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

import activity
import airquality
import pomodoro
import sound_engine
import weather

# ── config ────────────────────────────────────────────────────────────

SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
TICK_INTERVAL = 30.0  # live data refresh
SAVE_DIR = os.path.expanduser("~/.streamdeck-arcade")

# ── font helper ───────────────────────────────────────────────────────

def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


# ── game registry (mirrors arcade.py) ────────────────────────────────

GAMES = [
    {"title": "BEAVER",  "subtitle": "HUNT",     "bg": "#2d1b0e",  "script": "beaver_game",     "pos": 1},
    {"title": "SIMON",   "subtitle": "SAYS",     "bg": "#4c1d95",  "script": "simon_game",      "pos": 2},
    {"title": "REACT",   "subtitle": "SPEED",    "bg": "#065f46",  "script": "reaction_game",   "pos": 3},
    {"title": "SNAKE",   "subtitle": "GAME",     "bg": "#14532d",  "script": "snake_game",      "pos": 4},
    {"title": "MEMORY",  "subtitle": "MATCH",    "bg": "#1e3a5f",  "script": "memory_game",     "pos": 5},
    {"title": "BREAK",   "subtitle": "OUT",      "bg": "#92400e",  "script": "breakout_game",   "pos": 6},
    {"title": "CHIMP",   "subtitle": "TEST",     "bg": "#991b1b",  "script": "sequence_game",   "pos": 7},
    {"title": "N-BACK",  "subtitle": "IQ",       "bg": "#1e3a5f",  "script": "nback_game",      "pos": 8},
    {"title": "PATTERN", "subtitle": "LOGIC",    "bg": "#7c3aed",  "script": "pattern_game",    "pos": 9},
    {"title": "MATH",    "subtitle": "SEQ",      "bg": "#0c4a6e",  "script": "mathseq_game",    "pos": 10},
    {"title": "QUICK",   "subtitle": "MATH",     "bg": "#166534",  "script": "quickmath_game",  "pos": 11},
    {"title": "NUM",     "subtitle": "GRID",     "bg": "#4c1d95",  "script": "numgrid_game",    "pos": 12},
    {"title": "BUNNY",   "subtitle": "BLITZ",    "bg": "#065f46",  "script": "bunny_game",      "pos": 13},
    {"title": "SPACE",   "subtitle": "INVADERS", "bg": "#1e1b4b",  "script": "invaders_game",   "pos": 14},
    {"title": "LIGHTS",  "subtitle": "OUT",      "bg": "#854d0e",  "script": "lights_game",     "pos": 16},
    {"title": "DODGE",   "subtitle": "METEORS",  "bg": "#7f1d1d",  "script": "dodge_game",      "pos": 17},
    {"title": "MINE",    "subtitle": "SWEEP",    "bg": "#374151",  "script": "mines_game",      "pos": 18},
    {"title": "COLONY",  "subtitle": "BUILD",    "bg": "#ca8a04",  "script": "colony_game",     "pos": 19},
    {"title": "DUNGEON", "subtitle": "CRAWL",    "bg": "#7c3aed",  "script": "dungeon_game",    "pos": 20},
    {"title": "FACTORY", "subtitle": "CHAIN",    "bg": "#ea580c",  "script": "factory_game",    "pos": 21},
    {"title": "TOWER",   "subtitle": "DEFENSE",  "bg": "#15803d",  "script": "tower_game",      "pos": 22},
    {"title": "SPACE",   "subtitle": "TRADER",   "bg": "#1e3a5f",  "script": "trader_game",     "pos": 23},
    {"title": "MINI",    "subtitle": "EMPIRE",   "bg": "#92400e",  "script": "empire_game",     "pos": 24},
    {"title": "CRYPTO",  "subtitle": "TYCOON",   "bg": "#166534",  "script": "crypto_game",     "pos": 25},
    {"title": "CRYPTO",  "subtitle": "REAL",     "bg": "#0ea5e9",  "script": "crypto_real_game","pos": 26},
    {"title": "DJ",      "subtitle": "BEATS",    "bg": "#7c3aed",  "script": "sequencer_game", "pos": 27},
]

CLASS_MAP = {
    "beaver_game": "BeaverGame",
    "simon_game": "SimonGame",
    "reaction_game": "ReactionGame",
    "snake_game": "SnakeGame",
    "memory_game": "MemoryGame",
    "invaders_game": "InvadersGame",
    "breakout_game": "BreakoutGame",
    "sequence_game": "SequenceGame",
    "nback_game": "NBackGame",
    "pattern_game": "PatternGame",
    "mathseq_game": "MathSeqGame",
    "quickmath_game": "QuickMathGame",
    "numgrid_game": "NumGridGame",
    "bunny_game": "BunnyGame",
    "lights_game": "LightsGame",
    "dodge_game": "DodgeGame",
    "mines_game": "MinesGame",
    "colony_game": "ColonyGame",
    "dungeon_game": "DungeonGame",
    "factory_game": "FactoryGame",
    "tower_game": "TowerGame",
    "trader_game": "TraderGame",
    "empire_game": "EmpireGame",
    "crypto_game": "CryptoGame",
    "crypto_real_game": "CryptoRealGame",
    "sequencer_game": "SequencerGame",
}

# Build lookup: script_name -> game info
_GAME_BY_SCRIPT = {g["script"]: g for g in GAMES}


# ── renderers ─────────────────────────────────────────────────────────

def render_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_back(label="HOME", size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#374151")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "<< BACK", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 52), label, font=_font(14), fill="#9ca3af", anchor="mt")
    return img


def render_game_btn(title: str, subtitle: str, bg: str, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 30), title, font=_font(16), fill="white", anchor="mt")
    d.text((48, 54), subtitle, font=_font(11), fill="#d1d5db", anchor="mt")
    return img


def render_voice_btn(enabled: bool, size=SIZE) -> Image.Image:
    bg = "#065f46" if enabled else "#7f1d1d"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    state = "ON" if enabled else "OFF"
    color = "#34d399" if enabled else "#f87171"
    d.text((48, 30), "SOUND", font=_font(14), fill="white", anchor="mt")
    d.text((48, 54), state, font=_font(16), fill=color, anchor="mt")
    return img


def render_section_btn(title: str, subtitle: str, bg: str, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 30), title, font=_font(14), fill="white", anchor="mt")
    if subtitle:
        d.text((48, 52), subtitle, font=_font(11), fill="#d1d5db", anchor="mt")
    return img


def render_greyed(title: str, subtitle: str, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#1f2937")
    d = ImageDraw.Draw(img)
    d.text((48, 30), title, font=_font(14), fill="#6b7280", anchor="mt")
    if subtitle:
        d.text((48, 52), subtitle, font=_font(10), fill="#4b5563", anchor="mt")
    return img


# ── status bar renderers (row 0) ─────────────────────────────────────

def render_clock_digit(ch: str, sub: str = "", size=SIZE) -> Image.Image:
    """Single large digit for the 4-key clock. Optional small sub-text."""
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((48, 38), ch, font=_font(52), fill="white", anchor="mm")
    if sub:
        d.text((48, 78), sub, font=_font(11), fill="#64748b", anchor="mm")
    return img


def render_clock_keys(size=SIZE) -> list[Image.Image]:
    """Return 4 images for keys 0-3: H H : M M with date/day subtexts."""
    now = time.localtime()
    h = time.strftime("%H", now)
    m = time.strftime("%M", now)
    day = time.strftime("%a", now).upper()
    date_str = time.strftime("%d %b", now).upper()
    return [
        render_clock_digit(h[0]),
        render_clock_digit(h[1], day),
        render_clock_digit(m[0], date_str),
        render_clock_digit(m[1]),
    ]


_WEATHER_EMOJI = {
    "SUN": "O", "CLOUD": "~", "FOG": "=", "RAIN": "///",
    "SNOW": "*", "SHOWER": "||", "STORM": "!!", "?": "?",
}


def render_weather_key(w: dict, size=SIZE) -> Image.Image:
    bg = "#0f172a"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    icon = _WEATHER_EMOJI.get(w.get("icon", "?"), "?")
    temp = w.get("temp", 0)
    d.text((48, 18), icon, font=_font(18), fill="#fbbf24", anchor="mm")
    d.text((48, 48), f"{temp:.0f}°C", font=_font(18), fill="white", anchor="mm")
    d.text((48, 72), w.get("icon", ""), font=_font(10), fill="#64748b", anchor="mm")
    return img


def render_wind_hum_key(w: dict, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    wind = w.get("wind", 0)
    hum = w.get("humidity", 0)
    d.text((48, 22), f"{wind:.0f}", font=_font(18), fill="white", anchor="mm")
    d.text((48, 44), "km/h", font=_font(10), fill="#64748b", anchor="mm")
    d.text((48, 64), f"{hum}%", font=_font(14), fill="#38bdf8", anchor="mm")
    d.text((48, 80), "humid", font=_font(9), fill="#64748b", anchor="mm")
    return img


def _fmt_price(price: float) -> str:
    if price >= 10000:
        return f"{price/1000:.1f}K"
    if price >= 100:
        return f"{price:.0f}"
    if price >= 1:
        return f"{price:.2f}"
    return f"{price:.4f}"


def render_crypto_key(symbol: str, price: float, pct: float, size=SIZE) -> Image.Image:
    if pct > 0:
        bg = "#052e16"  # green tint
        pct_color = "#4ade80"
        sign = "+"
    elif pct < 0:
        bg = "#450a0a"  # red tint
        pct_color = "#f87171"
        sign = ""
    else:
        bg = "#0f172a"
        pct_color = "#94a3b8"
        sign = ""
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 16), symbol, font=_font(12), fill="#94a3b8", anchor="mm")
    d.text((48, 40), _fmt_price(price), font=_font(18), fill="white", anchor="mm")
    d.text((48, 66), f"{sign}{pct:.1f}%", font=_font(13), fill=pct_color, anchor="mm")
    return img



# ── air quality / environment renderers ──────────────────────────────

def render_pm25_key(val: float, online: bool = True, size=SIZE) -> Image.Image:
    bg, color, label = airquality.pm25_color(val)
    if not online:
        bg = "#1c1917"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "PM2.5", font=_font(11), fill="#94a3b8", anchor="mm")
    d.text((48, 42), f"{val:.1f}", font=_font(22), fill=color if online else "#6b7280", anchor="mm")
    d.text((48, 66), "µg/m³", font=_font(9), fill="#64748b", anchor="mm")
    d.text((48, 82), label, font=_font(10), fill=color if online else "#4b5563", anchor="mm")
    return img


def render_pm10_key(val: float, online: bool = True, size=SIZE) -> Image.Image:
    bg, color, label = airquality.pm10_color(val)
    if not online:
        bg = "#1c1917"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "PM10", font=_font(11), fill="#94a3b8", anchor="mm")
    d.text((48, 42), f"{val:.1f}", font=_font(22), fill=color if online else "#6b7280", anchor="mm")
    d.text((48, 66), "µg/m³", font=_font(9), fill="#64748b", anchor="mm")
    d.text((48, 82), label, font=_font(10), fill=color if online else "#4b5563", anchor="mm")
    return img


def render_indoor_key(temp: float, hum: float, online: bool = True, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((48, 14), "INDOOR", font=_font(10), fill="#94a3b8", anchor="mm")
    tc = "white" if online else "#6b7280"
    d.text((48, 38), f"{temp:.1f}°", font=_font(20), fill=tc, anchor="mm")
    hc = "#38bdf8" if online else "#6b7280"
    d.text((48, 62), f"{hum:.0f}%", font=_font(16), fill=hc, anchor="mm")
    d.text((48, 82), "humid", font=_font(9), fill="#64748b", anchor="mm")
    return img


def render_pressure_key(hpa: float, online: bool = True, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((48, 14), "PRESSURE", font=_font(9), fill="#94a3b8", anchor="mm")
    c = "white" if online else "#6b7280"
    d.text((48, 42), f"{hpa:.0f}", font=_font(22), fill=c, anchor="mm")
    d.text((48, 66), "hPa", font=_font(11), fill="#64748b", anchor="mm")
    return img


def render_uv_key(val: float, size=SIZE) -> Image.Image:
    bg, color, label = airquality.uv_color(val)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "UV INDEX", font=_font(9), fill="#94a3b8", anchor="mm")
    d.text((48, 42), f"{val:.1f}", font=_font(24), fill=color, anchor="mm")
    d.text((48, 70), label, font=_font(12), fill=color, anchor="mm")
    return img


def render_aqi_key(val: int, pm25: float, pm10: float, size=SIZE) -> Image.Image:
    bg, color, label = airquality.aqi_color(val)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 12), "AQI OUT", font=_font(9), fill="#94a3b8", anchor="mm")
    d.text((48, 36), str(val), font=_font(22), fill=color, anchor="mm")
    d.text((48, 58), label, font=_font(10), fill=color, anchor="mm")
    d.text((48, 76), f"{pm25:.0f}/{pm10:.0f}", font=_font(9), fill="#64748b", anchor="mm")
    d.text((48, 88), "pm2/10", font=_font(8), fill="#475569", anchor="mm")
    return img


def render_wave_key(height: float, period: float, direction: float, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0c1929")
    d = ImageDraw.Draw(img)
    wc = airquality.wave_color(height)
    arrow = airquality._deg_to_arrow(direction)
    d.text((48, 12), "SEA", font=_font(10), fill="#94a3b8", anchor="mm")
    d.text((48, 36), f"{height:.1f}m", font=_font(20), fill=wc, anchor="mm")
    d.text((48, 60), f"{period:.0f}s {arrow}", font=_font(13), fill="#38bdf8", anchor="mm")
    d.text((48, 80), f"{direction:.0f}°", font=_font(10), fill="#64748b", anchor="mm")
    return img


def render_swell_key(swell: float, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0c1929")
    d = ImageDraw.Draw(img)
    wc = airquality.wave_color(swell)
    d.text((48, 14), "SWELL", font=_font(10), fill="#94a3b8", anchor="mm")
    d.text((48, 42), f"{swell:.1f}m", font=_font(22), fill=wc, anchor="mm")
    d.text((48, 70), "height", font=_font(10), fill="#64748b", anchor="mm")
    return img


# ── activity / system renderers ──────────────────────────────────────

def _fmt_hm(seconds: int) -> str:
    """Format seconds as 'Xh Ym'."""
    h, m = divmod(seconds // 60, 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    return f"{m}m"


def _session_color(seconds: int) -> tuple[str, str]:
    """Return (bg, text_color) based on continuous work time."""
    if seconds < 45 * 60:
        return "#052e16", "#4ade80"  # green — fresh
    if seconds < 90 * 60:
        return "#422006", "#fbbf24"  # yellow — time for break soon
    if seconds < 120 * 60:
        return "#431407", "#fb923c"  # orange — should break
    return "#450a0a", "#f87171"  # red — overdue break!


def render_uptime_key(uptime_sec: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((48, 14), "UPTIME", font=_font(9), fill="#94a3b8", anchor="mm")
    d.text((48, 42), _fmt_hm(uptime_sec), font=_font(20), fill="white", anchor="mm")
    d.text((48, 66), "system", font=_font(9), fill="#64748b", anchor="mm")
    return img


def render_session_key(session_sec: int, size=SIZE) -> Image.Image:
    bg, color = _session_color(session_sec)
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "SESSION", font=_font(9), fill="#94a3b8", anchor="mm")
    d.text((48, 42), _fmt_hm(session_sec), font=_font(20), fill=color, anchor="mm")
    if session_sec < 45 * 60:
        label = "FRESH"
    elif session_sec < 90 * 60:
        label = "BREAK SOON"
    elif session_sec < 120 * 60:
        label = "BREAK!"
    else:
        label = "OVERDUE!"
    d.text((48, 68), label, font=_font(9), fill=color, anchor="mm")
    return img


def render_total_work_key(total_sec: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((48, 14), "TODAY", font=_font(9), fill="#94a3b8", anchor="mm")
    d.text((48, 42), _fmt_hm(total_sec), font=_font(20), fill="#38bdf8", anchor="mm")
    d.text((48, 66), "worked", font=_font(9), fill="#64748b", anchor="mm")
    return img


def render_idle_key(idle_sec: int, is_active: bool, size=SIZE) -> Image.Image:
    if is_active:
        bg = "#052e16"
        dot = "#4ade80"
        label = "ACTIVE"
    else:
        bg = "#1c1917"
        dot = "#6b7280"
        label = "IDLE"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    # Status dot
    d.ellipse([40, 8, 56, 24], fill=dot)
    d.text((48, 42), label, font=_font(13), fill="white" if is_active else "#9ca3af", anchor="mm")
    if idle_sec > 0:
        d.text((48, 62), _fmt_hm(idle_sec), font=_font(12), fill="#64748b", anchor="mm")
    d.text((48, 80), "input", font=_font(8), fill="#475569", anchor="mm")
    return img


# ── lightweight crypto fetcher ────────────────────────────────────────

class CryptoTicker:
    """Lightweight BTC+ETH price fetcher using ccxt."""

    SYMBOLS = ["BTC/USDT", "ETH/USDT"]

    def __init__(self):
        self.exchange = None
        self.prices = {}
        self.pct_24h = {}
        self.online = False
        self._init_exchange()

    def _init_exchange(self):
        try:
            import ccxt
            self.exchange = ccxt.binance({"enableRateLimit": True, "timeout": 5000})
            self.online = True
        except Exception:
            self.exchange = None
            self.online = False

    def fetch(self) -> bool:
        if not self.exchange:
            self._init_exchange()
            if not self.exchange:
                return False
        try:
            tickers = self.exchange.fetch_tickers(self.SYMBOLS)
            for sym in self.SYMBOLS:
                t = tickers.get(sym)
                if t:
                    self.prices[sym] = t.get("last", 0) or 0
                    self.pct_24h[sym] = t.get("percentage", 0) or 0
            self.online = True
            return True
        except Exception:
            self.online = False
            return False


# ── dashboard ─────────────────────────────────────────────────────────

BRIGHTNESS_LEVELS = [40, 60, 80, 100]


class Dashboard:
    def __init__(self, deck):
        self.deck = deck
        self.page = "home"  # home | games | agents
        self.active_game = None
        self.active_module = None
        self.lock = threading.Lock()
        self.tick_timer = None
        self.running = False
        self.start_time = time.time()

        # Brightness
        self.brightness_idx = 2  # start at 80%
        self.brightness = BRIGHTNESS_LEVELS[self.brightness_idx]

        # Data sources
        self.crypto = CryptoTicker()
        self.weather_data = {"temp": 0, "humidity": 0, "wind": 0, "code": 0, "icon": "?"}

        # Pomodoro timer (keys 24-27)
        self.pomo = pomodoro.Pomodoro(self.set_key)

        # Activity tracker (keys 28-31)
        self.activity_data = {"uptime_sec": 0, "idle_sec": 0, "session_sec": 0, "total_work_sec": 0, "is_active": False}

        # Health alert state
        self._health_alerting = False
        self._health_flash_on = True
        self._health_flash_timer: threading.Timer | None = None
        self._health_snooze_until = 0  # timestamp: suppress alerts until this time
        self._health_alert_threshold = 45 * 60  # 45 min continuous work
        self._health_snooze_duration = 15 * 60  # 15 min snooze

        # Air quality data
        self.local_sensor = {"pm25": 0, "pm10": 0, "temp": 0, "humidity": 0, "pressure": 0, "online": False}
        self.remote_env = {
            "uv_index": 0, "aqi": 0, "pm25_out": 0, "pm10_out": 0,
            "wave_height": 0, "wave_period": 0, "wave_dir": 0, "swell_height": 0, "online": False,
        }

        # Track weather refresh separately (every 15 min)
        self._last_weather = 0
        self._last_env_remote = 0
        self._tick_count = 0

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # ── pages ─────────────────────────────────────────────────────────

    def show_home(self):
        with self.lock:
            self.page = "home"
            self.active_game = None
            self.active_module = None
        self.deck.reset()
        self._render_status_bar()
        self._render_sections()
        self._render_env_row()
        # Row 3: pomodoro (24-27) + activity (28-31)
        self.pomo.start()
        try:
            self.activity_data = activity.get_activity()
        except Exception:
            pass
        self._render_activity_row()

    def _render_status_bar(self):
        """Row 0: big clock (keys 0-3) + weather + crypto (keys 4-7)."""
        digits = render_clock_keys()
        for i, img in enumerate(digits):
            self.set_key(i, img)

        self.set_key(4, render_weather_key(self.weather_data))
        self.set_key(5, render_wind_hum_key(self.weather_data))

        btc_price = self.crypto.prices.get("BTC/USDT", 0)
        btc_pct = self.crypto.pct_24h.get("BTC/USDT", 0)
        eth_price = self.crypto.prices.get("ETH/USDT", 0)
        eth_pct = self.crypto.pct_24h.get("ETH/USDT", 0)
        self.set_key(6, render_crypto_key("BTC", btc_price, btc_pct))
        self.set_key(7, render_crypto_key("ETH", eth_price, eth_pct))

    def _render_sections(self):
        """Row 1: section buttons."""
        n_games = len(GAMES)
        self.set_key(8, render_section_btn("GAMES", f"({n_games})", "#4c1d95"))
        self.set_key(9, render_section_btn("CRYPTO", "REAL", "#0ea5e9"))
        self.set_key(10, render_section_btn("CRYPTO", "SIM", "#166534"))
        self.set_key(11, render_section_btn("AGENTS", "", "#7c3aed"))
        self.set_key(12, render_greyed("CALENDAR", "SOON"))
        self.set_key(13, render_section_btn("SIRI", "", "#1d4ed8"))
        self.set_key(14, render_voice_btn(not sound_engine.global_mute))
        self.set_key(15, self._render_bright_btn())

    def _render_env_row(self):
        """Row 2 (keys 16-23): air quality + environment sensors."""
        ls = self.local_sensor
        re = self.remote_env
        on = ls.get("online", False)

        # Local sensor: PM2.5, PM10, indoor, pressure
        self.set_key(16, render_pm25_key(ls["pm25"], on))
        self.set_key(17, render_pm10_key(ls["pm10"], on))
        self.set_key(18, render_indoor_key(ls["temp"], ls["humidity"], on))
        self.set_key(19, render_pressure_key(ls["pressure"], on))

        # Remote: UV, AQI, sea
        self.set_key(20, render_uv_key(re["uv_index"]))
        self.set_key(21, render_aqi_key(re["aqi"], re["pm25_out"], re["pm10_out"]))
        self.set_key(22, render_wave_key(re["wave_height"], re["wave_period"], re["wave_dir"]))
        self.set_key(23, render_swell_key(re["swell_height"]))

    def _render_activity_row(self):
        """Keys 28-31: system activity (uptime, session, total, idle)."""
        a = self.activity_data
        self.set_key(28, render_uptime_key(a["uptime_sec"]))
        # Session key: normal or alert flash
        if not self._health_alerting:
            self.set_key(29, render_session_key(a["session_sec"]))
        self.set_key(30, render_total_work_key(a["total_work_sec"]))
        self.set_key(31, render_idle_key(a["idle_sec"], a["is_active"]))

    def _check_health_alert(self):
        """Check if we should trigger health alert (called every tick)."""
        session = self.activity_data.get("session_sec", 0)
        now = time.time()

        # Already alerting? keep going
        if self._health_alerting:
            return

        # In snooze? skip
        if now < self._health_snooze_until:
            return

        # Threshold reached — start flashing!
        if session >= self._health_alert_threshold:
            self._start_health_alert()

    def _start_health_alert(self):
        """Start flashing SESSION key with sound."""
        self._health_alerting = True
        self._health_flash_on = True
        # Play health sound from Silicon Valley pack
        import pomodoro
        pomodoro._play("health")
        self._health_flash_tick()

    def _health_flash_tick(self):
        """Flash the session key between alert and normal."""
        if not self._health_alerting or not self.running:
            return
        with self.lock:
            page = self.page
        if page != "home":
            return

        self._health_flash_on = not self._health_flash_on
        if self._health_flash_on:
            # Alert frame: bright red with message
            img = Image.new("RGB", SIZE, "#dc2626")
            d = ImageDraw.Draw(img)
            d.text((48, 18), "STAND", font=_font(16), fill="white", anchor="mm")
            d.text((48, 40), "UP!", font=_font(18), fill="white", anchor="mm")
            d.text((48, 62), "DRINK", font=_font(12), fill="#fef08a", anchor="mm")
            d.text((48, 78), "WATER", font=_font(12), fill="#fef08a", anchor="mm")
        else:
            # Dark frame
            img = Image.new("RGB", SIZE, "#450a0a")
            d = ImageDraw.Draw(img)
            session = self.activity_data.get("session_sec", 0)
            d.text((48, 30), _fmt_hm(session), font=_font(18), fill="#f87171", anchor="mm")
            d.text((48, 55), "TAP =", font=_font(10), fill="#991b1b", anchor="mm")
            d.text((48, 70), "RESET", font=_font(10), fill="#991b1b", anchor="mm")

        self.set_key(29, img)
        self._health_flash_timer = threading.Timer(0.7, self._health_flash_tick)
        self._health_flash_timer.daemon = True
        self._health_flash_timer.start()

    def _snooze_health_alert(self):
        """User tapped — they took a break. Reset session."""
        self._health_alerting = False
        if self._health_flash_timer:
            self._health_flash_timer.cancel()
            self._health_flash_timer = None
        # Reset session in activity tracker
        activity.reset_session()
        self.activity_data = activity.get_activity()
        # Play positive sound
        import pomodoro
        pomodoro._play("break_done")
        # Render fresh session key (0m)
        self._render_activity_row()

    def _stop_health_alert(self):
        """Stop flash (page change, shutdown)."""
        self._health_alerting = False
        if self._health_flash_timer:
            self._health_flash_timer.cancel()
            self._health_flash_timer = None

    def _render_bright_btn(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#1e293b")
        d = ImageDraw.Draw(img)
        d.text((48, 26), "BRIGHT", font=_font(12), fill="white", anchor="mm")
        d.text((48, 50), f"{self.brightness}%", font=_font(18), fill="#fbbf24", anchor="mm")
        return img

    def show_games(self):
        self.pomo.stop()
        self._stop_health_alert()
        with self.lock:
            self.page = "games"
        self.deck.reset()

        # Key 0: back to home
        self.set_key(0, render_back("HOME"))

        # Keys 1-31: all games
        for game in GAMES:
            pos = game["pos"]
            if pos < 32:
                self.set_key(pos, render_game_btn(game["title"], game["subtitle"], game["bg"]))

        # Sound mute toggle
        self.set_key(15, render_voice_btn(not sound_engine.global_mute))

        # Fill empty slots
        used = {0, 15} | {g["pos"] for g in GAMES}
        for k in range(1, 32):
            if k not in used:
                self.set_key(k, render_empty())

    def show_agents(self):
        self.pomo.stop()
        self._stop_health_alert()
        with self.lock:
            self.page = "agents"
        self.deck.reset()

        self.set_key(0, render_back("HOME"))
        for k in range(1, 32):
            img = Image.new("RGB", SIZE, "#1f2937")
            d = ImageDraw.Draw(img)
            d.text((48, 30), "COMING", font=_font(14), fill="#6b7280", anchor="mt")
            d.text((48, 52), "SOON", font=_font(14), fill="#4b5563", anchor="mt")
            self.set_key(k, img)

    # ── game launch/stop (same pattern as arcade.py) ──────────────────

    def launch_game(self, game_info: dict):
        self.pomo.stop()
        self._stop_health_alert()
        with self.lock:
            self.page = "game"

        script = game_info["script"]
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        mod = importlib.import_module(script)

        # Generate SFX if needed
        if hasattr(mod, "_generate_sfx") and hasattr(mod, "_sfx_cache") and not mod._sfx_cache:
            try:
                mod._generate_sfx()
            except Exception:
                pass

        cls_name = CLASS_MAP.get(script)
        if not cls_name or not hasattr(mod, cls_name):
            self.show_home()
            return
        game = getattr(mod, cls_name)(self.deck)

        with self.lock:
            self.active_game = game
            self.active_module = mod

        # Back button
        self.set_key(0, render_back("HOME"))

        # Show game idle screen
        game.show_idle()

        # Wrap key callback to intercept back button
        original_on_key = game.on_key

        def wrapped_on_key(deck, key, pressed):
            if pressed and key == 0:
                self._stop_game()
                self.show_home()
                self.deck.set_key_callback(self.on_key)
                return
            original_on_key(deck, key, pressed)

        self.deck.set_key_callback(wrapped_on_key)

    def _stop_game(self):
        sound_engine.stop_all()
        with self.lock:
            game = self.active_game
            self.active_game = None
            self.active_module = None
        if game:
            for attr in ("_cancel_beaver_timer", "_cancel_tick", "_cancel_all_timers", "_cancel_timer"):
                if hasattr(game, attr):
                    try:
                        getattr(game, attr)()
                    except Exception:
                        pass
            if hasattr(game, "running"):
                game.running = False

    # ── key handler ───────────────────────────────────────────────────

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        with self.lock:
            page = self.page

        if page == "home":
            self._on_key_home(key)
        elif page == "games":
            self._on_key_games(key)
        elif page == "agents":
            self._on_key_agents(key)

    def _on_key_home(self, key: int):
        # Row 1 sections
        if key == 8:
            self.show_games()
            return
        if key == 9:
            # CRYPTO REAL — direct launch
            info = _GAME_BY_SCRIPT.get("crypto_real_game")
            if info:
                self.launch_game(info)
            return
        if key == 10:
            # CRYPTO SIM — direct launch
            info = _GAME_BY_SCRIPT.get("crypto_game")
            if info:
                self.launch_game(info)
            return
        if key == 11:
            self.show_agents()
            return
        if key == 13:
            # Siri
            import subprocess
            subprocess.Popen(["osascript", "-e", 'tell application "Siri" to activate'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        if key == 14:
            # Global sound mute toggle
            sound_engine.global_mute = not sound_engine.global_mute
            if sound_engine.global_mute:
                sound_engine.stop_all()
            self.set_key(14, render_voice_btn(not sound_engine.global_mute))
            return
        if key == 15:
            # Brightness cycle
            self.brightness_idx = (self.brightness_idx + 1) % len(BRIGHTNESS_LEVELS)
            self.brightness = BRIGHTNESS_LEVELS[self.brightness_idx]
            self.deck.set_brightness(self.brightness)
            self.set_key(15, self._render_bright_btn())
            return
        # Health alert snooze (key 29 = SESSION)
        if key == 29 and self._health_alerting:
            self._snooze_health_alert()
            return
        # Pomodoro keys (24-27)
        if key in self.pomo.keys:
            self.pomo.on_key(key)
            return

    def _on_key_games(self, key: int):
        if key == 0:
            self.show_home()
            return
        if key == 15:
            sound_engine.global_mute = not sound_engine.global_mute
            if sound_engine.global_mute:
                sound_engine.stop_all()
            self.set_key(15, render_voice_btn(not sound_engine.global_mute))
            return
        for game in GAMES:
            if key == game["pos"]:
                self.launch_game(game)
                return

    def _on_key_agents(self, key: int):
        if key == 0:
            self.show_home()
            return

    # ── tick loop ─────────────────────────────────────────────────────

    def _schedule_tick(self):
        if not self.running:
            return
        self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    def _cancel_tick(self):
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None

    def _tick(self):
        if not self.running:
            return
        self._tick_count += 1

        # Fetch crypto in background thread (non-blocking for UI)
        try:
            self.crypto.fetch()
        except Exception:
            pass

        # Weather every 15 min (or first tick)
        now = time.time()
        if now - self._last_weather > 900 or self._last_weather == 0:
            try:
                self.weather_data = weather.get_weather()
                self._last_weather = now
            except Exception:
                pass

        # Activity tracker every tick
        try:
            self.activity_data = activity.get_activity()
        except Exception:
            pass

        # Local air sensor every tick (has its own 5-min cache)
        try:
            self.local_sensor = airquality.fetch_local()
        except Exception:
            pass

        # Remote environment every 15 min
        if now - self._last_env_remote > 900 or self._last_env_remote == 0:
            try:
                self.remote_env = airquality.fetch_remote()
                self._last_env_remote = now
            except Exception:
                pass

        # Update home screen if visible
        with self.lock:
            page = self.page

        if page == "home":
            try:
                self._render_status_bar()
                self._render_env_row()
                self._render_activity_row()
                self._check_health_alert()
            except Exception:
                pass

        self._schedule_tick()

    # ── run / lifecycle ───────────────────────────────────────────────

    def run(self):
        """Start tick loop and block until device is lost."""
        self.running = True

        # Initial data fetch (background)
        t = threading.Thread(target=self._initial_fetch, daemon=True)
        t.start()

        self._schedule_tick()

        # Block until device disconnects or Ctrl+C
        try:
            while self.running:
                # Check if deck is still connected
                try:
                    self.deck.key_count()
                except Exception:
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            self._cancel_tick()

    def _initial_fetch(self):
        """Fetch weather + crypto + air quality on startup, then update home."""
        try:
            self.weather_data = weather.get_weather()
            self._last_weather = time.time()
        except Exception:
            pass
        try:
            self.crypto.fetch()
        except Exception:
            pass
        try:
            self.local_sensor = airquality.fetch_local()
        except Exception:
            pass
        try:
            self.remote_env = airquality.fetch_remote()
            self._last_env_remote = time.time()
        except Exception:
            pass
        try:
            self.activity_data = activity.get_activity()
        except Exception:
            pass
        # Update display if still on home
        with self.lock:
            page = self.page
        if page == "home":
            try:
                self._render_status_bar()
                self._render_env_row()
                self._render_activity_row()
            except Exception:
                pass


# ── USB reconnect main loop ──────────────────────────────────────────

def _find_deck():
    """Find first visual Stream Deck."""
    try:
        decks = DeviceManager().enumerate()
        for d in decks:
            if d.is_visual():
                return d
    except Exception:
        pass
    return None


def main():
    print("Stream Deck Dashboard — waiting for device...")
    while True:
        deck = _find_deck()
        if not deck:
            time.sleep(2)
            continue

        try:
            deck.open()
            deck.reset()
            deck.set_brightness(80)
            print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")

            app = Dashboard(deck)
            app.show_home()
            deck.set_key_callback(app.on_key)
            app.run()
        except Exception as e:
            print(f"Device error: {e}")
        finally:
            try:
                sound_engine.stop_all()
                deck.reset()
                deck.close()
            except Exception:
                pass
            print("Device disconnected. Waiting for reconnect...")
        time.sleep(1)


if __name__ == "__main__":
    main()
