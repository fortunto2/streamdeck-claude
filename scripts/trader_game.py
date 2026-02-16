"""Space Trader -- Stream Deck trading game.

Travel between planets, buy low sell high, upgrade your ship,
and survive pirates. Reach 50,000 credits to win.

Voice pack: RA2 Kirov

Usage:
    uv run python scripts/trader_game.py
"""

import json
import math
import os
import random
import struct
import sys
import tempfile
import threading
import time
import wave

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

import scores
import sound_engine

# -- config ----------------------------------------------------------------
ROWS = 3
COLS = 8
ROW_OFFSET = 1  # game row 0 = deck row 1
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
SAVE_FILE = os.path.expanduser("~/.streamdeck-arcade/trader_save.json")
WIN_CREDITS = 50000
START_CREDITS = 500
START_CARGO_MAX = 10
START_FUEL = 5

# -- planets ---------------------------------------------------------------
PLANETS = [
    {"id": "terra",   "name": "TERRA",   "bg": "#1e40af", "cheap": None,        "expensive": None},
    {"id": "mars",    "name": "MARS",    "bg": "#991b1b", "cheap": "minerals",  "expensive": "food"},
    {"id": "neptune", "name": "NEPTUNE", "bg": "#1e3a5f", "cheap": "fuel",      "expensive": "tech"},
    {"id": "venus",   "name": "VENUS",   "bg": "#a16207", "cheap": "chemicals", "expensive": "minerals"},
    {"id": "kronos",  "name": "KRONOS",  "bg": "#6b21a8", "cheap": "tech",      "expensive": "weapons"},
    {"id": "kepler",  "name": "KEPLER",  "bg": "#15803d", "cheap": "food",      "expensive": "chemicals"},
    {"id": "titan",   "name": "TITAN",   "bg": "#c2410c", "cheap": "fuel",      "expensive": "food"},
    {"id": "omega",   "name": "OMEGA",   "bg": "#4b5563", "cheap": None,        "expensive": None},
]

# -- goods -----------------------------------------------------------------
GOODS = [
    {"id": "food",      "name": "FOOD",  "base": 20, "color": "#22c55e"},
    {"id": "minerals",  "name": "MINRL", "base": 35, "color": "#a8a29e"},
    {"id": "fuel",      "name": "FUEL",  "base": 15, "color": "#f97316"},
    {"id": "tech",      "name": "TECH",  "base": 60, "color": "#3b82f6"},
    {"id": "chemicals", "name": "CHEM",  "base": 45, "color": "#eab308"},
    {"id": "weapons",   "name": "WEAPN", "base": 80, "color": "#ef4444"},
]
GOODS_BY_ID = {g["id"]: g for g in GOODS}

# Planets where weapons are illegal
WEAPONS_ILLEGAL = {"terra", "kepler", "neptune"}

# -- events ----------------------------------------------------------------
EVENT_TYPES = ["pirates", "asteroid", "police", "trader", "distress"]
EVENT_CHANCE = 0.30

# -- distance matrix (simple: index distance with wrap) --------------------
def _planet_distance(a_idx, b_idx):
    d = abs(a_idx - b_idx)
    return max(1, min(d, 8 - d))

# -- grid helpers ----------------------------------------------------------
def pos_to_rc(pos):
    return pos // COLS - ROW_OFFSET, pos % COLS

def rc_to_pos(row, col):
    return (row + ROW_OFFSET) * COLS + col

# -- voice pack (RA2 Kirov) -----------------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "ra2_kirov/sounds/KirovReporting.mp3",
        "ra2_kirov/sounds/AirshipReady.mp3",
    ],
    "travel": [
        "ra2_kirov/sounds/SettingNewCourse.mp3",
        "ra2_kirov/sounds/ManeuverPropsEngaged.mp3",
        "ra2_kirov/sounds/BearingSet.mp3",
    ],
    "trade": [
        "ra2_kirov/sounds/Acknowledged.mp3",
        "ra2_kirov/sounds/HeliumMixOptimal.mp3",
    ],
    "combat": [
        "ra2_kirov/sounds/BombingBaysReady.mp3",
        "ra2_kirov/sounds/TargetAcquired.mp3",
        "ra2_kirov/sounds/ClosingOnTarget.mp3",
    ],
    "damage": [
        "ra2_kirov/sounds/WereLosingAltitude.mp3",
        "ra2_kirov/sounds/MaydayMayday.mp3",
    ],
    "win": [
        "ra2_kirov/sounds/BombardiersToYourStations.mp3",
    ],
    "death": [
        "ra2_kirov/sounds/ShesGoingToBlow.mp3",
    ],
}

_last_voice_time: float = 0
VOICE_COOLDOWN = 3.0

def play_voice(event: str):
    global _last_voice_time
    now = time.monotonic()
    if now - _last_voice_time < VOICE_COOLDOWN:
        return
    paths = VOICES.get(event, [])
    if not paths:
        return
    random.shuffle(paths)
    for rel in paths:
        full = os.path.join(PEON_DIR, rel)
        if os.path.exists(full):
            _last_voice_time = now
            sound_engine.play_voice(full)
            return

def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()

# -- 8-bit SFX ------------------------------------------------------------
SAMPLE_RATE = 22050
_sfx_cache: dict[str, str] = {}
_sfx_dir: str = ""

def _square(freq, dur, vol=1.0, duty=0.5):
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        if freq == 0:
            samples.append(0)
        else:
            t = i / SAMPLE_RATE
            phase = (t * freq) % 1.0
            val = vol if phase < duty else -vol
            env = min(1.0, i / (SAMPLE_RATE * 0.003))
            tail = max(0.0, 1.0 - (i / n) * 0.8)
            samples.append(val * env * tail)
    return samples

def _triangle(freq, dur, vol=1.0):
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        if freq == 0:
            samples.append(0)
        else:
            t = i / SAMPLE_RATE
            phase = (t * freq) % 1.0
            val = (4 * abs(phase - 0.5) - 1) * vol
            env = min(1.0, i / (SAMPLE_RATE * 0.003))
            tail = max(0.0, 1.0 - (i / n) * 0.6)
            samples.append(val * env * tail)
    return samples

def _noise(dur, vol=1.0):
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        val = random.uniform(-1, 1) * vol
        tail = max(0.0, 1.0 - (i / n) * 0.9)
        samples.append(val * tail)
    return samples

def _write_wav(path, samples):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for s in samples:
            s = max(-0.95, min(0.95, s))
            w.writeframes(struct.pack("<h", int(s * 32767)))

def _generate_sfx():
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="trader-sfx-")
    v = SFX_VOLUME

    # buy: cash register blip
    s = (_square(880, 0.03, v * 0.4) + _square(1100, 0.03, v * 0.5) +
         _triangle(1320, 0.06, v * 0.4))
    _write_wav(os.path.join(_sfx_dir, "buy.wav"), s)
    _sfx_cache["buy"] = os.path.join(_sfx_dir, "buy.wav")

    # sell: coin sound
    s = (_triangle(1200, 0.04, v * 0.4) + _triangle(1500, 0.04, v * 0.45) +
         _triangle(1800, 0.06, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "sell.wav"), s)
    _sfx_cache["sell"] = os.path.join(_sfx_dir, "sell.wav")

    # travel: whoosh
    s = []
    n = int(SAMPLE_RATE * 0.25)
    for i in range(n):
        t = i / n
        freq = 200 + 600 * t
        phase = (i / SAMPLE_RATE * freq) % 1.0
        val = (random.uniform(-1, 1) * 0.3 + (1 if phase < 0.5 else -1) * 0.2)
        env = math.sin(math.pi * t)
        s.append(val * env * v * 0.4)
    _write_wav(os.path.join(_sfx_dir, "travel.wav"), s)
    _sfx_cache["travel"] = os.path.join(_sfx_dir, "travel.wav")

    # combat: laser
    s = []
    n = int(SAMPLE_RATE * 0.15)
    for i in range(n):
        t = i / n
        freq = 1200 - 800 * t
        phase = (i / SAMPLE_RATE * freq) % 1.0
        val = (1 if phase < 0.3 else -1) * v * 0.5
        tail = max(0.0, 1.0 - t * 0.8)
        s.append(val * tail)
    _write_wav(os.path.join(_sfx_dir, "combat.wav"), s)
    _sfx_cache["combat"] = os.path.join(_sfx_dir, "combat.wav")

    # damage: explosion
    s = _noise(0.2, v * 0.5) + _noise(0.15, v * 0.3)
    _write_wav(os.path.join(_sfx_dir, "damage.wav"), s)
    _sfx_cache["damage"] = os.path.join(_sfx_dir, "damage.wav")

    # event: alert beep
    s = (_square(600, 0.08, v * 0.4, 0.5) + _square(0, 0.04) +
         _square(600, 0.08, v * 0.4, 0.5))
    _write_wav(os.path.join(_sfx_dir, "event.wav"), s)
    _sfx_cache["event"] = os.path.join(_sfx_dir, "event.wav")

    # levelup: fanfare
    s = (_triangle(523, 0.08, v * 0.4) + _triangle(659, 0.08, v * 0.45) +
         _triangle(784, 0.08, v * 0.5) + _triangle(1047, 0.2, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "levelup.wav"), s)
    _sfx_cache["levelup"] = os.path.join(_sfx_dir, "levelup.wav")

    # save: click
    s = _square(800, 0.02, v * 0.3, 0.3)
    _write_wav(os.path.join(_sfx_dir, "save.wav"), s)
    _sfx_cache["save"] = os.path.join(_sfx_dir, "save.wav")

    # error
    s = _square(150, 0.1, v * 0.3, 0.3) + _square(120, 0.1, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "error.wav"), s)
    _sfx_cache["error"] = os.path.join(_sfx_dir, "error.wav")

    # win
    s = (_triangle(523, 0.1, v * 0.5) + _triangle(659, 0.1, v * 0.55) +
         _triangle(784, 0.1, v * 0.6) + _triangle(1047, 0.3, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "win.wav"), s)
    _sfx_cache["win"] = os.path.join(_sfx_dir, "win.wav")

    # select
    s = _square(800, 0.02, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "select.wav"), s)
    _sfx_cache["select"] = os.path.join(_sfx_dir, "select.wav")

def play_sfx(name):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)

def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)

# -- compact number display ------------------------------------------------
def _compact(n):
    n = int(n)
    if n < 1000:
        return str(n)
    elif n < 10000:
        return f"{n / 1000:.1f}K"
    elif n < 1000000:
        return f"{n // 1000}K"
    return f"{n / 1000000:.1f}M"

# -- renderers -------------------------------------------------------------

def render_hud_empty(size=SIZE):
    return Image.new("RGB", size, "#111827")

def render_title(text, sub="", size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 28), text, font=_font(16), fill="#fbbf24", anchor="mm")
    if sub:
        d.text((48, 56), sub, font=_font(12), fill="#9ca3af", anchor="mm")
    return img

def render_btn(t1, t2, bg="#065f46", c1="white", c2="#34d399", size=SIZE):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 34), t1, font=_font(16), fill=c1, anchor="mm")
    d.text((48, 60), t2, font=_font(14), fill=c2, anchor="mm")
    return img

def render_empty_tile(size=SIZE):
    img = Image.new("RGB", size, "#1a1a2e")
    return img

# -- HUD renderers --------------------------------------------------------

def render_hud_credits(amount, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "CREDITS", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 46), _compact(amount), font=_font(22), fill="#fbbf24", anchor="mm")
    pct = min(100, int(amount / WIN_CREDITS * 100))
    # progress bar toward 50K
    bw = 60
    bx = 48 - bw // 2
    d.rectangle([bx, 70, bx + bw, 78], outline="#4b5563")
    fw = int(bw * pct / 100)
    if fw > 0:
        d.rectangle([bx, 70, bx + fw, 78], fill="#fbbf24")
    d.text((48, 88), f"{pct}%>50K", font=_font(8), fill="#6b7280", anchor="mm")
    return img

def render_hud_cargo(used, maximum, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "CARGO", font=_font(10), fill="#9ca3af", anchor="mt")
    color = "#34d399" if used < maximum else "#ef4444"
    d.text((48, 46), f"{used}/{maximum}", font=_font(20), fill=color, anchor="mm")
    d.text((48, 76), "HOLD", font=_font(10), fill="#6b7280", anchor="mm")
    return img

def render_hud_planet(planet, size=SIZE):
    p = planet
    img = Image.new("RGB", size, p["bg"])
    d = ImageDraw.Draw(img)
    d.text((48, 20), p["name"], font=_font(16), fill="white", anchor="mt")
    if p["cheap"]:
        d.text((48, 50), f"Cheap:{GOODS_BY_ID[p['cheap']]['name']}", font=_font(9), fill="#86efac", anchor="mm")
    if p["expensive"]:
        d.text((48, 68), f"Dear:{GOODS_BY_ID[p['expensive']]['name']}", font=_font(9), fill="#f87171", anchor="mm")
    if p["id"] == "omega":
        d.text((48, 56), "BLACK MKT", font=_font(10), fill="#fbbf24", anchor="mm")
        d.text((48, 72), "VOLATILE!", font=_font(9), fill="#f87171", anchor="mm")
    return img

def render_hud_ship(hp, max_hp, fuel, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "SHIP", font=_font(10), fill="#9ca3af", anchor="mt")
    hp_color = "#34d399" if hp > 30 else "#ef4444"
    d.text((48, 36), f"HP:{hp}", font=_font(14), fill=hp_color, anchor="mm")
    d.text((48, 58), f"FUEL:{fuel}", font=_font(13), fill="#f97316", anchor="mm")
    return img

def render_hud_turn(turn, best, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "TURN", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 40), str(turn), font=_font(20), fill="#60a5fa", anchor="mm")
    if best > 0:
        d.text((48, 68), f"BEST:{best}", font=_font(9), fill="#6b7280", anchor="mm")
    return img

def render_hud_stats(weapons, shields, speed, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "STATS", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 34), f"WPN:{weapons}", font=_font(11), fill="#ef4444", anchor="mm")
    d.text((48, 52), f"SHD:{shields}", font=_font(11), fill="#3b82f6", anchor="mm")
    d.text((48, 70), f"SPD:{speed}", font=_font(11), fill="#22c55e", anchor="mm")
    return img

# -- good tile renderers ---------------------------------------------------

def render_good_buy(good, price, qty_held, size=SIZE):
    """Render a good tile for buying on the market."""
    g = GOODS_BY_ID[good]
    img = Image.new("RGB", size, "#1f2937")
    d = ImageDraw.Draw(img)
    # colored top bar
    d.rectangle([0, 0, 95, 18], fill=g["color"])
    d.text((48, 9), g["name"], font=_font(11), fill="white", anchor="mm")
    d.text((48, 36), f"${price}", font=_font(18), fill="#fbbf24", anchor="mm")
    d.text((48, 60), "BUY", font=_font(12), fill="#86efac", anchor="mm")
    if qty_held > 0:
        d.text((48, 80), f"x{qty_held}", font=_font(10), fill="#9ca3af", anchor="mm")
    return img

def render_good_sell(good, price, qty_held, size=SIZE):
    """Render a cargo tile for selling."""
    g = GOODS_BY_ID[good]
    if qty_held <= 0:
        img = Image.new("RGB", size, "#111827")
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 95, 18], fill="#374151")
        d.text((48, 9), g["name"], font=_font(11), fill="#6b7280", anchor="mm")
        d.text((48, 48), "---", font=_font(14), fill="#374151", anchor="mm")
        return img
    img = Image.new("RGB", size, "#1a1a2e")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 95, 18], fill=g["color"])
    d.text((48, 9), g["name"], font=_font(11), fill="white", anchor="mm")
    d.text((48, 36), f"x{qty_held}", font=_font(18), fill="white", anchor="mm")
    d.text((48, 60), "SELL", font=_font(12), fill="#fbbf24", anchor="mm")
    d.text((48, 80), f"${price}", font=_font(10), fill="#9ca3af", anchor="mm")
    return img

# -- upgrade tile renderers ------------------------------------------------

def render_upgrade_tile(label, sub, level, max_lvl, cost, can_afford, size=SIZE):
    bg = "#1f2937" if level < max_lvl else "#0f172a"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 12), label, font=_font(11), fill="white", anchor="mt")
    if level >= max_lvl:
        d.text((48, 36), sub, font=_font(10), fill="#9ca3af", anchor="mt")
        d.text((48, 64), "MAX", font=_font(16), fill="#fbbf24", anchor="mm")
    else:
        d.text((48, 30), sub, font=_font(10), fill="#9ca3af", anchor="mt")
        d.text((48, 52), f"Lv{level}", font=_font(16), fill="#60a5fa", anchor="mm")
        cfill = "#86efac" if can_afford else "#6b7280"
        d.text((48, 74), f"${cost}", font=_font(12), fill=cfill, anchor="mm")
        if can_afford:
            d.rectangle([2, 2, 93, 93], outline="#fbbf24", width=2)
    return img

def render_action_btn(label, sub, bg, c1="white", c2="#9ca3af", size=SIZE):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 32), label, font=_font(16), fill=c1, anchor="mm")
    d.text((48, 60), sub, font=_font(11), fill=c2, anchor="mm")
    return img

# -- travel screen ---------------------------------------------------------

def render_planet_tile(planet, fuel_cost, is_current, can_afford_fuel, size=SIZE):
    bg = planet["bg"] if not is_current else "#0f172a"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    # planet circle
    cx, cy = 48, 30
    r = 14
    circle_color = planet["bg"] if is_current else "white"
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=circle_color, outline="white", width=2)
    d.text((48, 56), planet["name"], font=_font(11), fill="white", anchor="mm")
    if is_current:
        d.text((48, 72), "HERE", font=_font(10), fill="#fbbf24", anchor="mm")
    else:
        fc = "#86efac" if can_afford_fuel else "#ef4444"
        d.text((48, 72), f"FUEL:{fuel_cost}", font=_font(10), fill=fc, anchor="mm")
        if can_afford_fuel:
            d.rectangle([2, 2, 93, 93], outline="#34d399", width=2)
    return img

# -- event screen ----------------------------------------------------------

def render_event_title(text, sub, bg="#7f1d1d", size=SIZE):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    lines = text.split("\n")
    y = 20
    for line in lines:
        d.text((48, y), line, font=_font(12), fill="white", anchor="mt")
        y += 18
    if sub:
        d.text((48, 78), sub, font=_font(10), fill="#fbbf24", anchor="mm")
    return img

def render_event_choice(label, sub, bg, size=SIZE):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 93, 93], outline="#fbbf24", width=2)
    d.text((48, 34), label, font=_font(16), fill="white", anchor="mm")
    d.text((48, 62), sub, font=_font(10), fill="#d1d5db", anchor="mm")
    return img

def render_event_result(lines, bg="#111827", size=SIZE):
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    y = 14
    for line in lines:
        d.text((48, y), line, font=_font(11), fill="white", anchor="mt")
        y += 16
    return img

# -- game over / win -------------------------------------------------------

def render_gameover_tile(text, size=SIZE):
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.text((48, 48), text, font=_font(14), fill="white", anchor="mm")
    return img

def render_win_tile(text="WIN!", size=SIZE):
    img = Image.new("RGB", size, "#7c3aed")
    d = ImageDraw.Draw(img)
    d.text((48, 48), text, font=_font(20), fill="#fbbf24", anchor="mm")
    return img

# -- game ------------------------------------------------------------------

class TraderGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.mode = "idle"  # idle | planet | travel | event | result | gameover
        self.timers = []

        # Ship state
        self.credits = START_CREDITS
        self.cargo = {g["id"]: 0 for g in GOODS}
        self.cargo_max = START_CARGO_MAX
        self.fuel = START_FUEL
        self.hp = 100
        self.max_hp = 100
        self.weapons = 1
        self.shields = 0
        self.speed = 1

        # World state
        self.planet_idx = 0  # index into PLANETS (start at TERRA)
        self.turn = 0
        self.won = False
        self.prices = {}  # planet_id -> {good_id: price}
        self.best_score = 0

        # Event state
        self.event_type = None
        self.event_data = {}

        # Prerender
        self.img_empty = render_empty_tile()

    def set_key(self, pos, img):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _cancel_all_timers(self):
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    def _delayed(self, delay, fn):
        t = threading.Timer(delay, fn)
        t.daemon = True
        t.start()
        self.timers.append(t)

    # -- prices ------------------------------------------------------------

    def _generate_prices(self):
        """Generate prices for all planets with fluctuation."""
        self.prices = {}
        for p in PLANETS:
            planet_prices = {}
            for g in GOODS:
                base = g["base"]
                # Cheap good: 40-70% of base
                if p["cheap"] == g["id"]:
                    price = int(base * random.uniform(0.40, 0.70))
                # Expensive good: 140-200% of base
                elif p["expensive"] == g["id"]:
                    price = int(base * random.uniform(1.40, 2.00))
                # Omega: wild swings 30-250%
                elif p["id"] == "omega":
                    price = int(base * random.uniform(0.30, 2.50))
                # Normal: 70-130%
                else:
                    price = int(base * random.uniform(0.70, 1.30))
                planet_prices[g["id"]] = max(1, price)
            self.prices[p["id"]] = planet_prices

    def _current_planet(self):
        return PLANETS[self.planet_idx]

    def _current_prices(self):
        return self.prices[self._current_planet()["id"]]

    def _cargo_used(self):
        return sum(self.cargo.values())

    # -- save / load -------------------------------------------------------

    def _save_game(self):
        data = {
            "credits": self.credits,
            "cargo": self.cargo,
            "cargo_max": self.cargo_max,
            "fuel": self.fuel,
            "hp": self.hp,
            "max_hp": self.max_hp,
            "weapons": self.weapons,
            "shields": self.shields,
            "speed": self.speed,
            "planet_idx": self.planet_idx,
            "turn": self.turn,
            "won": self.won,
            "prices": self.prices,
        }
        try:
            os.makedirs(os.path.dirname(SAVE_FILE), exist_ok=True)
            with open(SAVE_FILE, "w") as f:
                json.dump(data, f, indent=2)
            play_sfx("save")
        except Exception:
            pass

    def _load_save(self):
        try:
            with open(SAVE_FILE) as f:
                data = json.load(f)
            self.credits = data.get("credits", START_CREDITS)
            self.cargo = data.get("cargo", {g["id"]: 0 for g in GOODS})
            # Ensure all goods exist in cargo
            for g in GOODS:
                if g["id"] not in self.cargo:
                    self.cargo[g["id"]] = 0
            self.cargo_max = data.get("cargo_max", START_CARGO_MAX)
            self.fuel = data.get("fuel", START_FUEL)
            self.hp = data.get("hp", 100)
            self.max_hp = data.get("max_hp", 100)
            self.weapons = data.get("weapons", 1)
            self.shields = data.get("shields", 0)
            self.speed = data.get("speed", 1)
            self.planet_idx = data.get("planet_idx", 0)
            self.turn = data.get("turn", 0)
            self.won = data.get("won", False)
            self.prices = data.get("prices", {})
            if not self.prices:
                self._generate_prices()
            return True
        except Exception:
            return False

    def _delete_save(self):
        try:
            os.remove(SAVE_FILE)
        except FileNotFoundError:
            pass

    # -- idle screen -------------------------------------------------------

    def show_idle(self):
        self.running = False
        self.mode = "idle"
        self._cancel_all_timers()
        self.best_score = scores.load_best("trader", 0)
        has_save = os.path.exists(SAVE_FILE)

        # HUD row
        self.set_key(1, render_title("SPACE", "TRADER"))
        for k in range(2, 8):
            self.set_key(k, render_hud_empty())

        # Game area
        for k in range(8, 32):
            self.set_key(k, self.img_empty)

        if has_save:
            self._load_save()
            self.set_key(2, render_title(f"{_compact(self.credits)}$"))
            self.set_key(3, render_title(f"T{self.turn}", self._current_planet()["name"]))
            self.set_key(12, render_btn("CONT", "INUE", "#1e40af", "white", "#93c5fd"))
            self.set_key(20, render_btn("NEW", "GAME"))
        else:
            self.set_key(16, render_btn("NEW", "GAME"))

        if self.best_score > 0:
            self.set_key(5, render_title("BEST", f"{self.best_score} turns"))

    # -- start / continue --------------------------------------------------

    def _start_new(self):
        self._delete_save()
        self.credits = START_CREDITS
        self.cargo = {g["id"]: 0 for g in GOODS}
        self.cargo_max = START_CARGO_MAX
        self.fuel = START_FUEL
        self.hp = 100
        self.max_hp = 100
        self.weapons = 1
        self.shields = 0
        self.speed = 1
        self.planet_idx = 0
        self.turn = 0
        self.won = False
        self._generate_prices()
        self._begin_play()

    def _continue(self):
        self._load_save()
        self._begin_play()

    def _begin_play(self):
        self.running = True
        self.mode = "planet"
        self.best_score = scores.load_best("trader", 0)
        play_sfx("select")
        play_voice("start")
        self._render_planet_screen()

    # -- planet screen (main) ----------------------------------------------

    def _render_planet_screen(self):
        self.mode = "planet"
        planet = self._current_planet()
        prices = self._current_prices()

        # HUD: keys 1-7
        self.set_key(1, render_hud_credits(self.credits))
        self.set_key(2, render_hud_cargo(self._cargo_used(), self.cargo_max))
        self.set_key(3, render_hud_planet(planet))
        self.set_key(4, render_hud_ship(self.hp, self.max_hp, self.fuel))
        self.set_key(5, render_hud_turn(self.turn, self.best_score))
        self.set_key(6, render_hud_stats(self.weapons, self.shields, self.speed))
        if not self.won and self.credits >= WIN_CREDITS:
            self.set_key(7, render_action_btn("WIN!", "BUY STN", "#7c3aed", "#fbbf24", "#d1d5db"))
        else:
            self.set_key(7, render_hud_empty())

        # Row 0 (keys 8-15): 6 goods BUY + 2 actions
        for i, g in enumerate(GOODS):
            key = rc_to_pos(0, i)
            self.set_key(key, render_good_buy(g["id"], prices[g["id"]], self.cargo[g["id"]]))

        # key 14: TRAVEL button
        self.set_key(rc_to_pos(0, 6), render_action_btn("FLY", f"FUEL:{self.fuel}", "#1e40af", "white", "#f97316"))
        # key 15: SAVE button
        self.set_key(rc_to_pos(0, 7), render_action_btn("SAVE", "GAME", "#374151"))

        # Row 1 (keys 16-23): 6 goods SELL (your cargo)
        for i, g in enumerate(GOODS):
            key = rc_to_pos(1, i)
            self.set_key(key, render_good_sell(g["id"], prices[g["id"]], self.cargo[g["id"]]))

        # key 22, 23: empty
        self.set_key(rc_to_pos(1, 6), self.img_empty)
        self.set_key(rc_to_pos(1, 7), self.img_empty)

        # Row 2 (keys 24-31): upgrades + repair
        # Cargo upgrade
        cargo_cost = 500
        cargo_lvl = (self.cargo_max - 10) // 5  # 0..8
        cargo_max_lvl = 8  # max 50 cargo
        self.set_key(rc_to_pos(2, 0), render_upgrade_tile(
            "CARGO", f"{self.cargo_max} cap",
            cargo_lvl, cargo_max_lvl, cargo_cost,
            self.credits >= cargo_cost))

        # Weapons upgrade
        wpn_cost = 400 + self.weapons * 200
        self.set_key(rc_to_pos(2, 1), render_upgrade_tile(
            "WEAPON", f"Atk:{self.weapons}",
            self.weapons, 5, wpn_cost,
            self.credits >= wpn_cost))

        # Shield upgrade
        shd_cost = 300 + self.shields * 200
        self.set_key(rc_to_pos(2, 2), render_upgrade_tile(
            "SHIELD", f"Def:{self.shields}",
            self.shields, 5, shd_cost,
            self.credits >= shd_cost))

        # Speed upgrade
        spd_cost = 600 + self.speed * 400
        self.set_key(rc_to_pos(2, 3), render_upgrade_tile(
            "SPEED", f"Spd:{self.speed}",
            self.speed, 3, spd_cost,
            self.credits >= spd_cost))

        # Repair
        repair_cost = max(1, (self.max_hp - self.hp) * 2)
        needs_repair = self.hp < self.max_hp
        if needs_repair:
            self.set_key(rc_to_pos(2, 4), render_action_btn(
                "REPAIR", f"${repair_cost}", "#065f46", "white", "#86efac"))
        else:
            self.set_key(rc_to_pos(2, 4), render_action_btn(
                "REPAIR", "FULL HP", "#1f2937", "#6b7280", "#374151"))

        # HP upgrade
        hp_cost = 800
        hp_lvl = (self.max_hp - 100) // 25  # 0..4
        self.set_key(rc_to_pos(2, 5), render_upgrade_tile(
            "HULL", f"HP:{self.max_hp}",
            hp_lvl, 4, hp_cost,
            self.credits >= hp_cost))

        # keys 30, 31 empty
        self.set_key(rc_to_pos(2, 6), self.img_empty)
        self.set_key(rc_to_pos(2, 7), self.img_empty)

    # -- travel screen -----------------------------------------------------

    def _render_travel_screen(self):
        self.mode = "travel"
        planet = self._current_planet()

        # HUD
        self.set_key(1, render_hud_credits(self.credits))
        self.set_key(2, render_hud_cargo(self._cargo_used(), self.cargo_max))
        self.set_key(3, render_action_btn("PICK", "DEST", "#1e40af"))
        self.set_key(4, render_hud_ship(self.hp, self.max_hp, self.fuel))
        self.set_key(5, render_hud_turn(self.turn, self.best_score))
        self.set_key(6, render_hud_stats(self.weapons, self.shields, self.speed))
        self.set_key(7, render_action_btn("BACK", "PLANET", "#374151", "#fbbf24", "#9ca3af"))

        # Row 0: 8 planets
        for i, p in enumerate(PLANETS):
            key = rc_to_pos(0, i)
            dist = _planet_distance(self.planet_idx, i)
            fuel_cost = max(1, dist - (self.speed - 1))
            fuel_cost = max(1, fuel_cost)
            is_current = (i == self.planet_idx)
            can_afford = (self.fuel >= fuel_cost)
            self.set_key(key, render_planet_tile(p, fuel_cost, is_current, can_afford))

        # Rows 1-2: empty
        for r in range(1, ROWS):
            for c in range(COLS):
                self.set_key(rc_to_pos(r, c), self.img_empty)

    # -- buy / sell --------------------------------------------------------

    def _buy_good(self, good_idx):
        if good_idx >= len(GOODS):
            return
        g = GOODS[good_idx]
        prices = self._current_prices()
        price = prices[g["id"]]

        if self.credits < price:
            play_sfx("error")
            return
        if self._cargo_used() >= self.cargo_max:
            play_sfx("error")
            return

        self.credits -= price
        self.cargo[g["id"]] += 1
        play_sfx("buy")
        play_voice("trade")
        self._render_planet_screen()

    def _sell_good(self, good_idx):
        if good_idx >= len(GOODS):
            return
        g = GOODS[good_idx]
        if self.cargo[g["id"]] <= 0:
            play_sfx("error")
            return

        prices = self._current_prices()
        price = prices[g["id"]]

        # Weapons illegal on some planets -- halved price (black market sale)
        planet = self._current_planet()
        if g["id"] == "weapons" and planet["id"] in WEAPONS_ILLEGAL:
            price = price // 2

        self.cargo[g["id"]] -= 1
        self.credits += price
        play_sfx("sell")
        self._check_win()
        self._render_planet_screen()

    # -- travel ------------------------------------------------------------

    def _travel_to(self, dest_idx):
        if dest_idx == self.planet_idx:
            play_sfx("error")
            return

        dist = _planet_distance(self.planet_idx, dest_idx)
        fuel_cost = max(1, dist - (self.speed - 1))
        fuel_cost = max(1, fuel_cost)

        if self.fuel < fuel_cost:
            play_sfx("error")
            return

        self.fuel -= fuel_cost
        self.planet_idx = dest_idx
        self.turn += 1
        self._generate_prices()  # new prices each turn
        play_sfx("travel")
        play_voice("travel")

        # Random event check
        if random.random() < EVENT_CHANCE:
            self._trigger_event()
        else:
            self._save_game()
            self._render_planet_screen()

    # -- upgrades ----------------------------------------------------------

    def _upgrade_cargo(self):
        cost = 500
        if self.cargo_max >= 50:
            play_sfx("error")
            return
        if self.credits < cost:
            play_sfx("error")
            return
        self.credits -= cost
        self.cargo_max += 5
        play_sfx("levelup")
        self._render_planet_screen()

    def _upgrade_weapons(self):
        cost = 400 + self.weapons * 200
        if self.weapons >= 5:
            play_sfx("error")
            return
        if self.credits < cost:
            play_sfx("error")
            return
        self.credits -= cost
        self.weapons += 1
        play_sfx("levelup")
        self._render_planet_screen()

    def _upgrade_shields(self):
        cost = 300 + self.shields * 200
        if self.shields >= 5:
            play_sfx("error")
            return
        if self.credits < cost:
            play_sfx("error")
            return
        self.credits -= cost
        self.shields += 1
        play_sfx("levelup")
        self._render_planet_screen()

    def _upgrade_speed(self):
        cost = 600 + self.speed * 400
        if self.speed >= 3:
            play_sfx("error")
            return
        if self.credits < cost:
            play_sfx("error")
            return
        self.credits -= cost
        self.speed += 1
        play_sfx("levelup")
        self._render_planet_screen()

    def _repair_ship(self):
        if self.hp >= self.max_hp:
            play_sfx("error")
            return
        cost = max(1, (self.max_hp - self.hp) * 2)
        if self.credits < cost:
            play_sfx("error")
            return
        self.credits -= cost
        self.hp = self.max_hp
        play_sfx("buy")
        self._render_planet_screen()

    def _upgrade_hull(self):
        cost = 800
        if self.max_hp >= 200:
            play_sfx("error")
            return
        if self.credits < cost:
            play_sfx("error")
            return
        self.credits -= cost
        self.max_hp += 25
        self.hp += 25  # get the HP too
        play_sfx("levelup")
        self._render_planet_screen()

    # -- events ------------------------------------------------------------

    def _trigger_event(self):
        self.mode = "event"
        etype = random.choice(EVENT_TYPES)

        # Police only if carrying weapons
        if etype == "police" and self.cargo["weapons"] <= 0:
            etype = random.choice(["pirates", "asteroid", "trader", "distress"])

        self.event_type = etype
        self.event_data = {}

        play_sfx("event")

        # Clear game area
        for k in range(8, 32):
            self.set_key(k, self.img_empty)

        if etype == "pirates":
            pirate_str = random.randint(2, 4) + self.turn // 10
            self.event_data["pirate_str"] = pirate_str
            bounty = random.randint(100, 300) + self.turn * 5
            self.event_data["bounty"] = bounty

            # HUD
            self.set_key(1, render_event_title("PIRATES\nATTACK!", f"STR:{pirate_str}", "#7f1d1d"))
            self.set_key(2, render_hud_ship(self.hp, self.max_hp, self.fuel))
            self.set_key(3, render_hud_stats(self.weapons, self.shields, self.speed))
            for k in range(4, 8):
                self.set_key(k, render_hud_empty())

            # Choices on row 0
            self.set_key(rc_to_pos(0, 2), render_event_choice("FIGHT", f"WPN:{self.weapons}", "#991b1b"))
            self.set_key(rc_to_pos(0, 5), render_event_choice("PAY", f"${bounty}", "#854d0e"))

        elif etype == "asteroid":
            dmg = random.randint(10, 30)
            dodge_fuel = 2
            self.event_data["damage"] = dmg
            self.event_data["dodge_fuel"] = dodge_fuel

            self.set_key(1, render_event_title("ASTEROID\nFIELD!", f"DMG:{dmg}", "#4b5563"))
            self.set_key(2, render_hud_ship(self.hp, self.max_hp, self.fuel))
            for k in range(3, 8):
                self.set_key(k, render_hud_empty())

            self.set_key(rc_to_pos(0, 2), render_event_choice("BRACE", f"-{dmg}HP", "#7f1d1d"))
            can_dodge = self.fuel >= dodge_fuel
            bg = "#065f46" if can_dodge else "#374151"
            self.set_key(rc_to_pos(0, 5), render_event_choice("DODGE", f"-{dodge_fuel}FUEL", bg))

        elif etype == "police":
            fine = random.randint(200, 500)
            confiscate = self.cargo["weapons"]
            self.event_data["fine"] = fine
            self.event_data["confiscate"] = confiscate

            self.set_key(1, render_event_title("POLICE\nSCAN!", "WEAPONS!", "#1e40af"))
            for k in range(2, 8):
                self.set_key(k, render_hud_empty())

            self.set_key(rc_to_pos(0, 2), render_event_choice("COMPLY", f"-${fine}", "#1e40af"))
            flee_dmg = random.randint(15, 35)
            self.event_data["flee_dmg"] = flee_dmg
            self.set_key(rc_to_pos(0, 5), render_event_choice("FLEE", f"-{flee_dmg}HP", "#7f1d1d"))

        elif etype == "trader":
            # Offer a random good at 30-50% of base
            g = random.choice(GOODS)
            discount_price = int(g["base"] * random.uniform(0.30, 0.50))
            qty = random.randint(2, 5)
            self.event_data["good"] = g["id"]
            self.event_data["price"] = discount_price
            self.event_data["qty"] = qty

            self.set_key(1, render_event_title("TRADER\nOFFERS!", f"{g['name']}!", "#065f46"))
            self.set_key(2, render_hud_credits(self.credits))
            self.set_key(3, render_hud_cargo(self._cargo_used(), self.cargo_max))
            for k in range(4, 8):
                self.set_key(k, render_hud_empty())

            total = discount_price * qty
            can_buy = self.credits >= total and self._cargo_used() + qty <= self.cargo_max
            bg = "#065f46" if can_buy else "#374151"
            self.set_key(rc_to_pos(0, 2), render_event_choice("BUY", f"{qty}x${discount_price}", bg))
            self.set_key(rc_to_pos(0, 5), render_event_choice("PASS", "NO DEAL", "#374151"))

        elif etype == "distress":
            reward = random.randint(200, 600)
            fuel_cost = 1
            self.event_data["reward"] = reward
            self.event_data["fuel_cost"] = fuel_cost

            self.set_key(1, render_event_title("DISTRESS\nSIGNAL!", "HELP?", "#854d0e"))
            for k in range(2, 8):
                self.set_key(k, render_hud_empty())

            can_help = self.fuel >= fuel_cost
            bg = "#065f46" if can_help else "#374151"
            self.set_key(rc_to_pos(0, 2), render_event_choice("HELP", f"-{fuel_cost}FUEL", bg))
            self.set_key(rc_to_pos(0, 5), render_event_choice("IGNORE", "FLY ON", "#374151"))

    def _resolve_event(self, choice):
        """choice: 0 = left option, 1 = right option."""
        result_lines = []

        if self.event_type == "pirates":
            if choice == 0:  # fight
                play_sfx("combat")
                play_voice("combat")
                pirate_str = self.event_data["pirate_str"]
                my_power = self.weapons * 2 + random.randint(0, 3)
                if my_power >= pirate_str:
                    # Win fight
                    loot = random.randint(50, 200) + self.turn * 3
                    self.credits += loot
                    result_lines = ["VICTORY!", f"+${loot}", "Pirates fled!"]
                else:
                    # Take damage
                    dmg = max(1, (pirate_str - self.shields) * random.randint(8, 15))
                    self.hp -= dmg
                    loot = random.randint(20, 80)
                    self.credits += loot
                    result_lines = ["PYRRHIC WIN", f"-{dmg}HP +${loot}", "Barely survived!"]
                    play_voice("damage")
            else:  # pay
                bounty = self.event_data["bounty"]
                if self.credits >= bounty:
                    self.credits -= bounty
                    result_lines = ["PAID OFF", f"-${bounty}", "Pirates leave"]
                else:
                    # Can't pay -- forced fight with penalty
                    dmg = max(5, self.event_data["pirate_str"] * 10)
                    self.hp -= dmg
                    result_lines = ["CAN'T PAY!", f"-{dmg}HP", "They attack!"]
                    play_sfx("damage")
                    play_voice("damage")

        elif self.event_type == "asteroid":
            if choice == 0:  # brace
                dmg = self.event_data["damage"]
                actual_dmg = max(1, dmg - self.shields * 2)
                self.hp -= actual_dmg
                result_lines = ["IMPACT!", f"-{actual_dmg}HP", f"Shields:{self.shields}"]
                play_sfx("damage")
            else:  # dodge
                dodge_fuel = self.event_data["dodge_fuel"]
                if self.fuel >= dodge_fuel:
                    self.fuel -= dodge_fuel
                    result_lines = ["DODGED!", f"-{dodge_fuel}FUEL", "Close call!"]
                else:
                    dmg = self.event_data["damage"]
                    self.hp -= dmg
                    result_lines = ["NO FUEL!", f"-{dmg}HP", "Can't dodge!"]
                    play_sfx("damage")

        elif self.event_type == "police":
            if choice == 0:  # comply
                fine = self.event_data["fine"]
                confiscated = self.event_data["confiscate"]
                self.credits = max(0, self.credits - fine)
                self.cargo["weapons"] = 0
                result_lines = ["FINED!", f"-${fine}", f"-{confiscated} weapons"]
            else:  # flee
                flee_dmg = self.event_data["flee_dmg"]
                actual = max(1, flee_dmg - self.shields * 2)
                self.hp -= actual
                result_lines = ["ESCAPED!", f"-{actual}HP", "Kept cargo!"]
                play_sfx("damage")

        elif self.event_type == "trader":
            if choice == 0:  # buy
                g_id = self.event_data["good"]
                price = self.event_data["price"]
                qty = self.event_data["qty"]
                total = price * qty
                space = self.cargo_max - self._cargo_used()
                actual_qty = min(qty, space)
                actual_cost = price * actual_qty
                if self.credits >= actual_cost and actual_qty > 0:
                    self.credits -= actual_cost
                    self.cargo[g_id] += actual_qty
                    result_lines = ["BOUGHT!", f"+{actual_qty} {GOODS_BY_ID[g_id]['name']}", f"-${actual_cost}"]
                    play_sfx("buy")
                else:
                    result_lines = ["CAN'T BUY!", "No space/cash", "Deal lost!"]
                    play_sfx("error")
            else:  # pass
                result_lines = ["PASSED", "No deal", "Fly on..."]

        elif self.event_type == "distress":
            if choice == 0:  # help
                fuel_cost = self.event_data["fuel_cost"]
                if self.fuel >= fuel_cost:
                    self.fuel -= fuel_cost
                    reward = self.event_data["reward"]
                    self.credits += reward
                    result_lines = ["RESCUED!", f"+${reward}", f"-{fuel_cost}FUEL"]
                else:
                    result_lines = ["NO FUEL!", "Can't help", "Fly on..."]
            else:  # ignore
                result_lines = ["IGNORED", "Signal fades", "..."]

        # Check death
        if self.hp <= 0:
            self._game_over()
            return

        # Show result
        self.mode = "result"
        for k in range(8, 32):
            self.set_key(k, self.img_empty)
        self.set_key(1, render_hud_credits(self.credits))
        self.set_key(2, render_hud_ship(self.hp, self.max_hp, self.fuel))
        for k in range(3, 8):
            self.set_key(k, render_hud_empty())

        self.set_key(rc_to_pos(0, 3), render_event_result(result_lines, "#1f2937"))
        self.set_key(rc_to_pos(1, 3), render_action_btn("OK", "CONTINUE", "#1e40af"))

        self._save_game()

    # -- win / game over ---------------------------------------------------

    def _check_win(self):
        if not self.won and self.credits >= WIN_CREDITS:
            # Don't auto-win, just update the HUD to show the WIN button
            pass

    def _buy_station(self):
        if self.credits < WIN_CREDITS:
            play_sfx("error")
            return
        self.won = True
        self.credits -= WIN_CREDITS

        # Save best score (fewer turns = better)
        if self.best_score == 0 or self.turn < self.best_score:
            scores.save_best("trader", self.turn)
            self.best_score = self.turn

        self._save_game()
        play_sfx("win")
        play_voice("win")

        # Win animation
        def _animate():
            for _ in range(3):
                for k in range(8, 32):
                    self.set_key(k, render_win_tile("WIN!"))
                time.sleep(0.4)
                for k in range(8, 32):
                    self.set_key(k, render_win_tile())
                time.sleep(0.3)
            # Show final screen
            self.set_key(1, render_title("SPACE", "STATION!"))
            self.set_key(2, render_title(f"T{self.turn}", "TURNS"))
            self.set_key(3, render_title("BEST", f"{self.best_score}"))
            for k in range(4, 8):
                self.set_key(k, render_hud_empty())
            for k in range(8, 32):
                self.set_key(k, self.img_empty)
            self.set_key(rc_to_pos(0, 3), render_btn("KEEP", "PLAYING", "#065f46"))
            self.set_key(rc_to_pos(1, 3), render_btn("NEW", "GAME", "#1e40af"))
            self.mode = "win_screen"

        t = threading.Thread(target=_animate, daemon=True)
        t.start()

    def _game_over(self):
        self.mode = "gameover"
        self.running = False
        self._delete_save()
        play_sfx("damage")
        play_voice("death")

        # Clear all
        for k in range(1, 32):
            self.set_key(k, self.img_empty)

        self.set_key(1, render_title("GAME", "OVER"))
        self.set_key(2, render_title(f"T{self.turn}", "TURNS"))
        self.set_key(3, render_title(f"{_compact(self.credits)}$"))

        for k in range(8, 32):
            self.set_key(k, render_gameover_tile(""))
        self.set_key(rc_to_pos(0, 3), render_gameover_tile("SHIP"))
        self.set_key(rc_to_pos(0, 4), render_gameover_tile("DESTROYED"))
        self.set_key(rc_to_pos(1, 3), render_btn("NEW", "GAME", "#065f46"))

    # -- key handler -------------------------------------------------------

    def on_key(self, _deck, key, pressed):
        if not pressed:
            return
        with self.lock:
            if self.mode == "idle":
                self._on_idle(key)
            elif self.mode == "planet":
                self._on_planet(key)
            elif self.mode == "travel":
                self._on_travel(key)
            elif self.mode == "event":
                self._on_event(key)
            elif self.mode == "result":
                self._on_result(key)
            elif self.mode == "gameover":
                self._on_gameover(key)
            elif self.mode == "win_screen":
                self._on_win_screen(key)

    def _on_idle(self, key):
        has_save = os.path.exists(SAVE_FILE)
        if has_save:
            if key == 12:
                self._continue()
            elif key == 20:
                self._start_new()
        else:
            if key == 16:
                self._start_new()

    def _on_planet(self, key):
        # Key 7: buy station if eligible
        if key == 7 and not self.won and self.credits >= WIN_CREDITS:
            self._buy_station()
            return

        # Row 0 (keys 8-13): buy goods
        if 8 <= key <= 13:
            good_idx = key - 8
            self._buy_good(good_idx)
            return

        # Key 14: travel
        if key == rc_to_pos(0, 6):
            if self.fuel <= 0:
                play_sfx("error")
                return
            self._render_travel_screen()
            return

        # Key 15: save
        if key == rc_to_pos(0, 7):
            self._save_game()
            return

        # Row 1 (keys 16-21): sell goods
        if 16 <= key <= 21:
            good_idx = key - 16
            self._sell_good(good_idx)
            return

        # Row 2: upgrades
        if key == rc_to_pos(2, 0):
            self._upgrade_cargo()
            return
        if key == rc_to_pos(2, 1):
            self._upgrade_weapons()
            return
        if key == rc_to_pos(2, 2):
            self._upgrade_shields()
            return
        if key == rc_to_pos(2, 3):
            self._upgrade_speed()
            return
        if key == rc_to_pos(2, 4):
            self._repair_ship()
            return
        if key == rc_to_pos(2, 5):
            self._upgrade_hull()
            return

    def _on_travel(self, key):
        # Key 7: back to planet
        if key == 7:
            self._render_planet_screen()
            return

        # Row 0 (keys 8-15): planet selection
        if 8 <= key <= 15:
            dest_idx = key - 8
            if dest_idx < len(PLANETS):
                self._travel_to(dest_idx)
            return

    def _on_event(self, key):
        # Left choice: rc(0, 2) = key 10
        if key == rc_to_pos(0, 2):
            self._resolve_event(0)
            return
        # Right choice: rc(0, 5) = key 13
        if key == rc_to_pos(0, 5):
            self._resolve_event(1)
            return

    def _on_result(self, key):
        # OK button at rc(1, 3) = key 19
        if key == rc_to_pos(1, 3):
            self._render_planet_screen()
            return

    def _on_gameover(self, key):
        if key == rc_to_pos(1, 3):
            self._start_new()
            return

    def _on_win_screen(self, key):
        if key == rc_to_pos(0, 3):
            # Keep playing
            self.mode = "planet"
            self._render_planet_screen()
            return
        if key == rc_to_pos(1, 3):
            # New game
            self._start_new()
            return

# -- main ------------------------------------------------------------------

def main():
    decks = DeviceManager().enumerate()
    deck = None
    for d in decks:
        if d.is_visual():
            deck = d
            break
    if not deck:
        print("No Stream Deck found!")
        sys.exit(1)

    try:
        _generate_sfx()
        print("Sound effects: ON")
    except Exception:
        print("Sound effects: OFF")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("SPACE TRADER -- buy low, sell high, reach 50K!")

    game = TraderGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nSaving...")
        if game.running:
            game._save_game()
    finally:
        game._cancel_all_timers()
        deck.reset()
        deck.close()
        cleanup_sfx()

if __name__ == "__main__":
    main()
