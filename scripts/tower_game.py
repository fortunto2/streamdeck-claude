"""Tower Defense — Stream Deck XL strategy game.

Defend the path! Place towers to stop enemies from reaching the exit.
3x8 game grid with serpentine enemy path, 4 tower types, wave-based combat.

Voice pack: Warcraft Peasant

Usage:
    uv run python scripts/tower_game.py
"""

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
TICK_INTERVAL = 1.5
SPAWN_INTERVAL = 3  # ticks between spawns

START_GOLD = 100
START_LIVES = 20

# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos):
    return pos // COLS - ROW_OFFSET, pos % COLS

def rc_to_pos(row, col):
    return (row + ROW_OFFSET) * COLS + col

# -- path definition -------------------------------------------------------
# Serpentine path through the 3x8 grid (14 tiles):
#   Row 0: (0,0) -> (0,1)               (0,3) -> (0,4) -> (0,5)
#   Row 1:          (1,1)  (1,3)                          (1,5)
#   Row 2:          (2,1) -> (2,2) -> (2,3)  (2,5) -> (2,6) -> (2,7)
PATH = [
    (0, 0), (0, 1), (1, 1), (2, 1), (2, 2), (2, 3),
    (1, 3), (0, 3), (0, 4), (0, 5), (1, 5), (2, 5),
    (2, 6), (2, 7),
]
PATH_SET = set(PATH)
PATH_INDEX = {pos: i for i, pos in enumerate(PATH)}

# Direction from each path tile to the next (for drawing arrows)
def _path_directions():
    dirs = {}
    for i in range(len(PATH) - 1):
        r1, c1 = PATH[i]
        r2, c2 = PATH[i + 1]
        dirs[(r1, c1)] = (r2 - r1, c2 - c1)
    dirs[PATH[-1]] = (0, 0)  # exit tile
    return dirs

PATH_DIRS = _path_directions()

# Tower slots: all grid cells NOT on the path
TOWER_SLOTS = set()
for _r in range(ROWS):
    for _c in range(COLS):
        if (_r, _c) not in PATH_SET:
            TOWER_SLOTS.add((_r, _c))

# -- enemy types -----------------------------------------------------------
ENEMY_TYPES = {
    "rat":      {"hp": 5,  "gold": 5,  "color": "#22c55e", "name": "RAT"},
    "bat":      {"hp": 8,  "gold": 8,  "color": "#3b82f6", "name": "BAT"},
    "skeleton": {"hp": 15, "gold": 15, "color": "#e5e7eb", "name": "SKEL"},
    "orc":      {"hp": 25, "gold": 20, "color": "#ef4444", "name": "ORC"},
    "boss":     {"hp": 50, "gold": 50, "color": "#dc2626", "name": "BOSS"},
}

# -- tower types -----------------------------------------------------------
TOWER_TYPES = [
    {"id": "arrow",  "name": "ARROW",  "bg": "#15803d", "cost": 30,
     "damage": 2, "range": 2, "cooldown": 1, "splash": False, "slow": 0,
     "icon_color": "#22c55e"},
    {"id": "cannon", "name": "CANNON", "bg": "#991b1b", "cost": 60,
     "damage": 8, "range": 1, "cooldown": 2, "splash": True, "slow": 0,
     "icon_color": "#ef4444"},
    {"id": "ice",    "name": "ICE",    "bg": "#1e40af", "cost": 50,
     "damage": 1, "range": 2, "cooldown": 1, "splash": False, "slow": 3,
     "icon_color": "#60a5fa"},
    {"id": "laser",  "name": "LASER",  "bg": "#6b21a8", "cost": 100,
     "damage": 5, "range": 3, "cooldown": 1, "splash": False, "slow": 0,
     "icon_color": "#a855f7"},
]
TOWER_BY_ID = {t["id"]: t for t in TOWER_TYPES}

# -- wave definitions ------------------------------------------------------
WAVE_DEFS = [
    # (enemy_type, count) pairs per wave
    [("rat", 5)],
    [("rat", 8)],
    [("bat", 4)],
    [("bat", 6), ("rat", 2)],
    [("skeleton", 3), ("boss", 1)],
]

def _wave_enemies(wave_num):
    """Return list of enemy type strings for a given wave (1-indexed)."""
    if wave_num <= len(WAVE_DEFS):
        enemies = []
        for etype, count in WAVE_DEFS[wave_num - 1]:
            enemies.extend([etype] * count)
        return enemies
    # Scaling formula for wave 6+
    enemies = []
    n = wave_num
    # Rats taper off, harder types scale up
    if n < 10:
        enemies.extend(["rat"] * max(0, 10 - n))
    enemies.extend(["bat"] * min(n, 8))
    enemies.extend(["skeleton"] * max(0, n - 3))
    if n >= 8:
        enemies.extend(["orc"] * (n - 6))
    if n % 5 == 0:
        enemies.append("boss")
    # Scale HP with wave number
    return enemies

# -- voice pack (Warcraft Peasant) -----------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "peasant/sounds/PeasantReady1.wav",
        "peasant/sounds/PeasantYes1.wav",
        "peasant/sounds/PeasantYes2.wav",
    ],
    "kill": [
        "peasant/sounds/PeasantYesAttack1.wav",
        "peasant/sounds/PeasantYesAttack2.wav",
        "peasant/sounds/PeasantYesAttack3.wav",
        "peasant/sounds/PeasantYesAttack4.wav",
    ],
    "wave_complete": [
        "peasant/sounds/PeasantYes3.wav",
        "peasant/sounds/PeasantYes4.wav",
    ],
    "gameover": [
        "peasant/sounds/PeasantAngry1.wav",
        "peasant/sounds/PeasantAngry2.wav",
        "peasant/sounds/PeasantAngry3.wav",
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
    _sfx_dir = tempfile.mkdtemp(prefix="tower-sfx-")
    v = SFX_VOLUME

    # arrow_shoot — quick pew
    s = _square(880, 0.02, v * 0.4) + _square(1200, 0.03, v * 0.3) + _square(1600, 0.02, v * 0.2)
    _write_wav(os.path.join(_sfx_dir, "arrow_shoot.wav"), s)
    _sfx_cache["arrow_shoot"] = os.path.join(_sfx_dir, "arrow_shoot.wav")

    # cannon_boom — deep boom
    s = _square(80, 0.08, v * 0.6, 0.3) + _noise(0.12, v * 0.4) + _square(50, 0.1, v * 0.3, 0.4)
    _write_wav(os.path.join(_sfx_dir, "cannon_boom.wav"), s)
    _sfx_cache["cannon_boom"] = os.path.join(_sfx_dir, "cannon_boom.wav")

    # ice_slow — shimmer
    s = _triangle(1200, 0.04, v * 0.3) + _triangle(1400, 0.04, v * 0.25) + _triangle(1100, 0.06, v * 0.2)
    _write_wav(os.path.join(_sfx_dir, "ice_slow.wav"), s)
    _sfx_cache["ice_slow"] = os.path.join(_sfx_dir, "ice_slow.wav")

    # laser_zap — electronic zap
    s = _square(600, 0.03, v * 0.4, 0.3) + _square(900, 0.03, v * 0.5, 0.3) + _square(1500, 0.05, v * 0.3, 0.2)
    _write_wav(os.path.join(_sfx_dir, "laser_zap.wav"), s)
    _sfx_cache["laser_zap"] = os.path.join(_sfx_dir, "laser_zap.wav")

    # enemy_die — pop
    s = _square(400, 0.02, v * 0.5) + _square(600, 0.02, v * 0.4) + _square(200, 0.04, v * 0.3)
    _write_wav(os.path.join(_sfx_dir, "enemy_die.wav"), s)
    _sfx_cache["enemy_die"] = os.path.join(_sfx_dir, "enemy_die.wav")

    # wave_start — horn
    s = (_triangle(330, 0.1, v * 0.4) + _triangle(440, 0.1, v * 0.5) +
         _triangle(554, 0.15, v * 0.6) + _triangle(660, 0.2, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "wave_start.wav"), s)
    _sfx_cache["wave_start"] = os.path.join(_sfx_dir, "wave_start.wav")

    # game_over — sad trombone
    s = (_triangle(294, 0.2, v * 0.5) + _triangle(277, 0.2, v * 0.45) +
         _triangle(262, 0.2, v * 0.4) + _triangle(247, 0.4, v * 0.35))
    _write_wav(os.path.join(_sfx_dir, "game_over.wav"), s)
    _sfx_cache["game_over"] = os.path.join(_sfx_dir, "game_over.wav")

    # build — hammer tap
    s = _noise(0.02, v * 0.3) + _square(300, 0.03, v * 0.4) + _noise(0.02, v * 0.2)
    _write_wav(os.path.join(_sfx_dir, "build.wav"), s)
    _sfx_cache["build"] = os.path.join(_sfx_dir, "build.wav")

    # upgrade
    s = (_triangle(440, 0.06, v * 0.4) + _triangle(554, 0.06, v * 0.45) +
         _triangle(659, 0.08, v * 0.5) + _triangle(880, 0.12, v * 0.55))
    _write_wav(os.path.join(_sfx_dir, "upgrade.wav"), s)
    _sfx_cache["upgrade"] = os.path.join(_sfx_dir, "upgrade.wav")

    # select
    s = _square(800, 0.02, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "select.wav"), s)
    _sfx_cache["select"] = os.path.join(_sfx_dir, "select.wav")

    # error
    s = _square(150, 0.1, v * 0.3, 0.3) + _square(120, 0.1, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "error.wav"), s)
    _sfx_cache["error"] = os.path.join(_sfx_dir, "error.wav")

def play_sfx(name):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)

def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)

# -- renderers -------------------------------------------------------------

def _manhattan(r1, c1, r2, c2):
    return abs(r1 - r2) + abs(c1 - c2)

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

def render_path_tile(r, c, size=SIZE):
    """Render an empty path tile with direction indicators."""
    img = Image.new("RGB", size, "#1a1a1a")
    d = ImageDraw.Draw(img)
    dr, dc = PATH_DIRS.get((r, c), (0, 0))
    # Draw subtle direction dots along the path direction
    cx, cy = 48, 48
    if dr == 0 and dc == 0:
        # Exit tile
        d.text((48, 48), "EXIT", font=_font(10), fill="#4b5563", anchor="mm")
    else:
        # Draw dots showing direction
        for step in range(-1, 2):
            dx = cx + dc * step * 16
            dy = cy + dr * step * 16
            d.ellipse([dx - 2, dy - 2, dx + 2, dy + 2], fill="#2d2d2d")
        # Arrow head
        ax = cx + dc * 28
        ay = cy + dr * 28
        d.ellipse([ax - 3, ay - 3, ax + 3, ay + 3], fill="#3d3d3d")
    # Subtle border to show path
    d.rectangle([0, 0, 95, 95], outline="#222222", width=1)
    return img

def render_enemy_tile(enemy, size=SIZE):
    """Render a path tile with an enemy on it."""
    img = Image.new("RGB", size, "#1a1a1a")
    d = ImageDraw.Draw(img)
    einfo = ENEMY_TYPES[enemy["type"]]
    color = einfo["color"]
    # Draw enemy circle
    radius = 18 if enemy["type"] != "boss" else 24
    cx, cy = 48, 40
    d.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=color)
    # Slow indicator
    if enemy.get("slow_ticks", 0) > 0:
        d.ellipse([cx - radius - 3, cy - radius - 3, cx + radius + 3, cy + radius + 3],
                  outline="#60a5fa", width=2)
    # HP bar
    max_hp = enemy["max_hp"]
    cur_hp = enemy["hp"]
    bar_w = 70
    bar_h = 8
    bar_x = 48 - bar_w // 2
    bar_y = 72
    d.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], fill="#374151")
    hp_w = int(bar_w * cur_hp / max_hp)
    if hp_w > 0:
        hp_color = "#22c55e" if cur_hp > max_hp * 0.5 else "#eab308" if cur_hp > max_hp * 0.25 else "#ef4444"
        d.rectangle([bar_x, bar_y, bar_x + hp_w, bar_y + bar_h], fill=hp_color)
    # HP text
    d.text((48, 88), f"{cur_hp}", font=_font(9), fill="#9ca3af", anchor="mm")
    return img

def render_tower_tile(tower, size=SIZE):
    """Render a tower tile with icon and level."""
    tinfo = TOWER_BY_ID[tower["type"]]
    img = Image.new("RGB", size, tinfo["bg"])
    d = ImageDraw.Draw(img)
    ic = tinfo["icon_color"]
    cx, cy = 48, 38

    if tower["type"] == "arrow":
        # Triangle pointing up (arrow tower)
        d.polygon([(cx, cy - 18), (cx - 14, cy + 12), (cx + 14, cy + 12)], fill=ic)
    elif tower["type"] == "cannon":
        # Filled circle (cannonball)
        d.ellipse([cx - 16, cy - 16, cx + 16, cy + 16], fill=ic)
    elif tower["type"] == "ice":
        # Snowflake-like: lines radiating from center
        for angle in range(0, 360, 60):
            rad = math.radians(angle)
            ex = cx + int(16 * math.cos(rad))
            ey = cy + int(16 * math.sin(rad))
            d.line([(cx, cy), (ex, ey)], fill=ic, width=2)
        d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill=ic)
    elif tower["type"] == "laser":
        # Beam shape: vertical line with glow
        d.rectangle([cx - 3, cy - 18, cx + 3, cy + 18], fill=ic)
        d.ellipse([cx - 8, cy - 8, cx + 8, cy + 8], fill=ic)

    # Level number
    level = tower["level"]
    d.text((48, 76), f"Lv{level}", font=_font(14), fill="#fbbf24", anchor="mm")
    # Border
    d.rectangle([0, 0, 95, 95], outline="#000000", width=1)
    return img

def render_empty_slot(size=SIZE):
    """Render an empty buildable tile with subtle + mark."""
    img = Image.new("RGB", size, "#0f0f1a")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "+", font=_font(20), fill="#1a1a2e", anchor="mm")
    return img

def render_hud_wave(wave_num, enemies_left, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 12), "WAVE", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 42), str(wave_num), font=_font(24), fill="#fbbf24", anchor="mm")
    d.text((48, 72), f"{enemies_left} left", font=_font(11), fill="#6b7280", anchor="mm")
    return img

def render_hud_lives(lives, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 12), "LIVES", font=_font(10), fill="#9ca3af", anchor="mt")
    color = "#22c55e" if lives > 10 else "#eab308" if lives > 5 else "#ef4444"
    d.text((48, 48), str(lives), font=_font(28), fill=color, anchor="mm")
    # Hearts
    hearts = min(lives, 5)
    heart_str = "\u2665" * hearts
    d.text((48, 78), heart_str, font=_font(12), fill="#ef4444", anchor="mm")
    return img

def render_hud_gold(gold, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 12), "GOLD", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 48), str(gold), font=_font(22), fill="#fbbf24", anchor="mm")
    return img

def render_hud_kills(kills, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 12), "KILLS", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 48), str(kills), font=_font(22), fill="#f87171", anchor="mm")
    return img

def render_hud_tower_info(tower, size=SIZE):
    """Show info about a selected tower."""
    tinfo = TOWER_BY_ID[tower["type"]]
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), tinfo["name"], font=_font(12), fill=tinfo["icon_color"], anchor="mt")
    lvl = tower["level"]
    dmg = int(tinfo["damage"] * (1 + 0.5 * (lvl - 1)))
    d.text((48, 30), f"Lv{lvl}", font=_font(12), fill="#fbbf24", anchor="mt")
    d.text((48, 50), f"DMG:{dmg}", font=_font(10), fill="#e5e7eb", anchor="mt")
    d.text((48, 66), f"RNG:{tinfo['range']}", font=_font(10), fill="#e5e7eb", anchor="mt")
    cost = int(tinfo["cost"] * lvl)
    d.text((48, 82), f"UP:{cost}g", font=_font(10), fill="#86efac", anchor="mt")
    return img

def render_next_wave_btn(size=SIZE):
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 28), "NEXT", font=_font(16), fill="white", anchor="mm")
    d.text((48, 54), "WAVE", font=_font(16), fill="#34d399", anchor="mm")
    d.text((48, 78), "\u25b6", font=_font(14), fill="#fbbf24", anchor="mm")
    return img

def render_wave_active_btn(size=SIZE):
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.text((48, 28), "WAVE", font=_font(14), fill="white", anchor="mm")
    d.text((48, 50), "IN", font=_font(12), fill="#f87171", anchor="mm")
    d.text((48, 68), "PROG", font=_font(12), fill="#f87171", anchor="mm")
    return img

def render_build_btn(active=False, size=SIZE):
    bg = "#065f46" if not active else "#7f1d1d"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    label = "BUILD" if not active else "CANCEL"
    color = "#34d399" if not active else "#f87171"
    d.text((48, 30), "B", font=_font(28), fill="white", anchor="mm")
    d.text((48, 68), label, font=_font(12), fill=color, anchor="mm")
    return img

def render_build_option(tinfo, selected, can_afford, size=SIZE):
    bg = tinfo["bg"] if can_afford else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    if selected:
        d.rectangle([2, 2, 93, 93], outline="#fbbf24", width=3)
    fill = "white" if can_afford else "#6b7280"
    d.text((48, 18), tinfo["name"], font=_font(14), fill=fill, anchor="mt")
    cfill = "#86efac" if can_afford else "#6b7280"
    d.text((48, 46), f"{tinfo['cost']}g", font=_font(14), fill=cfill, anchor="mm")
    # Brief stats
    d.text((48, 68), f"D:{tinfo['damage']} R:{tinfo['range']}", font=_font(9), fill="#9ca3af", anchor="mm")
    if tinfo["slow"]:
        d.text((48, 82), "SLOW", font=_font(9), fill="#60a5fa", anchor="mm")
    elif tinfo["splash"]:
        d.text((48, 82), "SPLASH", font=_font(9), fill="#f87171", anchor="mm")
    return img

def render_upgrade_prompt(tower, size=SIZE):
    """Show upgrade option on a tower tile."""
    tinfo = TOWER_BY_ID[tower["type"]]
    cost = int(tinfo["cost"] * tower["level"])
    img = Image.new("RGB", size, tinfo["bg"])
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 93, 93], outline="#fbbf24", width=3)
    d.text((48, 14), tinfo["name"], font=_font(12), fill="white", anchor="mt")
    d.text((48, 38), f"Lv{tower['level'] + 1}", font=_font(18), fill="#fbbf24", anchor="mm")
    d.text((48, 60), f"{cost}g", font=_font(14), fill="#86efac", anchor="mm")
    d.text((48, 80), "TAP!", font=_font(12), fill="#9ca3af", anchor="mm")
    return img

# -- game ------------------------------------------------------------------

class TowerGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.mode = "idle"  # idle | playing | build | gameover
        self.gold = START_GOLD
        self.lives = START_LIVES
        self.wave_num = 0
        self.kills = 0
        self.tick_count = 0
        self.wave_active = False
        self.enemies = []  # list of enemy dicts on the path
        self.spawn_queue = []  # enemy types waiting to spawn
        self.towers = {}  # (r,c) -> tower dict
        self.selected_build = 0
        self.upgrade_target = None
        self.upgrade_timer = None
        self.tick_timer = None
        self.timers = []
        # Pre-render static images
        self._path_imgs = {}
        for r, c in PATH:
            self._path_imgs[(r, c)] = render_path_tile(r, c)
        self._empty_slot_img = render_empty_slot()

    def set_key(self, pos, img):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _cancel_all_timers(self):
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None
        if self.upgrade_timer:
            self.upgrade_timer.cancel()
            self.upgrade_timer = None
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    # -- idle screen -------------------------------------------------------

    def show_idle(self):
        self.running = False
        self.mode = "idle"
        self._cancel_all_timers()
        best = scores.load_best("tower", 0)

        # HUD row
        self.set_key(1, render_title("TOWER", "DEFENSE"))
        for k in range(2, 8):
            self.set_key(k, render_hud_empty())
        if best > 0:
            self.set_key(2, render_title(f"BEST", f"Wave {best}"))

        # Game area
        for k in range(8, 32):
            self.set_key(k, render_hud_empty())

        # Start button
        self.set_key(20, render_btn("START", "GAME"))

    # -- start game --------------------------------------------------------

    def _start_game(self):
        self.gold = START_GOLD
        self.lives = START_LIVES
        self.wave_num = 0
        self.kills = 0
        self.tick_count = 0
        self.wave_active = False
        self.enemies = []
        self.spawn_queue = []
        self.towers = {}
        self.selected_build = 0
        self.upgrade_target = None
        self.running = True
        self.mode = "playing"
        play_sfx("wave_start")
        play_voice("start")
        self._render_hud()
        self._render_grid()
        # Start tick loop
        self._schedule_tick()

    def _schedule_tick(self):
        if not self.running:
            return
        self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    # -- rendering ---------------------------------------------------------

    def _render_hud(self):
        enemies_left = len(self.enemies) + len(self.spawn_queue)
        self.set_key(1, render_hud_wave(self.wave_num, enemies_left))
        self.set_key(2, render_hud_lives(self.lives))
        self.set_key(3, render_hud_gold(self.gold))
        self.set_key(4, render_hud_kills(self.kills))
        # Key 5: tower info or empty
        if self.upgrade_target and self.upgrade_target in self.towers:
            self.set_key(5, render_hud_tower_info(self.towers[self.upgrade_target]))
        else:
            self.set_key(5, render_hud_empty())
        # Key 6: next wave or wave active
        if self.wave_active:
            self.set_key(6, render_wave_active_btn())
        else:
            self.set_key(6, render_next_wave_btn())
        # Key 7: build
        self.set_key(7, render_build_btn(self.mode == "build"))

    def _render_grid(self):
        """Render all game tiles."""
        # Build a map of enemy positions (use the one closest to exit per tile)
        enemy_on_tile = {}
        for enemy in self.enemies:
            pos = PATH[enemy["path_idx"]]
            if pos not in enemy_on_tile or enemy["path_idx"] > enemy_on_tile[pos]["path_idx"]:
                enemy_on_tile[pos] = enemy

        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                if (r, c) in PATH_SET:
                    if (r, c) in enemy_on_tile:
                        self.set_key(pos, render_enemy_tile(enemy_on_tile[(r, c)]))
                    else:
                        self.set_key(pos, self._path_imgs[(r, c)])
                elif (r, c) in self.towers:
                    self.set_key(pos, render_tower_tile(self.towers[(r, c)]))
                else:
                    self.set_key(pos, self._empty_slot_img)

    def _render_tile(self, r, c):
        """Render a single game tile."""
        pos = rc_to_pos(r, c)
        if (r, c) in PATH_SET:
            # Check for enemy at this tile
            enemy_here = None
            for enemy in self.enemies:
                epos = PATH[enemy["path_idx"]]
                if epos == (r, c):
                    if enemy_here is None or enemy["path_idx"] > enemy_here["path_idx"]:
                        enemy_here = enemy
            if enemy_here:
                self.set_key(pos, render_enemy_tile(enemy_here))
            else:
                self.set_key(pos, self._path_imgs[(r, c)])
        elif (r, c) in self.towers:
            self.set_key(pos, render_tower_tile(self.towers[(r, c)]))
        else:
            self.set_key(pos, self._empty_slot_img)

    def _render_build_hud(self):
        for i, ttype in enumerate(TOWER_TYPES):
            key = i + 1
            if key > 4:
                break
            afford = self.gold >= ttype["cost"]
            sel = (i == self.selected_build)
            self.set_key(key, render_build_option(ttype, sel, afford))
        # Keys 5-6 empty in build mode
        self.set_key(5, render_hud_empty())
        self.set_key(6, render_hud_empty())
        self.set_key(7, render_build_btn(active=True))

    # -- wave management ---------------------------------------------------

    def _start_wave(self):
        self.wave_num += 1
        self.wave_active = True
        enemy_types = _wave_enemies(self.wave_num)
        random.shuffle(enemy_types)
        self.spawn_queue = list(enemy_types)
        play_sfx("wave_start")
        self._render_hud()

    def _spawn_enemy(self):
        if not self.spawn_queue:
            return
        etype = self.spawn_queue.pop(0)
        einfo = ENEMY_TYPES[etype]
        # Scale HP for waves beyond 5
        hp_mult = 1.0
        if self.wave_num > 5:
            hp_mult = 1.0 + (self.wave_num - 5) * 0.15
        hp = int(einfo["hp"] * hp_mult)
        enemy = {
            "type": etype,
            "hp": hp,
            "max_hp": hp,
            "path_idx": 0,
            "slow_ticks": 0,
        }
        self.enemies.append(enemy)

    # -- tick (game loop) --------------------------------------------------

    def _tick(self):
        if not self.running:
            return
        with self.lock:
            self.tick_count += 1

            if self.wave_active:
                # Spawn enemies
                if self.spawn_queue and self.tick_count % SPAWN_INTERVAL == 0:
                    self._spawn_enemy()
                    # Also spawn on first tick of wave
                elif self.spawn_queue and len(self.enemies) == 0:
                    self._spawn_enemy()

                # Move enemies
                self._move_enemies()

                # Towers attack
                self._towers_attack()

                # Check wave complete
                if not self.enemies and not self.spawn_queue:
                    self.wave_active = False
                    play_sfx("wave_start")
                    play_voice("wave_complete")
                    # Save best wave
                    best = scores.load_best("tower", 0)
                    if self.wave_num > best:
                        scores.save_best("tower", self.wave_num)

                # Check game over
                if self.lives <= 0:
                    self._game_over()
                    return

            self._render_hud()
            self._render_grid()

        self._schedule_tick()

    def _move_enemies(self):
        """Move all enemies one step along the path."""
        reached_end = []
        for enemy in self.enemies:
            # Slowed enemies move every other tick
            if enemy.get("slow_ticks", 0) > 0:
                enemy["slow_ticks"] -= 1
                if self.tick_count % 2 != 0:
                    continue  # skip this tick

            enemy["path_idx"] += 1
            if enemy["path_idx"] >= len(PATH):
                reached_end.append(enemy)

        # Remove enemies that reached the end
        for enemy in reached_end:
            self.enemies.remove(enemy)
            self.lives -= 1
            play_sfx("error")

    def _towers_attack(self):
        """Each tower attacks the enemy closest to the exit within range."""
        if not self.enemies:
            return

        killed = []
        sfx_played = set()

        for (tr, tc), tower in self.towers.items():
            tinfo = TOWER_BY_ID[tower["type"]]
            # Cooldown check
            tower.setdefault("last_fire", 0)
            if self.tick_count - tower["last_fire"] < tinfo["cooldown"]:
                continue

            # Level-scaled damage
            level = tower["level"]
            damage = int(tinfo["damage"] * (1 + 0.5 * (level - 1)))
            t_range = tinfo["range"]

            # Find target: enemy closest to EXIT (highest path_idx) within range
            target = None
            for enemy in self.enemies:
                if enemy in killed:
                    continue
                er, ec = PATH[enemy["path_idx"]]
                dist = _manhattan(tr, tc, er, ec)
                if dist <= t_range:
                    if target is None or enemy["path_idx"] > target["path_idx"]:
                        target = enemy

            if target is None:
                continue

            tower["last_fire"] = self.tick_count

            # Apply damage
            target["hp"] -= damage

            # Splash damage (cannon)
            if tinfo["splash"]:
                target_pos = PATH[target["path_idx"]]
                for enemy in self.enemies:
                    if enemy is target or enemy in killed:
                        continue
                    epos = PATH[enemy["path_idx"]]
                    if _manhattan(target_pos[0], target_pos[1], epos[0], epos[1]) <= 1:
                        enemy["hp"] -= damage // 2

            # Slow (ice)
            if tinfo["slow"] > 0:
                target["slow_ticks"] = max(target.get("slow_ticks", 0), tinfo["slow"])

            # Play SFX (once per type per tick)
            sfx_map = {"arrow": "arrow_shoot", "cannon": "cannon_boom",
                       "ice": "ice_slow", "laser": "laser_zap"}
            sfx_name = sfx_map.get(tower["type"])
            if sfx_name and sfx_name not in sfx_played:
                play_sfx(sfx_name)
                sfx_played.add(sfx_name)

        # Remove dead enemies
        for enemy in list(self.enemies):
            if enemy["hp"] <= 0:
                self.enemies.remove(enemy)
                einfo = ENEMY_TYPES[enemy["type"]]
                # Scale gold reward slightly for later waves
                gold_reward = einfo["gold"]
                self.gold += gold_reward
                self.kills += 1
                if "enemy_die" not in sfx_played:
                    play_sfx("enemy_die")
                    sfx_played.add("enemy_die")
                play_voice("kill")

    # -- game over ---------------------------------------------------------

    def _game_over(self):
        self.running = False
        self.wave_active = False
        self.mode = "gameover"
        self._cancel_all_timers()
        play_sfx("game_over")
        play_voice("gameover")

        best = scores.load_best("tower", 0)
        if self.wave_num > best:
            scores.save_best("tower", self.wave_num)
            best = self.wave_num

        # Show game over screen
        for k in range(1, 8):
            self.set_key(k, render_hud_empty())
        self.set_key(1, render_title("GAME", "OVER"))
        self.set_key(2, render_title(f"WAVE", f"{self.wave_num}"))
        self.set_key(3, render_title(f"KILLS", f"{self.kills}"))
        self.set_key(4, render_title(f"BEST", f"Wave {best}"))

        for k in range(8, 32):
            self.set_key(k, render_hud_empty())
        self.set_key(20, render_btn("PLAY", "AGAIN"))

    # -- build mode --------------------------------------------------------

    def _enter_build(self):
        self.mode = "build"
        self.selected_build = 0
        self._cancel_upgrade()
        self._render_build_hud()
        play_sfx("select")

    def _exit_build(self):
        self.mode = "playing"
        self._render_hud()

    def _select_type(self, idx):
        if idx >= len(TOWER_TYPES):
            return
        self.selected_build = idx
        self._render_build_hud()
        play_sfx("select")

    def _build_at(self, r, c):
        if (r, c) in self.towers or (r, c) in PATH_SET:
            play_sfx("error")
            return
        if (r, c) not in TOWER_SLOTS:
            play_sfx("error")
            return
        ttype = TOWER_TYPES[self.selected_build]
        if self.gold < ttype["cost"]:
            play_sfx("error")
            return
        self.gold -= ttype["cost"]
        self.towers[(r, c)] = {"type": ttype["id"], "level": 1, "last_fire": 0}
        self.set_key(rc_to_pos(r, c), render_tower_tile(self.towers[(r, c)]))
        play_sfx("build")
        self._render_build_hud()

    # -- upgrade -----------------------------------------------------------

    def _start_upgrade(self, r, c):
        if (r, c) not in self.towers:
            return
        tower = self.towers[(r, c)]
        self.upgrade_target = (r, c)
        self.set_key(rc_to_pos(r, c), render_upgrade_prompt(tower))
        self.set_key(5, render_hud_tower_info(tower))
        if self.upgrade_timer:
            self.upgrade_timer.cancel()
        self.upgrade_timer = threading.Timer(3.0, self._cancel_upgrade)
        self.upgrade_timer.daemon = True
        self.upgrade_timer.start()
        play_sfx("select")

    def _confirm_upgrade(self, r, c):
        if (r, c) not in self.towers:
            return
        tower = self.towers[(r, c)]
        tinfo = TOWER_BY_ID[tower["type"]]
        cost = int(tinfo["cost"] * tower["level"])
        if self.gold < cost:
            play_sfx("error")
            self._cancel_upgrade()
            return
        self.gold -= cost
        tower["level"] += 1
        self.set_key(rc_to_pos(r, c), render_tower_tile(tower))
        self.upgrade_target = None
        if self.upgrade_timer:
            self.upgrade_timer.cancel()
            self.upgrade_timer = None
        play_sfx("upgrade")
        self._render_hud()

    def _cancel_upgrade(self):
        if self.upgrade_target and self.upgrade_target in self.towers:
            r, c = self.upgrade_target
            self.set_key(rc_to_pos(r, c), render_tower_tile(self.towers[(r, c)]))
        self.upgrade_target = None
        if self.upgrade_timer:
            self.upgrade_timer.cancel()
            self.upgrade_timer = None
        # Clear tower info from HUD key 5
        if self.mode == "playing":
            self.set_key(5, render_hud_empty())

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
            elif self.mode == "gameover":
                self._on_gameover(key)

    def _on_idle(self, key):
        if key == 20:
            self._start_game()

    def _on_playing(self, key):
        # Key 6: next wave
        if key == 6 and not self.wave_active:
            self._start_wave()
            return
        # Key 7: build mode
        if key == 7:
            self._enter_build()
            return
        # Game grid
        if key < ROW_OFFSET * COLS or key >= (ROW_OFFSET + ROWS) * COLS:
            return
        r, c = pos_to_rc(key)
        if (r, c) in self.towers:
            if self.upgrade_target == (r, c):
                self._confirm_upgrade(r, c)
            else:
                self._cancel_upgrade()
                self._start_upgrade(r, c)
        else:
            self._cancel_upgrade()

    def _on_build(self, key):
        if key == 7:
            self._exit_build()
            return
        if 1 <= key <= 4:
            self._select_type(key - 1)
            return
        # Game grid
        if key < ROW_OFFSET * COLS or key >= (ROW_OFFSET + ROWS) * COLS:
            return
        r, c = pos_to_rc(key)
        if (r, c) in TOWER_SLOTS and (r, c) not in self.towers:
            self._build_at(r, c)
        elif (r, c) in self.towers:
            # Switch to upgrade mode on existing tower
            self._exit_build()
            self._start_upgrade(r, c)

    def _on_gameover(self, key):
        if key == 20:
            self._start_game()

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
    print("TOWER DEFENSE -- defend the path!")

    game = TowerGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        game._cancel_all_timers()
        deck.reset()
        deck.close()
        cleanup_sfx()

if __name__ == "__main__":
    main()
