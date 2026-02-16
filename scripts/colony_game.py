"""Colony Builder — Stream Deck strategy game.

Build and manage a space colony on a 3x8 grid (24 tiles).
Manage energy, ore, credits and science. Unlock tech tiers,
upgrade buildings, and ultimately build a spaceport to win.
Game auto-saves so you can resume between sessions.

Voice pack: TF2 Engineer

Usage:
    uv run python scripts/colony_game.py
"""

import json
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

import sound_engine

# -- config ----------------------------------------------------------------
ROWS = 3
COLS = 8
ROW_OFFSET = 1  # game row 0 = deck row 1
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
TICK_INTERVAL = 3.0
SAVE_INTERVAL = 30.0
UPGRADE_TIMEOUT = 3.0
SAVE_FILE = os.path.expanduser("~/.streamdeck-arcade/colony_save.json")
START_CREDITS = 150

# Tech thresholds (cumulative science needed)
TECH_THRESHOLDS = [0, 50, 200, 600]

# -- building definitions -------------------------------------------------
BUILDING_TYPES = [
    {
        "id": "solar", "name": "SOLAR", "sub": "PANEL",
        "bg": "#ca8a04", "cost": 25,
        "energy": 2, "ore_prod": 0, "ore_cons": 0,
        "credit_prod": 0, "science_prod": 0, "tier": 0,
    },
    {
        "id": "mine", "name": "MINE", "sub": "",
        "bg": "#78716c", "cost": 40,
        "energy": -1, "ore_prod": 1, "ore_cons": 0,
        "credit_prod": 0, "science_prod": 0, "tier": 0,
    },
    {
        "id": "farm", "name": "FARM", "sub": "",
        "bg": "#15803d", "cost": 50,
        "energy": -1, "ore_prod": 0, "ore_cons": 0,
        "credit_prod": 2, "science_prod": 0, "tier": 0,
    },
    {
        "id": "lab", "name": "LAB", "sub": "",
        "bg": "#7c3aed", "cost": 120,
        "energy": -2, "ore_prod": 0, "ore_cons": 0,
        "credit_prod": 0, "science_prod": 1, "tier": 0,
    },
    {
        "id": "factory", "name": "FACT", "sub": "ORY",
        "bg": "#dc2626", "cost": 250,
        "energy": -2, "ore_prod": 0, "ore_cons": 2,
        "credit_prod": 6, "science_prod": 0, "tier": 1,
    },
    {
        "id": "plant", "name": "POWER", "sub": "PLANT",
        "bg": "#0369a1", "cost": 400,
        "energy": 6, "ore_prod": 0, "ore_cons": 2,
        "credit_prod": 0, "science_prod": 0, "tier": 2,
    },
]

BUILDING_BY_ID = {b["id"]: b for b in BUILDING_TYPES}

# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos):
    return pos // COLS - ROW_OFFSET, pos % COLS

def rc_to_pos(row, col):
    return (row + ROW_OFFSET) * COLS + col

def grid_neighbors(r, c):
    result = []
    for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
        nr, nc = r + dr, c + dc
        if 0 <= nr < ROWS and 0 <= nc < COLS:
            result.append((nr, nc))
    return result

# -- voice pack (TF2 Engineer) --------------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "tf2_engineer/sounds/Engineer_battlecry01.mp3",
        "tf2_engineer/sounds/Engineer_battlecry03.mp3",
        "tf2_engineer/sounds/Engineer_battlecry04.mp3",
    ],
    "build": [
        "tf2_engineer/sounds/Engineer_autobuildingsentry01.mp3",
        "tf2_engineer/sounds/Engineer_autobuildingsentry02.mp3",
        "tf2_engineer/sounds/Engineer_autobuildingdispenser01.mp3",
        "tf2_engineer/sounds/Engineer_autobuildingteleporter01.mp3",
    ],
    "upgrade": [
        "tf2_engineer/sounds/Engineer_specialcompleted02.mp3",
        "tf2_engineer/sounds/Engineer_specialcompleted08.mp3",
        "tf2_engineer/sounds/Engineer_specialcompleted09.mp3",
    ],
    "win": [
        "tf2_engineer/sounds/Eng_quest_complete_easy_01.mp3",
        "tf2_engineer/sounds/Eng_quest_complete_easy_02.mp3",
        "tf2_engineer/sounds/Eng_quest_complete_easy_04.mp3",
    ],
    "problem": [
        "tf2_engineer/sounds/Engineer_helpme01.mp3",
        "tf2_engineer/sounds/Engineer_helpme02.mp3",
    ],
}

_last_voice_time: float = 0
VOICE_COOLDOWN = 4.0

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
    _sfx_dir = tempfile.mkdtemp(prefix="colony-sfx-")
    v = SFX_VOLUME

    s = _square(220, 0.03, v*0.4) + _square(330, 0.03, v*0.5) + _triangle(440, 0.06, v*0.4)
    _write_wav(os.path.join(_sfx_dir, "build.wav"), s)
    _sfx_cache["build"] = os.path.join(_sfx_dir, "build.wav")

    s = (_triangle(440, 0.06, v*0.4) + _triangle(554, 0.06, v*0.45) +
         _triangle(659, 0.08, v*0.5) + _triangle(880, 0.12, v*0.55))
    _write_wav(os.path.join(_sfx_dir, "upgrade.wav"), s)
    _sfx_cache["upgrade"] = os.path.join(_sfx_dir, "upgrade.wav")

    s = _square(150, 0.1, v*0.3, 0.3) + _square(120, 0.1, v*0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "error.wav"), s)
    _sfx_cache["error"] = os.path.join(_sfx_dir, "error.wav")

    s = _square(800, 0.02, v*0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "select.wav"), s)
    _sfx_cache["select"] = os.path.join(_sfx_dir, "select.wav")

    s = (_triangle(523, 0.1, v*0.5) + _triangle(659, 0.1, v*0.55) +
         _triangle(784, 0.1, v*0.6) + _triangle(1047, 0.3, v*0.7))
    _write_wav(os.path.join(_sfx_dir, "win.wav"), s)
    _sfx_cache["win"] = os.path.join(_sfx_dir, "win.wav")

    s = (_triangle(220, 0.05, v*0.3) + _triangle(330, 0.05, v*0.35) +
         _triangle(440, 0.05, v*0.4) + _triangle(554, 0.08, v*0.45))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    s = _triangle(660, 0.08, v*0.5) + _triangle(880, 0.08, v*0.55) + _triangle(1047, 0.15, v*0.6)
    _write_wav(os.path.join(_sfx_dir, "unlock.wav"), s)
    _sfx_cache["unlock"] = os.path.join(_sfx_dir, "unlock.wav")

    s = _square(440, 0.05, v*0.4) + _square(330, 0.06, v*0.35) + _square(220, 0.08, v*0.3)
    _write_wav(os.path.join(_sfx_dir, "sell.wav"), s)
    _sfx_cache["sell"] = os.path.join(_sfx_dir, "sell.wav")

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
        return f"{n/1000:.1f}K"
    elif n < 1000000:
        return f"{n//1000}K"
    return f"{n/1000000:.1f}M"

# -- renderers -------------------------------------------------------------

def render_empty_plot(size=SIZE):
    img = Image.new("RGB", size, "#1a1a2e")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "+", font=_font(24), fill="#2d2d52", anchor="mm")
    return img

def _prod_label(info):
    parts = []
    if info["energy"] > 0: parts.append(f"+{info['energy']}E")
    elif info["energy"] < 0: parts.append(f"{info['energy']}E")
    if info["ore_prod"]: parts.append(f"+{info['ore_prod']}O")
    if info["ore_cons"]: parts.append(f"-{info['ore_cons']}O")
    if info["credit_prod"]: parts.append(f"+{info['credit_prod']}$")
    if info["science_prod"]: parts.append(f"+{info['science_prod']}S")
    return " ".join(parts)

def render_building(btype, level, offline=False, size=SIZE):
    info = BUILDING_BY_ID[btype]
    bg = info["bg"] if not offline else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 16), info["name"], font=_font(14), fill="white", anchor="mt")
    if info["sub"]:
        d.text((48, 33), info["sub"], font=_font(11), fill="#d1d5db", anchor="mt")
    d.text((48, 58), f"Lv{level}", font=_font(20), fill="#fbbf24", anchor="mm")
    prod = _prod_label(info)
    d.text((48, 84), prod, font=_font(9), fill="#86efac" if not offline else "#6b7280", anchor="mm")
    if offline:
        d.rectangle([2, 2, 93, 93], outline="#ef4444", width=2)
    return img

def render_upgrade_prompt(btype, level, cost, size=SIZE):
    info = BUILDING_BY_ID[btype]
    img = Image.new("RGB", size, info["bg"])
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 93, 93], outline="#fbbf24", width=3)
    d.text((48, 16), info["name"], font=_font(12), fill="white", anchor="mt")
    d.text((48, 40), f"Lv{level+1}", font=_font(18), fill="#fbbf24", anchor="mm")
    d.text((48, 62), f"{_compact(cost)}$", font=_font(14), fill="#86efac", anchor="mm")
    d.text((48, 82), "TAP!", font=_font(12), fill="#9ca3af", anchor="mm")
    return img

def render_hud_credits(amount, rate, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "CREDIT", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 42), _compact(amount), font=_font(22), fill="#fbbf24", anchor="mm")
    rc = "#86efac" if rate >= 0 else "#f87171"
    sign = "+" if rate >= 0 else ""
    d.text((48, 72), f"{sign}{rate}/t", font=_font(11), fill=rc, anchor="mm")
    return img

def render_hud_energy(balance, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "ENERGY", font=_font(10), fill="#9ca3af", anchor="mt")
    color = "#34d399" if balance >= 0 else "#ef4444"
    sign = "+" if balance > 0 else ""
    d.text((48, 44), f"{sign}{balance}", font=_font(28), fill=color, anchor="mm")
    d.text((48, 76), "E/tick", font=_font(10), fill="#6b7280", anchor="mm")
    return img

def render_hud_ore(amount, rate, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "ORE", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 42), _compact(amount), font=_font(22), fill="#a8a29e", anchor="mm")
    rc = "#86efac" if rate >= 0 else "#f87171"
    sign = "+" if rate >= 0 else ""
    d.text((48, 72), f"{sign}{rate}/t", font=_font(11), fill=rc, anchor="mm")
    return img

def render_hud_science(science, tech, next_thresh, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 8), f"TECH {tech}", font=_font(11), fill="#a78bfa", anchor="mt")
    d.text((48, 38), str(int(science)), font=_font(20), fill="#c4b5fd", anchor="mm")
    if next_thresh:
        pct = min(100, int(science / next_thresh * 100))
        # Progress bar
        bar_w = 60
        bar_x = 48 - bar_w // 2
        d.rectangle([bar_x, 58, bar_x + bar_w, 66], outline="#4b5563")
        fill_w = int(bar_w * pct / 100)
        if fill_w > 0:
            d.rectangle([bar_x, 58, bar_x + fill_w, 66], fill="#a78bfa")
        d.text((48, 80), f"{pct}%>T{tech+1}", font=_font(9), fill="#6b7280", anchor="mm")
    else:
        d.text((48, 68), "MAX", font=_font(14), fill="#fbbf24", anchor="mm")
    return img

def render_hud_info(buildings, tick, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 18), str(buildings), font=_font(22), fill="#60a5fa", anchor="mm")
    d.text((48, 42), "BUILDS", font=_font(9), fill="#6b7280", anchor="mm")
    m, s = divmod(tick * 3, 60)
    d.text((48, 68), f"{m}:{s:02d}", font=_font(14), fill="#374151", anchor="mm")
    return img

def render_hud_goal(text, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    lines = text.split("\n")
    y = 48 - len(lines) * 10
    for line in lines:
        d.text((48, y), line, font=_font(12), fill="#9ca3af", anchor="mt")
        y += 22
    return img

def render_build_btn(active=False, size=SIZE):
    bg = "#065f46" if not active else "#7f1d1d"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    label = "BUILD" if not active else "CANCEL"
    color = "#34d399" if not active else "#f87171"
    d.text((48, 32), "B", font=_font(28), fill="white", anchor="mm")
    d.text((48, 68), label, font=_font(12), fill=color, anchor="mm")
    return img

def render_build_option(info, selected, can_afford, locked, size=SIZE):
    if locked:
        img = Image.new("RGB", size, "#1f2937")
        d = ImageDraw.Draw(img)
        d.text((48, 34), "LOCK", font=_font(14), fill="#4b5563", anchor="mm")
        d.text((48, 60), f"T{info['tier']}", font=_font(12), fill="#4b5563", anchor="mm")
        return img
    bg = info["bg"] if can_afford else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    if selected:
        d.rectangle([2, 2, 93, 93], outline="#fbbf24", width=3)
    fill = "white" if can_afford else "#6b7280"
    d.text((48, 18), info["name"], font=_font(13), fill=fill, anchor="mt")
    if info["sub"]:
        d.text((48, 36), info["sub"], font=_font(10), fill="#d1d5db" if can_afford else "#4b5563", anchor="mt")
    cfill = "#86efac" if can_afford else "#6b7280"
    d.text((48, 68), f"{info['cost']}$", font=_font(14), fill=cfill, anchor="mm")
    return img

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

def render_port_btn(can_afford, size=SIZE):
    bg = "#7c3aed" if can_afford else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    fill = "white" if can_afford else "#4b5563"
    d.text((48, 16), "SPACE", font=_font(13), fill=fill, anchor="mt")
    d.text((48, 32), "PORT", font=_font(13), fill=fill, anchor="mt")
    d.text((48, 58), "5000$", font=_font(12), fill="#86efac" if can_afford else "#6b7280", anchor="mm")
    d.text((48, 78), "WIN!", font=_font(11), fill="#fbbf24" if can_afford else "#4b5563", anchor="mm")
    return img

def render_sell_prompt(btype, level, refund, size=SIZE):
    info = BUILDING_BY_ID[btype]
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 93, 93], outline="#ef4444", width=3)
    d.text((48, 14), info["name"], font=_font(12), fill="white", anchor="mt")
    d.text((48, 32), f"Lv{level}", font=_font(14), fill="#fbbf24", anchor="mm")
    d.text((48, 52), "SELL?", font=_font(16), fill="#f87171", anchor="mm")
    d.text((48, 72), f"+{_compact(refund)}$", font=_font(13), fill="#86efac", anchor="mm")
    d.text((48, 88), "TAP!", font=_font(10), fill="#9ca3af", anchor="mm")
    return img

def render_win_tile(size=SIZE):
    img = Image.new("RGB", size, "#7c3aed")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "W", font=_font(36), fill="#fbbf24", anchor="mm")
    return img

# -- game ------------------------------------------------------------------

class ColonyGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.mode = "idle"  # idle | playing | build
        self.credits = START_CREDITS
        self.ore = 0.0
        self.science = 0.0
        self.tech_level = 0
        self.tick_count = 0
        self.won = False
        self.grid = {}  # (r,c) -> {"type": str, "level": int}
        self.selected_build = 0
        self.upgrade_target = None
        self.upgrade_timer = None
        self.sell_target = None
        self.sell_timer = None
        self.offline = set()
        self.tick_timer = None
        self.save_timer = None
        self.timers = []
        self.img_empty = render_empty_plot()

    def set_key(self, pos, img):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _cancel_all_timers(self):
        for t in [self.tick_timer, self.save_timer, self.upgrade_timer, self.sell_timer]:
            if t:
                t.cancel()
        self.tick_timer = self.save_timer = self.upgrade_timer = self.sell_timer = None
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    # -- save / load -------------------------------------------------------

    def _save_game(self):
        data = {
            "credits": self.credits, "ore": self.ore,
            "science": self.science, "tech_level": self.tech_level,
            "tick_count": self.tick_count, "won": self.won,
            "grid": {f"{r},{c}": v for (r, c), v in self.grid.items()},
        }
        try:
            os.makedirs(os.path.dirname(SAVE_FILE), exist_ok=True)
            with open(SAVE_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass
        if self.running:
            self.save_timer = threading.Timer(SAVE_INTERVAL, self._save_game)
            self.save_timer.daemon = True
            self.save_timer.start()

    def _load_save(self):
        try:
            with open(SAVE_FILE) as f:
                data = json.load(f)
            self.credits = data.get("credits", START_CREDITS)
            self.ore = data.get("ore", 0)
            self.science = data.get("science", 0)
            self.tech_level = data.get("tech_level", 0)
            self.tick_count = data.get("tick_count", 0)
            self.won = data.get("won", False)
            self.grid = {}
            for key, val in data.get("grid", {}).items():
                r, c = map(int, key.split(","))
                self.grid[(r, c)] = val
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
        has_save = os.path.exists(SAVE_FILE)

        # HUD row
        self.set_key(1, render_title("COLONY", "BUILDER"))
        for k in range(2, 8):
            self.set_key(k, render_hud_empty())

        # Game area
        for k in range(8, 32):
            self.set_key(k, self.img_empty)

        if has_save:
            self._load_save()
            self.set_key(2, render_title(f"{_compact(self.credits)}$"))
            self.set_key(3, render_title(f"T{self.tech_level}", f"{len(self.grid)} bld"))
            self.set_key(12, render_btn("CONT", "INUE", "#1e40af", "white", "#93c5fd"))
            self.set_key(20, render_btn("NEW", "GAME"))
        else:
            self.set_key(16, render_btn("NEW", "GAME"))

    # -- start / continue --------------------------------------------------

    def _start_new(self):
        self._delete_save()
        self.credits = START_CREDITS
        self.ore = self.science = 0.0
        self.tech_level = self.tick_count = 0
        self.won = False
        self.grid = {}
        self.offline = set()
        self._begin_play()

    def _continue(self):
        self._load_save()
        self.offline = set()
        self._begin_play()

    def _begin_play(self):
        self.running = True
        self.mode = "playing"
        self.selected_build = 0
        self.upgrade_target = None
        play_sfx("start")
        play_voice("start")
        self._render_hud()
        self._render_grid()
        self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()
        self.save_timer = threading.Timer(SAVE_INTERVAL, self._save_game)
        self.save_timer.daemon = True
        self.save_timer.start()

    # -- rendering ---------------------------------------------------------

    def _render_hud(self):
        e_bal = self._energy_balance()
        o_rate = self._ore_rate()
        c_rate = self._credit_rate()
        nt = TECH_THRESHOLDS[self.tech_level + 1] if self.tech_level + 1 < len(TECH_THRESHOLDS) else None

        self.set_key(1, render_hud_credits(self.credits, c_rate))
        self.set_key(2, render_hud_energy(e_bal))
        self.set_key(3, render_hud_ore(self.ore, o_rate))
        self.set_key(4, render_hud_science(self.science, self.tech_level, nt))
        self.set_key(5, render_hud_info(len(self.grid), self.tick_count))

        if self.tech_level >= 3 and not self.won:
            self.set_key(6, render_port_btn(self.credits >= 5000))
        else:
            self.set_key(6, render_hud_goal(self._goal_text()))

        self.set_key(7, render_build_btn(self.mode == "build"))

    def _render_grid(self):
        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                if (r, c) in self.grid:
                    b = self.grid[(r, c)]
                    self.set_key(pos, render_building(b["type"], b["level"], (r, c) in self.offline))
                else:
                    self.set_key(pos, self.img_empty)

    def _render_build_hud(self):
        for i, bt in enumerate(BUILDING_TYPES):
            key = i + 1
            if key > 6:
                break
            locked = bt["tier"] > self.tech_level
            afford = self.credits >= bt["cost"]
            sel = (i == self.selected_build)
            self.set_key(key, render_build_option(bt, sel, afford, locked))
        self.set_key(7, render_build_btn(active=True))

    # -- resource math -----------------------------------------------------

    def _energy_balance(self):
        total = 0
        for b in self.grid.values():
            info = BUILDING_BY_ID[b["type"]]
            total += info["energy"] * b["level"]
        return total

    def _ore_rate(self):
        rate = 0
        for (r, c), b in self.grid.items():
            if (r, c) in self.offline:
                continue
            info = BUILDING_BY_ID[b["type"]]
            rate += info["ore_prod"] * b["level"]
            rate -= info["ore_cons"] * b["level"]
        return rate

    def _credit_rate(self):
        rate = 0
        for (r, c), b in self.grid.items():
            if (r, c) in self.offline:
                continue
            info = BUILDING_BY_ID[b["type"]]
            bonus = self._adj_bonus(r, c, b["type"])
            rate += int(info["credit_prod"] * b["level"] * bonus)
        return rate

    def _adj_bonus(self, r, c, btype):
        bonus = 1.0
        for nr, nc in grid_neighbors(r, c):
            if (nr, nc) in self.grid and self.grid[(nr, nc)]["type"] == btype:
                bonus += 0.2
        return bonus

    def _goal_text(self):
        if self.tech_level < len(TECH_THRESHOLDS) - 1:
            nt = TECH_THRESHOLDS[self.tech_level + 1]
            return f"GOAL:\n{nt}S > T{self.tech_level+1}"
        elif not self.won:
            return "BUILD\nSPACEPORT!"
        return "COLONY\nCOMPLETE!"

    # -- tick --------------------------------------------------------------

    def _tick(self):
        if not self.running:
            return
        with self.lock:
            self.tick_count += 1
            e_bal = self._energy_balance()

            # Determine offline buildings
            old_offline = self.offline.copy()
            self.offline = set()

            if e_bal < 0:
                deficit = abs(e_bal)
                consumers = []
                for (r, c), b in self.grid.items():
                    info = BUILDING_BY_ID[b["type"]]
                    if info["energy"] < 0:
                        consumers.append(((r, c), b, info))
                consumers.sort(key=lambda x: (-x[2]["tier"], x[0]))
                for (r, c), b, info in consumers:
                    if deficit <= 0:
                        break
                    self.offline.add((r, c))
                    deficit -= abs(info["energy"]) * b["level"]

            # Process active buildings
            for (r, c), b in self.grid.items():
                if (r, c) in self.offline:
                    continue
                info = BUILDING_BY_ID[b["type"]]
                bonus = self._adj_bonus(r, c, b["type"])
                ore_cons = info["ore_cons"] * b["level"]
                if ore_cons > 0 and self.ore < ore_cons:
                    self.offline.add((r, c))
                    continue
                self.ore += info["ore_prod"] * b["level"]
                self.ore -= ore_cons
                self.credits += int(info["credit_prod"] * b["level"] * bonus)
                self.science += info["science_prod"] * b["level"]

            self.ore = max(0, min(9999, self.ore))
            self.credits = max(0, min(999999, self.credits))
            self.science = min(9999, self.science)

            # Tech progression
            old_tech = self.tech_level
            for i in range(len(TECH_THRESHOLDS) - 1, -1, -1):
                if self.science >= TECH_THRESHOLDS[i]:
                    self.tech_level = i
                    break
            if self.tech_level > old_tech:
                play_sfx("unlock")
                play_voice("upgrade")

            if self.offline != old_offline:
                self._render_grid()
            self._render_hud()

        if self.running:
            self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
            self.tick_timer.daemon = True
            self.tick_timer.start()

    # -- build mode --------------------------------------------------------

    def _enter_build(self):
        self.mode = "build"
        self.selected_build = 0
        self._render_build_hud()
        play_sfx("select")

    def _exit_build(self):
        self.mode = "playing"
        self.upgrade_target = None
        self.sell_target = None
        self._render_hud()
        self._render_grid()

    def _select_type(self, idx):
        if idx >= len(BUILDING_TYPES):
            return
        bt = BUILDING_TYPES[idx]
        if bt["tier"] > self.tech_level:
            play_sfx("error")
            return
        self.selected_build = idx
        self._render_build_hud()
        play_sfx("select")

    def _build_at(self, r, c):
        if (r, c) in self.grid:
            return
        bt = BUILDING_TYPES[self.selected_build]
        if bt["tier"] > self.tech_level:
            play_sfx("error")
            return
        if self.credits < bt["cost"]:
            play_sfx("error")
            return
        self.credits -= bt["cost"]
        self.grid[(r, c)] = {"type": bt["id"], "level": 1}
        self.set_key(rc_to_pos(r, c), render_building(bt["id"], 1))
        play_sfx("build")
        play_voice("build")
        self._render_build_hud()

    # -- upgrade -----------------------------------------------------------

    def _upgrade_cost(self, btype, level):
        return int(BUILDING_BY_ID[btype]["cost"] * (1.5 ** level))

    def _start_upgrade(self, r, c):
        if (r, c) not in self.grid:
            return
        b = self.grid[(r, c)]
        cost = self._upgrade_cost(b["type"], b["level"])
        self.upgrade_target = (r, c)
        self.set_key(rc_to_pos(r, c), render_upgrade_prompt(b["type"], b["level"], cost))
        if self.upgrade_timer:
            self.upgrade_timer.cancel()
        self.upgrade_timer = threading.Timer(UPGRADE_TIMEOUT, self._cancel_upgrade)
        self.upgrade_timer.daemon = True
        self.upgrade_timer.start()
        play_sfx("select")

    def _confirm_upgrade(self, r, c):
        if (r, c) not in self.grid:
            return
        b = self.grid[(r, c)]
        cost = self._upgrade_cost(b["type"], b["level"])
        if self.credits < cost:
            play_sfx("error")
            self._cancel_upgrade()
            return
        self.credits -= cost
        b["level"] += 1
        self.set_key(rc_to_pos(r, c), render_building(b["type"], b["level"], (r, c) in self.offline))
        self.upgrade_target = None
        if self.upgrade_timer:
            self.upgrade_timer.cancel()
            self.upgrade_timer = None
        play_sfx("upgrade")
        play_voice("upgrade")
        self._render_hud()

    def _cancel_upgrade(self):
        if self.upgrade_target:
            r, c = self.upgrade_target
            if (r, c) in self.grid:
                b = self.grid[(r, c)]
                self.set_key(rc_to_pos(r, c), render_building(b["type"], b["level"], (r, c) in self.offline))
        self.upgrade_target = None
        if self.upgrade_timer:
            self.upgrade_timer.cancel()
            self.upgrade_timer = None

    # -- sell (demolish) ---------------------------------------------------

    def _sell_refund(self, btype, level):
        base = BUILDING_BY_ID[btype]["cost"]
        total_invested = int(base * sum(1.5 ** i for i in range(level)))
        return max(1, int(total_invested * 0.3))

    def _start_sell(self, r, c):
        if (r, c) not in self.grid:
            return
        b = self.grid[(r, c)]
        refund = self._sell_refund(b["type"], b["level"])
        self.sell_target = (r, c)
        self.set_key(rc_to_pos(r, c), render_sell_prompt(b["type"], b["level"], refund))
        if self.sell_timer:
            self.sell_timer.cancel()
        self.sell_timer = threading.Timer(UPGRADE_TIMEOUT, self._cancel_sell)
        self.sell_timer.daemon = True
        self.sell_timer.start()
        play_sfx("select")

    def _confirm_sell(self, r, c):
        if (r, c) not in self.grid:
            return
        b = self.grid[(r, c)]
        refund = self._sell_refund(b["type"], b["level"])
        self.credits += refund
        del self.grid[(r, c)]
        self.offline.discard((r, c))
        self.set_key(rc_to_pos(r, c), self.img_empty)
        self.sell_target = None
        if self.sell_timer:
            self.sell_timer.cancel()
            self.sell_timer = None
        play_sfx("sell")
        self._render_build_hud()

    def _cancel_sell(self):
        if self.sell_target:
            r, c = self.sell_target
            if (r, c) in self.grid:
                b = self.grid[(r, c)]
                self.set_key(rc_to_pos(r, c), render_building(b["type"], b["level"], (r, c) in self.offline))
            else:
                self.set_key(rc_to_pos(r, c), self.img_empty)
        self.sell_target = None
        if self.sell_timer:
            self.sell_timer.cancel()
            self.sell_timer = None

    # -- spaceport (win) ---------------------------------------------------

    def _build_spaceport(self):
        if self.credits < 5000:
            play_sfx("error")
            return
        self.credits -= 5000
        self.won = True
        self._save_game()
        play_sfx("win")
        play_voice("win")

        def _animate():
            for _ in range(3):
                for r in range(ROWS):
                    for c in range(COLS):
                        self.set_key(rc_to_pos(r, c), render_win_tile())
                time.sleep(0.5)
                self._render_grid()
                time.sleep(0.5)
            self._render_hud()

        t = threading.Thread(target=_animate, daemon=True)
        t.start()

    # -- key handler -------------------------------------------------------

    def on_key(self, _deck, key, pressed):
        if not pressed:
            return
        with self.lock:
            if self.mode == "idle":
                self._on_idle(key)
            elif self.mode == "playing":
                self._on_playing(key)
            elif self.mode == "build":
                self._on_build(key)

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

    def _on_playing(self, key):
        if key == 7:
            self._enter_build()
            return
        if key == 6 and self.tech_level >= 3 and not self.won:
            self._build_spaceport()
            return
        # Game grid
        if key < ROW_OFFSET * COLS or key >= (ROW_OFFSET + ROWS) * COLS:
            return
        r, c = pos_to_rc(key)
        if (r, c) in self.grid:
            if self.upgrade_target == (r, c):
                self._confirm_upgrade(r, c)
            else:
                self._cancel_upgrade()
                self._start_upgrade(r, c)
        else:
            self._cancel_upgrade()

    def _on_build(self, key):
        if key == 7:
            self._cancel_sell()
            self._exit_build()
            return
        if 1 <= key <= 6:
            self._cancel_sell()
            self._select_type(key - 1)
            return
        if key < ROW_OFFSET * COLS or key >= (ROW_OFFSET + ROWS) * COLS:
            return
        r, c = pos_to_rc(key)
        if (r, c) not in self.grid:
            self._cancel_sell()
            self._build_at(r, c)
        elif self.sell_target == (r, c):
            self._confirm_sell(r, c)
        else:
            self._cancel_sell()
            self._start_sell(r, c)

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
    print("COLONY BUILDER — manage your space colony!")

    game = ColonyGame(deck)
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
