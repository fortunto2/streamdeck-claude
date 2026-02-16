"""Mini Empire -- Stream Deck civilization strategy game.

Turn-based strategy on a scrolling 16x16 world map. Build cities,
raise armies, research tech, and conquer all enemy capitals to win.

Voice pack: Peon (Warcraft)

Usage:
    uv run python scripts/empire_game.py
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

WORLD_W = 16
WORLD_H = 16

VIEW_ROWS = 3
VIEW_COLS = 8

SAVE_FILE = os.path.expanduser("~/.streamdeck-arcade/empire_save.json")

# Player is centered at this screen position
CURSOR_CENTER_ROW = 1
CURSOR_CENTER_COL = 3

# Terrain types
T_PLAINS = 0
T_FOREST = 1
T_MOUNTAIN = 2
T_WATER = 3
T_DESERT = 4

TERRAIN_NAMES = ["Plains", "Forest", "Mountain", "Water", "Desert"]
TERRAIN_FOOD =  [2, 1, 0, 0, 0]
TERRAIN_PROD =  [0, 1, 2, 0, 0]
TERRAIN_GOLD =  [0, 0, 0, 0, 1]

# Terrain colors
TERRAIN_BG = {
    T_PLAINS:   "#4ade80",
    T_FOREST:   "#166534",
    T_MOUNTAIN: "#6b7280",
    T_WATER:    "#2563eb",
    T_DESERT:   "#d4a017",
}
TERRAIN_BG_DIM = {
    T_PLAINS:   "#1a3d20",
    T_FOREST:   "#0a2a14",
    T_MOUNTAIN: "#2d2f33",
    T_WATER:    "#0f1f5a",
    T_DESERT:   "#5a4410",
}

# Player colors
PLAYER_COLORS = {
    0: "#3b82f6",  # Blue (player)
    1: "#ef4444",  # Red (AI1)
    2: "#22c55e",  # Green (AI2)
    3: "#eab308",  # Yellow (AI3)
}
PLAYER_NAMES = {0: "YOU", 1: "RED", 2: "GRN", 3: "YEL"}

# Unit definitions
UNIT_TYPES = {
    "warrior": {"name": "Warrior", "hp": 5, "atk": 3, "move": 1, "cost": 10, "tech": 0, "icon": "W"},
    "archer":  {"name": "Archer",  "hp": 4, "atk": 4, "move": 1, "cost": 15, "tech": 1, "icon": "A"},
    "knight":  {"name": "Knight",  "hp": 8, "atk": 5, "move": 2, "cost": 25, "tech": 2, "icon": "K"},
}

# Tech tree
TECH_COST = [0, 50, 150, 300]
TECH_NAMES = ["Bronze", "Iron", "Steel", "Siege"]

# City founding cost
CITY_COST = 100
CITY_BASE_DEF = 5

# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos):
    return pos // COLS - ROW_OFFSET, pos % COLS

def rc_to_pos(row, col):
    return (row + ROW_OFFSET) * COLS + col

# -- voice pack (Peon) ----------------------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "peon/sounds/PeonReady1.wav",
    ],
    "build": [
        "peon/sounds/PeonYes1.wav",
        "peon/sounds/PeonYes2.wav",
        "peon/sounds/PeonYes3.wav",
    ],
    "attack": [
        "peon/sounds/PeonYesAttack1.wav",
        "peon/sounds/PeonYesAttack2.wav",
        "peon/sounds/PeonYesAttack3.wav",
    ],
    "win": [
        "peon/sounds/PeonWarcry1.wav",
    ],
    "select": [
        "peon/sounds/PeonWhat1.wav",
        "peon/sounds/PeonWhat2.wav",
        "peon/sounds/PeonWhat3.wav",
    ],
    "defeat": [
        "peon/sounds/PeonDeath.wav",
    ],
    "angry": [
        "peon/sounds/PeonAngry1.wav",
        "peon/sounds/PeonAngry2.wav",
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
    _sfx_dir = tempfile.mkdtemp(prefix="empire-sfx-")
    v = SFX_VOLUME

    # move: quiet step
    s = _noise(0.02, v * 0.15) + _square(200, 0.02, v * 0.1, 0.3)
    _write_wav(os.path.join(_sfx_dir, "move.wav"), s)
    _sfx_cache["move"] = os.path.join(_sfx_dir, "move.wav")

    # attack: sword clash
    s = _noise(0.04, v * 0.35) + _square(800, 0.03, v * 0.4, 0.3) + _noise(0.05, v * 0.25)
    _write_wav(os.path.join(_sfx_dir, "attack.wav"), s)
    _sfx_cache["attack"] = os.path.join(_sfx_dir, "attack.wav")

    # build: hammer
    s = _square(220, 0.03, v * 0.4) + _square(330, 0.03, v * 0.5) + _triangle(440, 0.06, v * 0.4)
    _write_wav(os.path.join(_sfx_dir, "build.wav"), s)
    _sfx_cache["build"] = os.path.join(_sfx_dir, "build.wav")

    # research: sparkle
    s = (_triangle(880, 0.04, v * 0.3) + _triangle(1100, 0.04, v * 0.35) +
         _triangle(1320, 0.04, v * 0.4) + _triangle(1760, 0.08, v * 0.45))
    _write_wav(os.path.join(_sfx_dir, "research.wav"), s)
    _sfx_cache["research"] = os.path.join(_sfx_dir, "research.wav")

    # capture: fanfare
    s = (_triangle(523, 0.08, v * 0.4) + _triangle(659, 0.08, v * 0.45) +
         _triangle(784, 0.08, v * 0.5) + _triangle(1047, 0.2, v * 0.55))
    _write_wav(os.path.join(_sfx_dir, "capture.wav"), s)
    _sfx_cache["capture"] = os.path.join(_sfx_dir, "capture.wav")

    # turn_end: drum
    s = _square(100, 0.06, v * 0.4, 0.3) + _square(80, 0.08, v * 0.3, 0.3)
    _write_wav(os.path.join(_sfx_dir, "turn_end.wav"), s)
    _sfx_cache["turn_end"] = os.path.join(_sfx_dir, "turn_end.wav")

    # defeat: sad descending
    s = (_square(400, 0.12, v * 0.4, 0.4) + _square(300, 0.12, v * 0.35, 0.4) +
         _square(200, 0.15, v * 0.3, 0.4) + _square(100, 0.25, v * 0.25, 0.4))
    _write_wav(os.path.join(_sfx_dir, "defeat.wav"), s)
    _sfx_cache["defeat"] = os.path.join(_sfx_dir, "defeat.wav")

    # select: click
    s = _square(800, 0.02, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "select.wav"), s)
    _sfx_cache["select"] = os.path.join(_sfx_dir, "select.wav")

    # win: victory fanfare
    s = (_triangle(523, 0.1, v * 0.5) + _triangle(659, 0.1, v * 0.55) +
         _triangle(784, 0.1, v * 0.6) + _triangle(1047, 0.15, v * 0.65) +
         _triangle(1318, 0.3, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "win.wav"), s)
    _sfx_cache["win"] = os.path.join(_sfx_dir, "win.wav")

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

# -- world generation ------------------------------------------------------

def _generate_world():
    """Generate a 16x16 world map with terrain clusters.
    ~20% water, rest is land. Ensures land connectivity.
    Returns 2D list [row][col] of terrain type ints.
    """
    tiles = [[T_PLAINS] * WORLD_W for _ in range(WORLD_H)]

    # Seed terrain clusters using random walk
    # Water clusters (~20%)
    water_seeds = random.randint(3, 5)
    for _ in range(water_seeds):
        r, c = random.randint(2, WORLD_H - 3), random.randint(2, WORLD_W - 3)
        for _ in range(8):
            if 1 <= r < WORLD_H - 1 and 1 <= c < WORLD_W - 1:
                tiles[r][c] = T_WATER
            r += random.randint(-1, 1)
            c += random.randint(-1, 1)

    # Forest clusters
    for _ in range(4):
        r, c = random.randint(1, WORLD_H - 2), random.randint(1, WORLD_W - 2)
        for _ in range(6):
            if 0 <= r < WORLD_H and 0 <= c < WORLD_W and tiles[r][c] != T_WATER:
                tiles[r][c] = T_FOREST
            r += random.randint(-1, 1)
            c += random.randint(-1, 1)

    # Mountain clusters
    for _ in range(3):
        r, c = random.randint(1, WORLD_H - 2), random.randint(1, WORLD_W - 2)
        for _ in range(5):
            if 0 <= r < WORLD_H and 0 <= c < WORLD_W and tiles[r][c] != T_WATER:
                tiles[r][c] = T_MOUNTAIN
            r += random.randint(-1, 1)
            c += random.randint(-1, 1)

    # Desert clusters
    for _ in range(3):
        r, c = random.randint(1, WORLD_H - 2), random.randint(1, WORLD_W - 2)
        for _ in range(5):
            if 0 <= r < WORLD_H and 0 <= c < WORLD_W and tiles[r][c] != T_WATER:
                tiles[r][c] = T_DESERT
            r += random.randint(-1, 1)
            c += random.randint(-1, 1)

    # Ensure the 4 corners (starting positions) are land
    corners = [(1, 1), (1, WORLD_W - 2), (WORLD_H - 2, 1), (WORLD_H - 2, WORLD_W - 2)]
    for cr, cc in corners:
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                nr, nc = cr + dr, cc + dc
                if 0 <= nr < WORLD_H and 0 <= nc < WORLD_W:
                    if tiles[nr][nc] == T_WATER:
                        tiles[nr][nc] = T_PLAINS

    # Ensure land connectivity via flood fill; if disconnected, bridge water
    land_tiles = set()
    for r in range(WORLD_H):
        for c in range(WORLD_W):
            if tiles[r][c] != T_WATER:
                land_tiles.add((r, c))

    if land_tiles:
        # BFS from first corner
        start = corners[0]
        visited = set()
        queue = [start]
        visited.add(start)
        while queue:
            cr, cc = queue.pop(0)
            for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nr, nc = cr + dr, cc + dc
                if (nr, nc) in land_tiles and (nr, nc) not in visited:
                    visited.add((nr, nc))
                    queue.append((nr, nc))

        # If some land tiles aren't reachable, turn water between them to plains
        unreachable = land_tiles - visited
        if unreachable:
            # Simple fix: convert water tiles between components to plains
            for r in range(WORLD_H):
                for c in range(WORLD_W):
                    if tiles[r][c] == T_WATER:
                        # Check if adjacent to both visited and unreachable land
                        adj_visited = False
                        adj_unreach = False
                        for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                            nr, nc = r + dr, c + dc
                            if (nr, nc) in visited:
                                adj_visited = True
                            if (nr, nc) in unreachable:
                                adj_unreach = True
                        if adj_visited and adj_unreach:
                            tiles[r][c] = T_PLAINS

    return tiles


# -- tile renderers --------------------------------------------------------

def _render_terrain(terrain, dim=False):
    """Render a basic terrain tile."""
    bg = TERRAIN_BG_DIM[terrain] if dim else TERRAIN_BG[terrain]
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)

    if not dim:
        if terrain == T_FOREST:
            # Draw trees
            for tx, ty in [(30, 55), (50, 45), (70, 58)]:
                d.polygon([(tx, ty - 20), (tx - 10, ty), (tx + 10, ty)],
                          fill="#0d4a1c")
                d.rectangle([tx - 2, ty, tx + 2, ty + 8], fill="#5a3a1a")
        elif terrain == T_MOUNTAIN:
            # Draw peaks
            d.polygon([(48, 20), (25, 70), (71, 70)], fill="#9ca3af")
            d.polygon([(48, 20), (40, 40), (56, 40)], fill="#e5e7eb")
        elif terrain == T_WATER:
            # Waves
            for y in [30, 50, 70]:
                d.arc([10, y - 6, 45, y + 6], 0, 180, fill="#60a5fa", width=2)
                d.arc([50, y - 6, 85, y + 6], 0, 180, fill="#60a5fa", width=2)
        elif terrain == T_DESERT:
            # Sand dots
            for _ in range(4):
                x, y = random.randint(20, 76), random.randint(20, 76)
                d.ellipse([x - 2, y - 2, x + 2, y + 2], fill="#c4940f")
    return img


def _render_city(terrain, owner, is_capital=False, prod_dots=0):
    """Render a city on terrain with owner color border."""
    bg = TERRAIN_BG[terrain]
    color = PLAYER_COLORS[owner]
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)

    # Owner color border
    d.rectangle([2, 2, 93, 93], outline=color, width=3)

    # House/castle
    d.rectangle([30, 35, 66, 70], fill="#4b3621")  # building body
    d.polygon([(28, 35), (48, 15), (68, 35)], fill="#8b0000")  # roof
    d.rectangle([42, 52, 54, 70], fill="#1a1a2e")  # door

    if is_capital:
        # Star on top
        d.text((48, 10), "*", font=_font(14), fill="#fbbf24", anchor="mm")

    # Production dots at bottom
    if prod_dots > 0:
        dot_x = 48 - (prod_dots * 5)
        for i in range(min(prod_dots, 8)):
            d.ellipse([dot_x + i * 10, 78, dot_x + i * 10 + 6, 84],
                      fill="#fbbf24")

    return img


def _render_army(terrain, owner, unit_type, hp, max_hp):
    """Render an army unit on terrain."""
    bg = TERRAIN_BG[terrain]
    color = PLAYER_COLORS[owner]
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)

    uinfo = UNIT_TYPES.get(unit_type, UNIT_TYPES["warrior"])
    icon = uinfo["icon"]

    # Owner color circle background
    d.ellipse([18, 12, 78, 62], fill=color, outline="white", width=2)

    # Unit icon letter
    d.text((48, 37), icon, font=_font(28), fill="white", anchor="mm")

    # HP bar at bottom
    bar_w = 60
    bar_x = 18
    bar_y = 72
    d.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 10], outline="#4b5563")
    frac = max(0, hp / max_hp) if max_hp > 0 else 0
    fill_w = max(1, int(bar_w * frac))
    bar_color = "#22c55e" if frac > 0.5 else "#eab308" if frac > 0.25 else "#ef4444"
    d.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + 10], fill=bar_color)

    return img


def _render_cursor(base_img):
    """Overlay a bright white blinking border on a tile image."""
    img = base_img.copy()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 95, 95], outline="#ffffff", width=3)
    d.rectangle([3, 3, 92, 92], outline="#fbbf24", width=2)
    return img


def _render_selected(base_img):
    """Overlay a selection highlight (cyan) on a tile."""
    img = base_img.copy()
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 95, 95], outline="#06b6d4", width=3)
    return img


def _render_fog():
    """Unexplored tile."""
    return Image.new("RGB", SIZE, "#0a0a0f")


# -- HUD renderers --------------------------------------------------------

CLR_HUD_BG = "#111827"

def _render_hud_gold(gold, income):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 8), "GOLD", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 38), str(gold), font=_font(20), fill="#eab308", anchor="mm")
    sign = "+" if income >= 0 else ""
    color = "#86efac" if income >= 0 else "#f87171"
    d.text((48, 64), f"{sign}{income}/t", font=_font(11), fill=color, anchor="mm")
    return img

def _render_hud_food(net_food):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 8), "FOOD", font=_font(10), fill="#9ca3af", anchor="mt")
    sign = "+" if net_food >= 0 else ""
    color = "#86efac" if net_food >= 0 else "#f87171"
    d.text((48, 42), f"{sign}{net_food}", font=_font(22), fill=color, anchor="mm")
    d.text((48, 70), "per turn", font=_font(9), fill="#6b7280", anchor="mm")
    return img

def _render_hud_turn(turn_num):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 8), "TURN", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 48), str(turn_num), font=_font(26), fill="#60a5fa", anchor="mm")
    return img

def _render_hud_tech(tech_level, can_research=False):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 6), "TECH", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 30), TECH_NAMES[tech_level], font=_font(13), fill="#a78bfa", anchor="mm")
    d.text((48, 48), f"Lv{tech_level}", font=_font(16), fill="#c4b5fd", anchor="mm")
    if can_research and tech_level < 3:
        cost = TECH_COST[tech_level + 1]
        d.rectangle([10, 62, 86, 86], fill="#7c3aed", outline="#a78bfa", width=1)
        d.text((48, 74), f"UP {cost}G", font=_font(11), fill="white", anchor="mm")
    elif tech_level >= 3:
        d.text((48, 74), "MAX", font=_font(12), fill="#fbbf24", anchor="mm")
    else:
        cost = TECH_COST[tech_level + 1]
        d.text((48, 74), f"Need {cost}G", font=_font(9), fill="#4b5563", anchor="mm")
    return img

def _render_hud_info(text):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    lines = text.split("\n")
    y = 48 - len(lines) * 10
    for line in lines:
        d.text((48, y), line, font=_font(11), fill="#d1d5db", anchor="mm")
        y += 20
    return img

def _render_hud_minimap(cursor_r, cursor_c):
    """Show a tiny minimap showing cursor quadrant."""
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 8), "MAP", font=_font(10), fill="#9ca3af", anchor="mt")
    # Draw 4x4 grid representing the world
    mx, my = 18, 22
    mw, mh = 60, 60
    d.rectangle([mx, my, mx + mw, my + mh], outline="#4b5563")
    # Grid lines
    for i in range(1, 4):
        d.line([mx + i * mw // 4, my, mx + i * mw // 4, my + mh], fill="#2d2d52")
        d.line([mx, my + i * mh // 4, mx + mw, my + i * mh // 4], fill="#2d2d52")
    # Cursor position dot
    cx = mx + int((cursor_c / WORLD_W) * mw)
    cy = my + int((cursor_r / WORLD_H) * mh)
    d.ellipse([cx - 4, cy - 4, cx + 4, cy + 4], fill="#fbbf24")
    return img

def _render_hud_end_turn():
    img = Image.new("RGB", SIZE, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 28), "END", font=_font(18), fill="white", anchor="mm")
    d.text((48, 56), "TURN", font=_font(16), fill="#fb923c", anchor="mm")
    return img

def _render_hud_empty():
    return Image.new("RGB", SIZE, CLR_HUD_BG)

def _render_title(text, sub=""):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 28), text, font=_font(16), fill="#fbbf24", anchor="mm")
    if sub:
        d.text((48, 56), sub, font=_font(12), fill="#9ca3af", anchor="mm")
    return img

def _render_btn(t1, t2, bg="#065f46", c1="white", c2="#34d399"):
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 34), t1, font=_font(16), fill=c1, anchor="mm")
    d.text((48, 60), t2, font=_font(14), fill=c2, anchor="mm")
    return img


# -- city menu renderers ---------------------------------------------------

def _render_city_menu_header(city):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    color = PLAYER_COLORS[city["owner"]]
    d.rectangle([2, 2, 93, 93], outline=color, width=2)
    d.text((48, 14), "CITY", font=_font(12), fill=color, anchor="mt")
    d.text((48, 36), f"P:{city['prod_acc']}", font=_font(13), fill="#fbbf24", anchor="mm")
    d.text((48, 56), f"F:{city.get('food', 0)}", font=_font(11), fill="#86efac", anchor="mm")
    d.text((48, 76), f"D:{city['defense']}", font=_font(11), fill="#60a5fa", anchor="mm")
    return img

def _render_build_unit_btn(utype, uinfo, can_afford, unlocked):
    if not unlocked:
        img = Image.new("RGB", SIZE, "#1f2937")
        d = ImageDraw.Draw(img)
        d.text((48, 34), "LOCK", font=_font(14), fill="#4b5563", anchor="mm")
        d.text((48, 60), f"T{uinfo['tech']}", font=_font(12), fill="#4b5563", anchor="mm")
        return img
    bg = "#1e3a5f" if can_afford else "#1f2937"
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    fill = "white" if can_afford else "#6b7280"
    d.text((48, 14), uinfo["icon"], font=_font(24), fill=fill, anchor="mt")
    d.text((48, 52), uinfo["name"][:6], font=_font(11), fill=fill, anchor="mm")
    cfill = "#86efac" if can_afford else "#6b7280"
    d.text((48, 72), f"{uinfo['cost']}P", font=_font(12), fill=cfill, anchor="mm")
    return img

def _render_back_btn():
    img = Image.new("RGB", SIZE, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.text((48, 34), "BACK", font=_font(16), fill="white", anchor="mm")
    return img


# -- game ------------------------------------------------------------------

class EmpireGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.mode = "idle"  # idle | playing | city_menu | victory | defeat

        # World state
        self.tiles = []
        self.turn = 1
        self.gold = 0
        self.tech_level = 0

        # Cursor position (world coords)
        self.cursor_r = 1
        self.cursor_c = 1

        # Cities: dict of (r,c) -> city dict
        # city: {owner, is_capital, defense, prod_acc, building: None|unit_type}
        self.cities = {}

        # Armies: dict of (r,c) -> army dict
        # army: {owner, type, hp, max_hp, atk, moved}
        self.armies = {}

        # Selected army (world coords or None)
        self.selected_army = None

        # Currently viewing city (world coords or None) for city menu
        self.menu_city = None

        # Fog of war: set of (r,c) the player has explored
        self.explored = set()

        # Action log
        self.action_log = ""

        # Pre-rendered
        self._img_fog = _render_fog()

        # Blink state for cursor
        self._cursor_blink = True
        self._blink_timer = None

    def set_key(self, pos, img):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # -- fog of war --------------------------------------------------------

    def _visibility_set(self):
        """Get all tiles currently visible to the player (owner 0)."""
        visible = set()
        # Cities and armies owned by player
        for (r, c), city in self.cities.items():
            if city["owner"] == 0:
                for dr in range(-3, 4):
                    for dc in range(-3, 4):
                        if abs(dr) + abs(dc) <= 3:
                            nr, nc = r + dr, c + dc
                            if 0 <= nr < WORLD_H and 0 <= nc < WORLD_W:
                                visible.add((nr, nc))
        for (r, c), army in self.armies.items():
            if army["owner"] == 0:
                for dr in range(-3, 4):
                    for dc in range(-3, 4):
                        if abs(dr) + abs(dc) <= 3:
                            nr, nc = r + dr, c + dc
                            if 0 <= nr < WORLD_H and 0 <= nc < WORLD_W:
                                visible.add((nr, nc))
        return visible

    def _update_explored(self):
        """Mark currently visible tiles as explored."""
        self.explored |= self._visibility_set()

    # -- view window -------------------------------------------------------

    def _view_origin(self):
        vr = self.cursor_r - CURSOR_CENTER_ROW
        vc = self.cursor_c - CURSOR_CENTER_COL
        vr = max(0, min(vr, WORLD_H - VIEW_ROWS))
        vc = max(0, min(vc, WORLD_W - VIEW_COLS))
        return vr, vc

    def _world_to_screen(self, wr, wc):
        vr, vc = self._view_origin()
        sr = wr - vr
        sc = wc - vc
        if 0 <= sr < VIEW_ROWS and 0 <= sc < VIEW_COLS:
            return sr, sc
        return None

    def _screen_to_world(self, sr, sc):
        vr, vc = self._view_origin()
        return vr + sr, vc + sc

    # -- resource calculations ---------------------------------------------

    def _city_production(self, r, c, city):
        """Calculate a city's per-turn production and food."""
        prod = 1  # base production so cities always build
        food = 0
        gold_inc = 1  # base gold
        # Check adjacent tiles
        for dr in range(-1, 2):
            for dc in range(-1, 2):
                nr, nc = r + dr, c + dc
                if 0 <= nr < WORLD_H and 0 <= nc < WORLD_W:
                    t = self.tiles[nr][nc]
                    food += TERRAIN_FOOD[t]
                    prod += TERRAIN_PROD[t]
                    gold_inc += TERRAIN_GOLD[t]
        # Tech bonuses
        if self.tech_level >= 1 and city["owner"] == 0:
            prod += 1
        return food, prod, gold_inc

    def _total_gold_income(self):
        """Calculate total gold income per turn for player."""
        total = 0
        for (r, c), city in self.cities.items():
            if city["owner"] == 0:
                _, _, g = self._city_production(r, c, city)
                total += g
        return total

    def _total_food(self):
        """Calculate net food for player (production - army upkeep)."""
        food_prod = 0
        for (r, c), city in self.cities.items():
            if city["owner"] == 0:
                f, _, _ = self._city_production(r, c, city)
                food_prod += f
        # Army upkeep: 1 food per unit
        army_count = sum(1 for a in self.armies.values() if a["owner"] == 0)
        return food_prod - army_count

    # -- rendering ---------------------------------------------------------

    def _render_tile_at(self, wr, wc):
        """Render a single world tile as PIL image."""
        visible = self._visibility_set()
        is_visible = (wr, wc) in visible
        is_explored = (wr, wc) in self.explored

        if not is_visible and not is_explored:
            return self._img_fog

        dim = not is_visible
        terrain = self.tiles[wr][wc]

        # Base tile
        base = _render_terrain(terrain, dim=dim)

        # City on this tile
        if (wr, wc) in self.cities and is_visible:
            city = self.cities[(wr, wc)]
            _, prod, _ = self._city_production(wr, wc, city)
            base = _render_city(terrain, city["owner"], city["is_capital"],
                                min(prod, 8))

        # Army on this tile
        if (wr, wc) in self.armies and is_visible:
            army = self.armies[(wr, wc)]
            base = _render_army(terrain, army["owner"], army["type"],
                                army["hp"], army["max_hp"])

        # Selection highlight
        if self.selected_army and (wr, wc) == self.selected_army:
            base = _render_selected(base)

        # Cursor
        if (wr, wc) == (self.cursor_r, self.cursor_c) and self._cursor_blink:
            base = _render_cursor(base)

        return base

    def _render_game_grid(self):
        for sr in range(VIEW_ROWS):
            for sc in range(VIEW_COLS):
                wr, wc = self._screen_to_world(sr, sc)
                if 0 <= wr < WORLD_H and 0 <= wc < WORLD_W:
                    img = self._render_tile_at(wr, wc)
                else:
                    img = self._img_fog
                pos = rc_to_pos(sr, sc)
                self.set_key(pos, img)

    def _render_hud(self):
        gold_inc = self._total_gold_income()
        net_food = self._total_food()
        can_research = (self.tech_level < 3 and
                        self.gold >= TECH_COST[self.tech_level + 1])

        self.set_key(1, _render_hud_gold(self.gold, gold_inc))
        self.set_key(2, _render_hud_food(net_food))
        self.set_key(3, _render_hud_turn(self.turn))
        self.set_key(4, _render_hud_tech(self.tech_level, can_research))

        # Key 5: selected info or cursor position
        if self.selected_army:
            ar, ac = self.selected_army
            army = self.armies.get((ar, ac))
            if army:
                uinfo = UNIT_TYPES[army["type"]]
                self.set_key(5, _render_hud_info(
                    f"{uinfo['name']}\nHP:{army['hp']}/{army['max_hp']}\nATK:{army['atk']}"))
            else:
                self.set_key(5, _render_hud_info(self.action_log or
                    f"({self.cursor_r},{self.cursor_c})"))
        elif self.action_log:
            self.set_key(5, _render_hud_info(self.action_log))
        else:
            terrain = self.tiles[self.cursor_r][self.cursor_c]
            self.set_key(5, _render_hud_info(
                f"({self.cursor_r},{self.cursor_c})\n{TERRAIN_NAMES[terrain]}"))

        self.set_key(6, _render_hud_minimap(self.cursor_r, self.cursor_c))
        self.set_key(7, _render_hud_end_turn())

    def _render_all(self):
        self._update_explored()
        self._render_hud()
        self._render_game_grid()

    # -- cursor blink ------------------------------------------------------

    def _start_blink(self):
        if self._blink_timer:
            self._blink_timer.cancel()
        self._cursor_blink = True

        def _blink():
            if not self.running:
                return
            self._cursor_blink = not self._cursor_blink
            # Only re-render the cursor tile
            scr = self._world_to_screen(self.cursor_r, self.cursor_c)
            if scr:
                sr, sc = scr
                pos = rc_to_pos(sr, sc)
                img = self._render_tile_at(self.cursor_r, self.cursor_c)
                self.set_key(pos, img)
            if self.running and self.mode == "playing":
                self._blink_timer = threading.Timer(0.5, _blink)
                self._blink_timer.daemon = True
                self._blink_timer.start()

        self._blink_timer = threading.Timer(0.5, _blink)
        self._blink_timer.daemon = True
        self._blink_timer.start()

    def _stop_blink(self):
        if self._blink_timer:
            self._blink_timer.cancel()
            self._blink_timer = None

    # -- idle screen -------------------------------------------------------

    def show_idle(self):
        self.running = False
        self.mode = "idle"
        self._stop_blink()

        best = scores.load_best("empire", 0)

        self.set_key(1, _render_title("MINI", "EMPIRE"))
        if best > 0:
            self.set_key(2, _render_title("BEST", f"{best} turns"))
        else:
            self.set_key(2, _render_hud_empty())
        for k in range(3, 8):
            self.set_key(k, _render_hud_empty())

        for k in range(8, 32):
            self.set_key(k, self._img_fog)

        # Check for save
        has_save = os.path.exists(SAVE_FILE)
        if has_save:
            self.set_key(12, _render_btn("CONT", "INUE", "#1e40af", "white", "#93c5fd"))
            self.set_key(20, _render_btn("NEW", "GAME"))
        else:
            self.set_key(20, _render_btn("START", "GAME"))

    # -- save / load -------------------------------------------------------

    def _save_game(self):
        cities_s = {}
        for (r, c), city in self.cities.items():
            cities_s[f"{r},{c}"] = city
        armies_s = {}
        for (r, c), army in self.armies.items():
            armies_s[f"{r},{c}"] = army

        data = {
            "turn": self.turn,
            "gold": self.gold,
            "tech_level": self.tech_level,
            "cursor_r": self.cursor_r,
            "cursor_c": self.cursor_c,
            "tiles": self.tiles,
            "cities": cities_s,
            "armies": armies_s,
            "explored": [list(p) for p in self.explored],
        }
        try:
            os.makedirs(os.path.dirname(SAVE_FILE), exist_ok=True)
            with open(SAVE_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def _load_save(self):
        try:
            with open(SAVE_FILE) as f:
                data = json.load(f)
            self.turn = data["turn"]
            self.gold = data["gold"]
            self.tech_level = data["tech_level"]
            self.cursor_r = data["cursor_r"]
            self.cursor_c = data["cursor_c"]
            self.tiles = data["tiles"]
            self.cities = {}
            for key, val in data.get("cities", {}).items():
                r, c = map(int, key.split(","))
                self.cities[(r, c)] = val
            self.armies = {}
            for key, val in data.get("armies", {}).items():
                r, c = map(int, key.split(","))
                self.armies[(r, c)] = val
            self.explored = set()
            for p in data.get("explored", []):
                self.explored.add((p[0], p[1]))
            return True
        except Exception:
            return False

    def _delete_save(self):
        try:
            os.remove(SAVE_FILE)
        except FileNotFoundError:
            pass

    # -- start / continue --------------------------------------------------

    def _start_new(self):
        self._delete_save()
        self.turn = 1
        self.gold = 50
        self.tech_level = 0
        self.selected_army = None
        self.menu_city = None
        self.action_log = "Empire founded!"
        self.explored = set()

        # Generate world
        self.tiles = _generate_world()

        # Place cities in corners
        corners = [(1, 1), (1, WORLD_W - 2), (WORLD_H - 2, 1), (WORLD_H - 2, WORLD_W - 2)]
        self.cities = {}
        self.armies = {}

        for i, (cr, cc) in enumerate(corners):
            self.cities[(cr, cc)] = {
                "owner": i,
                "is_capital": True,
                "defense": CITY_BASE_DEF,
                "prod_acc": 0,
                "building": None,
            }
            # Starting warriors near each city (AI gets 2, player gets 1)
            num_warriors = 2 if i > 0 else 1
            placed = 0
            for dr, dc in [(0, 1), (1, 0), (0, -1), (-1, 0)]:
                if placed >= num_warriors:
                    break
                nr, nc = cr + dr, cc + dc
                if (0 <= nr < WORLD_H and 0 <= nc < WORLD_W and
                        self.tiles[nr][nc] != T_WATER and
                        (nr, nc) not in self.cities and
                        (nr, nc) not in self.armies):
                    uinfo = UNIT_TYPES["warrior"]
                    self.armies[(nr, nc)] = {
                        "owner": i,
                        "type": "warrior",
                        "hp": uinfo["hp"],
                        "max_hp": uinfo["hp"],
                        "atk": uinfo["atk"],
                        "moved": False,
                    }
                    placed += 1

        self.cursor_r, self.cursor_c = corners[0]
        self._begin_play()

    def _continue_game(self):
        if self._load_save():
            self.selected_army = None
            self.menu_city = None
            self.action_log = "Resumed"
            self._begin_play()

    def _begin_play(self):
        self.running = True
        self.mode = "playing"
        play_sfx("select")
        play_voice("start")
        self._render_all()
        self._start_blink()

    # -- cursor movement ---------------------------------------------------

    def _move_cursor(self, wr, wc):
        """Move cursor to world position."""
        if not (0 <= wr < WORLD_H and 0 <= wc < WORLD_W):
            return
        self.cursor_r = wr
        self.cursor_c = wc
        self.action_log = ""

        # Show tile info
        visible = self._visibility_set()
        if (wr, wc) in visible:
            if (wr, wc) in self.cities:
                city = self.cities[(wr, wc)]
                owner = PLAYER_NAMES[city["owner"]]
                cap = " *CAP*" if city["is_capital"] else ""
                self.action_log = f"{owner} city{cap}"
            elif (wr, wc) in self.armies:
                army = self.armies[(wr, wc)]
                owner = PLAYER_NAMES[army["owner"]]
                uinfo = UNIT_TYPES[army["type"]]
                self.action_log = f"{owner} {uinfo['name']}\nHP:{army['hp']}"

        play_sfx("move")
        self._render_all()

    # -- army selection and movement ---------------------------------------

    def _select_army(self, r, c):
        """Select a player army at (r, c)."""
        army = self.armies.get((r, c))
        if not army or army["owner"] != 0:
            return
        if army["moved"]:
            self.action_log = "Already moved"
            play_sfx("error")
            self._render_all()
            return
        self.selected_army = (r, c)
        uinfo = UNIT_TYPES[army["type"]]
        self.action_log = f"{uinfo['name']} sel\nHP:{army['hp']}"
        play_sfx("select")
        play_voice("select")
        self._render_all()

    def _move_army(self, from_r, from_c, to_r, to_c):
        """Move selected army to target tile. May trigger combat."""
        army = self.armies.get((from_r, from_c))
        if not army or army["owner"] != 0:
            self.selected_army = None
            return
        if army["moved"]:
            self.selected_army = None
            self.action_log = "Already moved"
            play_sfx("error")
            self._render_all()
            return

        # Check range
        dist = abs(to_r - from_r) + abs(to_c - from_c)
        max_move = UNIT_TYPES[army["type"]]["move"]
        if self.tech_level >= 2:
            max_move += 1
        if dist > max_move:
            self.action_log = "Too far!"
            play_sfx("error")
            self._render_all()
            return

        # Check terrain
        if self.tiles[to_r][to_c] == T_WATER:
            self.action_log = "Can't cross\nwater!"
            play_sfx("error")
            self._render_all()
            return

        # Check for enemy army -> combat
        if (to_r, to_c) in self.armies:
            target = self.armies[(to_r, to_c)]
            if target["owner"] != 0:
                self._resolve_combat(from_r, from_c, to_r, to_c)
                return
            else:
                # Can't move onto own unit
                self.action_log = "Tile occupied"
                play_sfx("error")
                self._render_all()
                return

        # Check for enemy city
        if (to_r, to_c) in self.cities:
            city = self.cities[(to_r, to_c)]
            if city["owner"] != 0:
                # Attack city
                self._attack_city(from_r, from_c, to_r, to_c)
                return

        # Normal move
        del self.armies[(from_r, from_c)]
        army["moved"] = True
        self.armies[(to_r, to_c)] = army
        self.selected_army = None
        uinfo = UNIT_TYPES[army["type"]]
        self.action_log = f"{uinfo['name']} moved"
        play_sfx("move")
        self._render_all()

    def _resolve_combat(self, ar, ac, dr, dc):
        """Resolve combat between attacker at (ar,ac) and defender at (dr,dc)."""
        attacker = self.armies[(ar, ac)]
        defender = self.armies[(dr, dc)]

        play_sfx("attack")
        play_voice("attack")

        # Attacker hits defender
        defender["hp"] -= attacker["atk"]
        # Defender hits back
        attacker["hp"] -= defender["atk"]

        self.action_log = f"Battle!\n"

        # Check results
        if defender["hp"] <= 0:
            del self.armies[(dr, dc)]
            self.action_log += "Enemy slain!"
            if attacker["hp"] > 0:
                del self.armies[(ar, ac)]
                attacker["moved"] = True
                self.armies[(dr, dc)] = attacker
            else:
                del self.armies[(ar, ac)]
                self.action_log += "\nBoth fell!"
        elif attacker["hp"] <= 0:
            del self.armies[(ar, ac)]
            self.action_log += "Unit lost!"
        else:
            attacker["moved"] = True
            self.action_log += f"ATK:{attacker['hp']}HP\nDEF:{defender['hp']}HP"

        self.selected_army = None
        self._render_all()
        self._check_defeat()

    def _attack_city(self, ar, ac, cr, cc):
        """Attack an enemy city."""
        attacker = self.armies[(ar, ac)]
        city = self.cities[(cr, cc)]

        play_sfx("attack")
        play_voice("attack")

        # Check if city has defenders
        # City has base defense HP
        city_hp = city["defense"]
        if self.tech_level >= 3:
            attacker_atk = attacker["atk"] + 3  # siege bonus
        else:
            attacker_atk = attacker["atk"]

        city["defense"] -= attacker_atk
        attacker["hp"] -= 2  # city fights back a bit

        if city["defense"] <= 0:
            # City captured!
            city["defense"] = CITY_BASE_DEF
            old_owner = city["owner"]
            city["owner"] = attacker["owner"]
            city["prod_acc"] = 0
            city["building"] = None

            del self.armies[(ar, ac)]
            if attacker["hp"] > 0:
                attacker["moved"] = True
                self.armies[(cr, cc)] = attacker

            if city["is_capital"]:
                self.action_log = f"CAPITAL\nCAPTURED!"
                play_sfx("capture")
            else:
                self.action_log = f"City taken!"
                play_sfx("capture")

            self.selected_army = None
            self._render_all()
            self._check_victory()
        else:
            if attacker["hp"] <= 0:
                del self.armies[(ar, ac)]
                self.action_log = f"Unit lost!\nCity def:{city['defense']}"
            else:
                attacker["moved"] = True
                self.action_log = f"City hit!\nDef:{city['defense']}"
            self.selected_army = None
            self._render_all()
            self._check_defeat()

    # -- city menu ---------------------------------------------------------

    def _open_city_menu(self, r, c):
        """Open city production menu."""
        city = self.cities.get((r, c))
        if not city or city["owner"] != 0:
            return
        self.menu_city = (r, c)
        self.mode = "city_menu"
        self._render_city_menu()

    def _render_city_menu(self):
        r, c = self.menu_city
        city = self.cities[(r, c)]
        _, prod, _ = self._city_production(r, c, city)

        # HUD row for city menu
        self.set_key(1, _render_city_menu_header(city))

        # Build unit buttons on keys 2-4
        unit_list = ["warrior", "archer", "knight"]
        for i, utype in enumerate(unit_list):
            uinfo = UNIT_TYPES[utype]
            unlocked = self.tech_level >= uinfo["tech"]
            can_afford = city["prod_acc"] >= uinfo["cost"]
            self.set_key(2 + i, _render_build_unit_btn(utype, uinfo,
                                                        can_afford, unlocked))

        self.set_key(5, _render_hud_info(f"Prod/t: {prod}\nAccum: {city['prod_acc']}"))
        self.set_key(6, _render_hud_empty())
        self.set_key(7, _render_back_btn())

        # Keep game grid visible
        self._render_game_grid()

    def _build_unit(self, utype):
        """Build a unit at the menu city."""
        if not self.menu_city:
            return
        r, c = self.menu_city
        city = self.cities.get((r, c))
        if not city or city["owner"] != 0:
            return
        uinfo = UNIT_TYPES[utype]
        if self.tech_level < uinfo["tech"]:
            play_sfx("error")
            return
        if city["prod_acc"] < uinfo["cost"]:
            play_sfx("error")
            self.action_log = "Need more\nproduction!"
            self._render_city_menu()
            return

        # Find empty adjacent tile to spawn
        spawn_pos = None
        for dr, dc in [(0, 1), (1, 0), (0, -1), (-1, 0), (1, 1), (-1, 1), (1, -1), (-1, -1)]:
            nr, nc = r + dr, c + dc
            if (0 <= nr < WORLD_H and 0 <= nc < WORLD_W and
                    self.tiles[nr][nc] != T_WATER and
                    (nr, nc) not in self.armies):
                spawn_pos = (nr, nc)
                break

        if not spawn_pos:
            self.action_log = "No space\nfor unit!"
            play_sfx("error")
            self._render_city_menu()
            return

        city["prod_acc"] -= uinfo["cost"]
        self.armies[spawn_pos] = {
            "owner": 0,
            "type": utype,
            "hp": uinfo["hp"],
            "max_hp": uinfo["hp"],
            "atk": uinfo["atk"],
            "moved": True,  # Can't move on spawn turn
        }
        self.action_log = f"{uinfo['name']}\ntrained!"
        play_sfx("build")
        play_voice("build")
        self._close_city_menu()

    def _close_city_menu(self):
        self.menu_city = None
        self.mode = "playing"
        self._render_all()

    # -- found city --------------------------------------------------------

    def _found_city(self, r, c):
        """Found a new city at cursor position."""
        if self.tiles[r][c] == T_WATER:
            self.action_log = "Can't build\non water!"
            play_sfx("error")
            self._render_all()
            return
        if (r, c) in self.cities:
            self.action_log = "City here\nalready!"
            play_sfx("error")
            self._render_all()
            return
        if (r, c) in self.armies:
            self.action_log = "Unit in\nthe way!"
            play_sfx("error")
            self._render_all()
            return
        if self.gold < CITY_COST:
            self.action_log = f"Need {CITY_COST}G\nHave {self.gold}G"
            play_sfx("error")
            self._render_all()
            return

        self.gold -= CITY_COST
        self.cities[(r, c)] = {
            "owner": 0,
            "is_capital": False,
            "defense": CITY_BASE_DEF,
            "prod_acc": 0,
            "building": None,
        }
        self.action_log = "City founded!"
        play_sfx("build")
        play_voice("build")
        self._render_all()

    # -- research ----------------------------------------------------------

    def _research(self):
        if self.tech_level >= 3:
            self.action_log = "Tech maxed!"
            play_sfx("error")
            self._render_all()
            return
        cost = TECH_COST[self.tech_level + 1]
        if self.gold < cost:
            self.action_log = f"Need {cost}G"
            play_sfx("error")
            self._render_all()
            return
        self.gold -= cost
        self.tech_level += 1
        self.action_log = f"Tech {self.tech_level}!\n{TECH_NAMES[self.tech_level]}"
        play_sfx("research")

        # Tech 3 bonus: walls for all cities
        if self.tech_level >= 3:
            for city in self.cities.values():
                if city["owner"] == 0:
                    city["defense"] = max(city["defense"], CITY_BASE_DEF + 5)

        self._render_all()

    # -- end turn ----------------------------------------------------------

    def _end_turn(self):
        play_sfx("turn_end")

        # 1. Collect resources
        self.gold += self._total_gold_income()
        self.gold = max(0, self.gold)  # Food deficit can cost gold

        # Check food: if negative food, lose gold instead
        net_food = self._total_food()
        if net_food < 0:
            self.gold += net_food  # negative, so subtracts
            self.gold = max(0, self.gold)

        # 2. Accumulate production for player cities only (AI does it in _ai_turn)
        for (r, c), city in self.cities.items():
            if city["owner"] == 0:
                _, prod, _ = self._city_production(r, c, city)
                city["prod_acc"] += prod

        # 3. Reset movement for player armies
        for army in self.armies.values():
            if army["owner"] == 0:
                army["moved"] = False

        # 4. AI turns
        self._ai_turn()

        # 5. Advance turn
        self.turn += 1
        self.selected_army = None
        self.action_log = f"Turn {self.turn}"

        # Auto-save
        self._save_game()

        self._render_all()
        self._check_victory()
        self._check_defeat()

    # -- AI ----------------------------------------------------------------

    def _ai_turn(self):
        """AI for each opponent — gets production bonus to stay threatening."""
        # AI bonus scales with turn count to keep pressure up
        ai_prod_bonus = 1 + self.turn // 8

        for ai_id in [1, 2, 3]:
            # Check if this AI still has cities
            ai_cities = [(r, c) for (r, c), city in self.cities.items()
                         if city["owner"] == ai_id]
            if not ai_cities:
                continue

            # Accumulate production + build units
            for cr, cc in ai_cities:
                city = self.cities[(cr, cc)]
                _, prod, gold_inc = self._city_production(cr, cc, city)
                city["prod_acc"] += prod + ai_prod_bonus

                # Try to build strongest affordable unit
                build_order = ["knight", "archer", "warrior"]
                for utype in build_order:
                    uinfo = UNIT_TYPES[utype]
                    # AI doesn't need tech, simplified
                    if city["prod_acc"] >= uinfo["cost"]:
                        # Find spawn tile
                        spawn = None
                        dirs = [(0, 1), (1, 0), (0, -1), (-1, 0)]
                        random.shuffle(dirs)
                        for dr, dc in dirs:
                            nr, nc = cr + dr, cc + dc
                            if (0 <= nr < WORLD_H and 0 <= nc < WORLD_W and
                                    self.tiles[nr][nc] != T_WATER and
                                    (nr, nc) not in self.armies and
                                    (nr, nc) not in self.cities):
                                spawn = (nr, nc)
                                break
                        if spawn:
                            city["prod_acc"] -= uinfo["cost"]
                            self.armies[spawn] = {
                                "owner": ai_id,
                                "type": utype,
                                "hp": uinfo["hp"],
                                "max_hp": uinfo["hp"],
                                "atk": uinfo["atk"],
                                "moved": False,
                            }
                        break

            # AI founds new city if enough production and only 1 city
            if len(ai_cities) < 2 and self.turn > 5:
                for cr, cc in ai_cities:
                    city = self.cities[(cr, cc)]
                    if city["prod_acc"] >= 20:
                        # Find empty land tile 2-3 steps away
                        for dist in range(2, 4):
                            for dr in range(-dist, dist + 1):
                                dc = dist - abs(dr)
                                for dcs in ([dc, -dc] if dc != 0 else [0]):
                                    nr, nc = cr + dr, cc + dcs
                                    if (0 <= nr < WORLD_H and 0 <= nc < WORLD_W and
                                            self.tiles[nr][nc] != T_WATER and
                                            (nr, nc) not in self.cities and
                                            (nr, nc) not in self.armies):
                                        city["prod_acc"] -= 20
                                        self.cities[(nr, nc)] = {
                                            "owner": ai_id,
                                            "is_capital": False,
                                            "defense": CITY_BASE_DEF,
                                            "prod_acc": 0,
                                            "building": None,
                                        }
                                        break
                                else:
                                    continue
                                break
                            else:
                                continue
                            break

            # Move AI armies toward nearest enemy
            ai_armies = [(r, c) for (r, c), a in self.armies.items()
                         if a["owner"] == ai_id and not a["moved"]]

            for ar, ac in ai_armies:
                if (ar, ac) not in self.armies:
                    continue
                army = self.armies[(ar, ac)]

                # Find nearest enemy (city or army)
                best_target = None
                best_dist = 999
                for (tr, tc), city in self.cities.items():
                    if city["owner"] != ai_id:
                        d = abs(tr - ar) + abs(tc - ac)
                        if d < best_dist:
                            best_dist = d
                            best_target = (tr, tc)
                for (tr, tc), tarmy in list(self.armies.items()):
                    if tarmy["owner"] != ai_id:
                        d = abs(tr - ar) + abs(tc - ac)
                        if d < best_dist:
                            best_dist = d
                            best_target = (tr, tc)

                if not best_target:
                    continue

                tr, tc = best_target
                # Move toward target (one step)
                best_step = None
                best_step_dist = best_dist

                for dr, dc in [(0, 1), (1, 0), (0, -1), (-1, 0)]:
                    nr, nc = ar + dr, ac + dc
                    if not (0 <= nr < WORLD_H and 0 <= nc < WORLD_W):
                        continue
                    if self.tiles[nr][nc] == T_WATER:
                        continue
                    d = abs(tr - nr) + abs(tc - nc)
                    if d < best_step_dist:
                        # Check if tile is free or has enemy
                        if (nr, nc) in self.armies:
                            target_army = self.armies[(nr, nc)]
                            if target_army["owner"] == ai_id:
                                continue  # Own unit blocking
                            # Attack enemy
                            best_step = (nr, nc)
                            best_step_dist = d
                        elif (nr, nc) in self.cities:
                            target_city = self.cities[(nr, nc)]
                            if target_city["owner"] == ai_id:
                                continue  # Own city
                            best_step = (nr, nc)
                            best_step_dist = d
                        else:
                            best_step = (nr, nc)
                            best_step_dist = d

                if best_step:
                    nr, nc = best_step
                    if (nr, nc) in self.armies and self.armies[(nr, nc)]["owner"] != ai_id:
                        # AI combat
                        defender = self.armies[(nr, nc)]
                        defender["hp"] -= army["atk"]
                        army["hp"] -= defender["atk"]

                        if defender["hp"] <= 0:
                            del self.armies[(nr, nc)]
                            if army["hp"] > 0:
                                del self.armies[(ar, ac)]
                                army["moved"] = True
                                self.armies[(nr, nc)] = army
                            else:
                                del self.armies[(ar, ac)]
                        elif army["hp"] <= 0:
                            del self.armies[(ar, ac)]
                        else:
                            army["moved"] = True

                    elif (nr, nc) in self.cities and self.cities[(nr, nc)]["owner"] != ai_id:
                        # AI attacks city
                        city = self.cities[(nr, nc)]
                        city["defense"] -= army["atk"]
                        army["hp"] -= 2
                        if city["defense"] <= 0:
                            city["defense"] = CITY_BASE_DEF
                            city["owner"] = ai_id
                            city["prod_acc"] = 0
                            del self.armies[(ar, ac)]
                            if army["hp"] > 0:
                                army["moved"] = True
                                self.armies[(nr, nc)] = army
                        else:
                            if army["hp"] <= 0:
                                del self.armies[(ar, ac)]
                            else:
                                army["moved"] = True
                    else:
                        # Just move
                        del self.armies[(ar, ac)]
                        army["moved"] = True
                        self.armies[(nr, nc)] = army

            # Reset AI army movement for next turn
            for army in self.armies.values():
                if army["owner"] == ai_id:
                    army["moved"] = False

    # -- victory / defeat --------------------------------------------------

    def _check_victory(self):
        """Check if player captured all 3 AI capitals."""
        ai_capitals = [city for city in self.cities.values()
                       if city["is_capital"] and city["owner"] != 0]
        if len(ai_capitals) == 0:
            self._victory()

    def _check_defeat(self):
        """Check if player lost all cities and armies."""
        player_cities = [c for c in self.cities.values() if c["owner"] == 0]
        player_armies = [a for a in self.armies.values() if a["owner"] == 0]
        if not player_cities and not player_armies:
            self._defeat()

    def _victory(self):
        self.running = False
        self.mode = "victory"
        self._stop_blink()
        self._delete_save()

        best = scores.load_best("empire", 0)
        if best == 0 or self.turn < best:
            scores.save_best("empire", self.turn)
            best = self.turn

        play_sfx("win")
        play_voice("win")

        self.set_key(1, _render_title("VICTORY", ""))
        self.set_key(2, _render_title("TURNS", str(self.turn)))
        if self.turn <= best:
            self.set_key(3, _render_title("NEW", "BEST!"))
        else:
            self.set_key(3, _render_title("BEST", str(best)))
        for k in range(4, 8):
            self.set_key(k, _render_hud_empty())

        for k in range(8, 32):
            self.set_key(k, self._img_fog)

        # Flash victory colors
        def _flash():
            colors = ["#3b82f6", "#fbbf24", "#22c55e"]
            for i in range(6):
                c = colors[i % len(colors)]
                img = Image.new("RGB", SIZE, c)
                d = ImageDraw.Draw(img)
                d.text((48, 48), "W", font=_font(36), fill="white", anchor="mm")
                for k in range(8, 32):
                    self.set_key(k, img)
                time.sleep(0.4)
            # Show restart
            for k in range(8, 32):
                self.set_key(k, self._img_fog)
            self.set_key(20, _render_btn("PLAY", "AGAIN"))

        t = threading.Thread(target=_flash, daemon=True)
        t.start()

    def _defeat(self):
        self.running = False
        self.mode = "defeat"
        self._stop_blink()
        self._delete_save()

        play_sfx("defeat")
        play_voice("defeat")

        self.set_key(1, _render_title("DEFEAT", ""))
        self.set_key(2, _render_hud_turn(self.turn))
        best = scores.load_best("empire", 0)
        if best > 0:
            self.set_key(3, _render_title("BEST", f"{best} turns"))
        for k in range(4, 8):
            self.set_key(k, _render_hud_empty())
        for k in range(8, 32):
            self.set_key(k, self._img_fog)
        self.set_key(20, _render_btn("PLAY", "AGAIN"))

    # -- key handler -------------------------------------------------------

    def on_key(self, _deck, key, pressed):
        if not pressed:
            return
        with self.lock:
            if self.mode == "idle":
                self._on_idle(key)
            elif self.mode == "playing":
                self._on_playing(key)
            elif self.mode == "city_menu":
                self._on_city_menu(key)
            elif self.mode in ("victory", "defeat"):
                self._on_endgame(key)

    def _on_idle(self, key):
        has_save = os.path.exists(SAVE_FILE)
        if has_save:
            if key == 12:
                self._continue_game()
            elif key == 20:
                self._start_new()
        else:
            if key == 20:
                self._start_new()

    def _on_endgame(self, key):
        if key == 20:
            self._start_new()

    def _on_playing(self, key):
        # HUD keys
        if key == 4:
            # Research
            self._research()
            return
        if key == 7:
            # End turn
            self._end_turn()
            return

        # Game grid keys
        if key < ROW_OFFSET * COLS or key >= (ROW_OFFSET + ROWS) * COLS:
            return

        sr, sc = pos_to_rc(key)
        if sr < 0 or sr >= VIEW_ROWS or sc < 0 or sc >= VIEW_COLS:
            return

        wr, wc = self._screen_to_world(sr, sc)
        if not (0 <= wr < WORLD_H and 0 <= wc < WORLD_W):
            return

        # If we have a selected army, try to move it
        if self.selected_army:
            sar, sac = self.selected_army
            if (wr, wc) == (sar, sac):
                # Deselect
                self.selected_army = None
                self.action_log = "Deselected"
                play_sfx("select")
                self._render_all()
                return
            # Try to move the army
            self._move_army(sar, sac, wr, wc)
            return

        # No army selected -- check what's at the tile
        # Move cursor to this tile first
        old_r, old_c = self.cursor_r, self.cursor_c
        self.cursor_r, self.cursor_c = wr, wc

        visible = self._visibility_set()
        if (wr, wc) not in visible and (wr, wc) not in self.explored:
            # Can't interact with unseen tiles, just move cursor
            self._move_cursor(wr, wc)
            return

        # Player army -> select it
        if (wr, wc) in self.armies and self.armies[(wr, wc)]["owner"] == 0:
            self._select_army(wr, wc)
            return

        # Player city -> open menu
        if (wr, wc) in self.cities and self.cities[(wr, wc)]["owner"] == 0:
            self._open_city_menu(wr, wc)
            return

        # Enemy army -> show info
        if (wr, wc) in self.armies and (wr, wc) in visible:
            army = self.armies[(wr, wc)]
            uinfo = UNIT_TYPES[army["type"]]
            owner = PLAYER_NAMES[army["owner"]]
            self.action_log = f"{owner}\n{uinfo['name']}\nHP:{army['hp']}"
            play_sfx("select")
            self._render_all()
            return

        # Enemy city -> show info
        if (wr, wc) in self.cities and (wr, wc) in visible:
            city = self.cities[(wr, wc)]
            owner = PLAYER_NAMES[city["owner"]]
            cap = " *CAP*" if city["is_capital"] else ""
            self.action_log = f"{owner} city{cap}\nDef:{city['defense']}"
            play_sfx("select")
            self._render_all()
            return

        # Empty explored land -> found city
        if (wr, wc) in visible and self.tiles[wr][wc] != T_WATER:
            if (wr, wc) not in self.cities and (wr, wc) not in self.armies:
                self._found_city(wr, wc)
                return

        # Just move cursor
        self._move_cursor(wr, wc)

    def _on_city_menu(self, key):
        if key == 7:
            # Back
            self._close_city_menu()
            return

        # Build unit buttons: keys 2,3,4 -> warrior, archer, knight
        unit_map = {2: "warrior", 3: "archer", 4: "knight"}
        if key in unit_map:
            self._build_unit(unit_map[key])
            return

        # Clicking game area closes menu
        if key >= ROW_OFFSET * COLS:
            self._close_city_menu()


# -- main ------------------------------------------------------------------

def main():
    from StreamDeck.DeviceManager import DeviceManager

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
    print("MINI EMPIRE -- conquer the world!")

    game = EmpireGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nSaving...")
        if game.running:
            game._save_game()
    finally:
        game._stop_blink()
        deck.reset()
        deck.close()
        cleanup_sfx()

if __name__ == "__main__":
    main()
