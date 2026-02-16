"""Dungeon Crawler — Stream Deck roguelike mini-game.

Explore a fog-of-war dungeon on a scrolling 20x20 grid viewed through
a 3x8 Stream Deck window. Fight monsters, collect loot, descend stairs.

Voice pack: SC Kerrigan

Usage:
    uv run python scripts/dungeon_game.py
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

DUNGEON_W = 20
DUNGEON_H = 20

# Player screen center
VIEW_ROWS = 3
VIEW_COLS = 8
PLAYER_SCREEN_ROW = 1  # middle row of the 3-row view
PLAYER_SCREEN_COL = 3  # slightly left of center

# Tile types
T_WALL = 0
T_FLOOR = 1
T_STAIRS = 2

# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos):
    return pos // COLS - ROW_OFFSET, pos % COLS

def rc_to_pos(row, col):
    return (row + ROW_OFFSET) * COLS + col

# -- monster definitions ---------------------------------------------------

MONSTERS = {
    "R": {"name": "Rat",      "hp": 3,  "atk": 1, "xp": 5,  "letter": "R"},
    "B": {"name": "Bat",      "hp": 4,  "atk": 2, "xp": 8,  "letter": "B"},
    "S": {"name": "Skeleton", "hp": 7,  "atk": 3, "xp": 15, "letter": "S"},
    "O": {"name": "Orc",      "hp": 10, "atk": 4, "xp": 25, "letter": "O"},
    "D": {"name": "Dragon",   "hp": 20, "atk": 6, "xp": 50, "letter": "D"},
}

# What spawns per floor range
def _floor_monsters(floor):
    if floor <= 3:
        return ["R", "B"]
    elif floor <= 6:
        return ["B", "S"]
    elif floor <= 9:
        return ["S", "O"]
    else:
        return ["O", "D"]

# -- item definitions ------------------------------------------------------

ITEMS = {
    "potion":  {"name": "Potion",  "color": "#22c55e", "symbol": "+"},
    "sword":   {"name": "Sword",   "color": "#ef4444", "symbol": "/"},
    "shield":  {"name": "Shield",  "color": "#3b82f6", "symbol": "O"},
    "gold":    {"name": "Gold",    "color": "#eab308", "symbol": "$"},
}

# -- voice pack (SC Kerrigan) ---------------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "sc_kerrigan/sounds/ImReady.mp3",
        "sc_kerrigan/sounds/IReadYou.mp3",
        "sc_kerrigan/sounds/KerriganReporting.mp3",
    ],
    "kill": [
        "sc_kerrigan/sounds/IGotcha.mp3",
        "sc_kerrigan/sounds/EasilyAmused.mp3",
        "sc_kerrigan/sounds/BeAPleasure.mp3",
    ],
    "gameover": [
        "sc_kerrigan/sounds/Death1.mp3",
        "sc_kerrigan/sounds/Death2.mp3",
    ],
    "levelup": [
        "sc_kerrigan/sounds/Telepath.mp3",
        "sc_kerrigan/sounds/ThinkingSameThing.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="dungeon-sfx-")
    v = SFX_VOLUME

    # attack: quick sword slash (rising noise burst)
    s = _noise(0.04, v * 0.3) + _square(800, 0.03, v * 0.4, 0.3) + _noise(0.03, v * 0.2)
    _write_wav(os.path.join(_sfx_dir, "attack.wav"), s)
    _sfx_cache["attack"] = os.path.join(_sfx_dir, "attack.wav")

    # hit: damage taken (low thud)
    s = _square(120, 0.08, v * 0.4, 0.3) + _square(80, 0.06, v * 0.3, 0.4)
    _write_wav(os.path.join(_sfx_dir, "hit.wav"), s)
    _sfx_cache["hit"] = os.path.join(_sfx_dir, "hit.wav")

    # pickup: item collected (bright ascending)
    s = (_triangle(440, 0.04, v * 0.4) + _triangle(660, 0.04, v * 0.45) +
         _triangle(880, 0.06, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "pickup.wav"), s)
    _sfx_cache["pickup"] = os.path.join(_sfx_dir, "pickup.wav")

    # stairs: descend (deep sweep down)
    s = (_triangle(600, 0.06, v * 0.4) + _triangle(400, 0.08, v * 0.35) +
         _triangle(250, 0.1, v * 0.3) + _triangle(150, 0.12, v * 0.25))
    _write_wav(os.path.join(_sfx_dir, "stairs.wav"), s)
    _sfx_cache["stairs"] = os.path.join(_sfx_dir, "stairs.wav")

    # death: game over (sad descending)
    s = (_square(400, 0.12, v * 0.4, 0.4) + _square(300, 0.12, v * 0.35, 0.4) +
         _square(200, 0.15, v * 0.3, 0.4) + _square(100, 0.25, v * 0.25, 0.4))
    _write_wav(os.path.join(_sfx_dir, "death.wav"), s)
    _sfx_cache["death"] = os.path.join(_sfx_dir, "death.wav")

    # levelup: fanfare (bright ascending chords)
    s = (_triangle(523, 0.08, v * 0.4) + _triangle(659, 0.08, v * 0.45) +
         _triangle(784, 0.08, v * 0.5) + _triangle(1047, 0.2, v * 0.55))
    _write_wav(os.path.join(_sfx_dir, "levelup.wav"), s)
    _sfx_cache["levelup"] = os.path.join(_sfx_dir, "levelup.wav")

    # step: quiet footstep
    s = _noise(0.02, v * 0.15) + _square(200, 0.02, v * 0.1, 0.3)
    _write_wav(os.path.join(_sfx_dir, "step.wav"), s)
    _sfx_cache["step"] = os.path.join(_sfx_dir, "step.wav")

def play_sfx(name):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)

def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)

# -- tile colors -----------------------------------------------------------

CLR_WALL      = "#2d2d3a"
CLR_FLOOR     = "#3b2f1e"
CLR_FLOOR_DIM = "#1e180e"
CLR_WALL_DIM  = "#1a1a22"
CLR_PLAYER    = "#06b6d4"
CLR_MONSTER   = "#dc2626"
CLR_ITEM      = "#eab308"
CLR_STAIRS    = "#e2e8f0"
CLR_FOG       = "#000000"
CLR_HUD_BG    = "#111827"

# -- tile renderers --------------------------------------------------------

def _render_wall(dim=False):
    bg = CLR_WALL_DIM if dim else CLR_WALL
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    brick = "#3d3d4f" if not dim else "#222230"
    mortar = "#222230" if not dim else "#151520"
    # Simple brick pattern
    for row_i in range(4):
        y = row_i * 24
        d.rectangle([0, y, 95, y + 1], fill=mortar)
        offset = 0 if row_i % 2 == 0 else 24
        for x in range(offset, 96, 48):
            d.rectangle([x, y + 2, x + 44, y + 22], fill=brick)
            d.rectangle([x, y, x + 1, y + 24], fill=mortar)
    return img

def _render_floor(dim=False):
    bg = CLR_FLOOR_DIM if dim else CLR_FLOOR
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    # Subtle floor texture: scattered dots
    dot_color = "#4a3d2a" if not dim else "#2a2418"
    for _ in range(6):
        x, y = random.randint(10, 85), random.randint(10, 85)
        d.ellipse([x - 1, y - 1, x + 1, y + 1], fill=dot_color)
    return img

def _render_player():
    img = Image.new("RGB", SIZE, CLR_FLOOR)
    d = ImageDraw.Draw(img)
    # Draw player character
    d.rectangle([2, 2, 93, 93], outline=CLR_PLAYER, width=2)
    d.text((48, 48), "@", font=_font(40), fill=CLR_PLAYER, anchor="mm")
    return img

def _render_monster(letter, hp_frac=1.0):
    img = Image.new("RGB", SIZE, CLR_FLOOR)
    d = ImageDraw.Draw(img)
    # Monster body
    color = CLR_MONSTER
    d.text((48, 44), letter, font=_font(36), fill=color, anchor="mm")
    # HP bar at bottom
    bar_w = 60
    bar_x = 18
    bar_y = 78
    d.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 8], outline="#4b5563")
    fill_w = max(1, int(bar_w * hp_frac))
    bar_color = "#22c55e" if hp_frac > 0.5 else "#eab308" if hp_frac > 0.25 else "#ef4444"
    d.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + 8], fill=bar_color)
    return img

def _render_item(item_type):
    info = ITEMS.get(item_type)
    if not info:
        return _render_floor()
    img = Image.new("RGB", SIZE, CLR_FLOOR)
    d = ImageDraw.Draw(img)
    # Item glow circle
    d.ellipse([20, 20, 76, 76], fill="#2a2418", outline=info["color"], width=2)
    d.text((48, 48), info["symbol"], font=_font(28), fill=info["color"], anchor="mm")
    return img

def _render_stairs():
    img = Image.new("RGB", SIZE, CLR_FLOOR)
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 93, 93], outline=CLR_STAIRS, width=1)
    # Down arrow
    d.text((48, 38), "v", font=_font(32), fill=CLR_STAIRS, anchor="mm")
    d.text((48, 72), "DOWN", font=_font(12), fill="#9ca3af", anchor="mm")
    return img

def _render_fog():
    return Image.new("RGB", SIZE, CLR_FOG)

# -- HUD renderers --------------------------------------------------------

def _render_hud_hp(hp, max_hp):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 10), "HP", font=_font(10), fill="#9ca3af", anchor="mt")
    # HP bar
    bar_w = 70
    bar_x = 13
    bar_y = 30
    bar_h = 16
    d.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h], outline="#4b5563")
    frac = max(0, hp / max_hp) if max_hp > 0 else 0
    fill_w = max(0, int(bar_w * frac))
    bar_color = "#22c55e" if frac > 0.5 else "#eab308" if frac > 0.25 else "#ef4444"
    if fill_w > 0:
        d.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + bar_h], fill=bar_color)
    d.text((48, 60), f"{hp}/{max_hp}", font=_font(16), fill="white", anchor="mm")
    return img

def _render_hud_atk_def(atk, defense):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "ATK", font=_font(10), fill="#f87171", anchor="mt")
    d.text((48, 34), str(atk), font=_font(20), fill="#ef4444", anchor="mm")
    d.text((48, 54), "DEF", font=_font(10), fill="#60a5fa", anchor="mt")
    d.text((48, 74), str(defense), font=_font(20), fill="#3b82f6", anchor="mm")
    return img

def _render_hud_level(level, xp, xp_next):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 10), f"LV {level}", font=_font(14), fill="#a78bfa", anchor="mt")
    # XP bar
    bar_w = 60
    bar_x = 18
    bar_y = 38
    d.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + 10], outline="#4b5563")
    xp_in_level = xp % 20
    frac = xp_in_level / 20.0
    fill_w = max(0, int(bar_w * frac))
    if fill_w > 0:
        d.rectangle([bar_x, bar_y, bar_x + fill_w, bar_y + 10], fill="#a78bfa")
    d.text((48, 68), f"XP {xp_in_level}/20", font=_font(11), fill="#6b7280", anchor="mm")
    return img

def _render_hud_floor(floor):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "FLOOR", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 50), str(floor), font=_font(28), fill="#fbbf24", anchor="mm")
    return img

def _render_hud_gold(gold):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "GOLD", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 50), str(gold), font=_font(22), fill="#eab308", anchor="mm")
    return img

def _render_hud_log(text):
    img = Image.new("RGB", SIZE, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    # Split text into lines that fit
    lines = []
    words = text.split()
    line = ""
    for w in words:
        test = (line + " " + w).strip()
        if len(test) <= 10:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    y = 48 - len(lines) * 10
    for l in lines:
        d.text((48, y), l, font=_font(12), fill="#d1d5db", anchor="mm")
        y += 20
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

# -- dungeon generation ----------------------------------------------------

def _generate_dungeon(floor):
    """Generate a 20x20 dungeon with rooms and corridors.

    Returns (tiles, monsters, items, stairs_pos, player_pos).
    tiles is a 2D list [row][col] of T_WALL / T_FLOOR / T_STAIRS.
    monsters is a dict {(r,c): {"type": letter, "hp": int, "max_hp": int, "atk": int, "xp": int}}.
    items is a dict {(r,c): item_type_string}.
    """
    tiles = [[T_WALL] * DUNGEON_W for _ in range(DUNGEON_H)]
    rooms = []

    # Place 4-6 random rooms
    num_rooms = random.randint(4, 6)
    attempts = 0
    while len(rooms) < num_rooms and attempts < 200:
        attempts += 1
        rw = random.randint(3, 6)
        rh = random.randint(3, 6)
        rx = random.randint(1, DUNGEON_W - rw - 1)
        ry = random.randint(1, DUNGEON_H - rh - 1)

        # Check overlap
        overlap = False
        for (ex, ey, ew, eh) in rooms:
            if (rx - 1 < ex + ew and rx + rw + 1 > ex and
                ry - 1 < ey + eh and ry + rh + 1 > ey):
                overlap = True
                break
        if overlap:
            continue

        rooms.append((rx, ry, rw, rh))
        for row in range(ry, ry + rh):
            for col in range(rx, rx + rw):
                tiles[row][col] = T_FLOOR

    # Connect rooms with L-shaped corridors
    for i in range(len(rooms) - 1):
        x1 = rooms[i][0] + rooms[i][2] // 2
        y1 = rooms[i][1] + rooms[i][3] // 2
        x2 = rooms[i + 1][0] + rooms[i + 1][2] // 2
        y2 = rooms[i + 1][1] + rooms[i + 1][3] // 2

        # Horizontal then vertical
        cx = x1
        while cx != x2:
            tiles[y1][cx] = T_FLOOR
            cx += 1 if x2 > x1 else -1
        tiles[y1][cx] = T_FLOOR
        cy = y1
        while cy != y2:
            tiles[cy][x2] = T_FLOOR
            cy += 1 if y2 > y1 else -1
        tiles[cy][x2] = T_FLOOR

    # Collect all floor tiles
    floor_tiles = []
    for r in range(DUNGEON_H):
        for c in range(DUNGEON_W):
            if tiles[r][c] == T_FLOOR:
                floor_tiles.append((r, c))

    if len(floor_tiles) < 15:
        # Fallback: carve a big room
        for r in range(3, 17):
            for c in range(3, 17):
                tiles[r][c] = T_FLOOR
        floor_tiles = [(r, c) for r in range(3, 17) for c in range(3, 17)]

    random.shuffle(floor_tiles)
    used = set()

    # Player start (in first room if available)
    if rooms:
        pr = rooms[0][1] + rooms[0][3] // 2
        pc = rooms[0][0] + rooms[0][2] // 2
    else:
        pr, pc = floor_tiles[0]
    player_pos = (pr, pc)
    used.add(player_pos)

    # Stairs (in last room, far from player)
    if rooms:
        sr = rooms[-1][1] + rooms[-1][3] // 2
        sc = rooms[-1][0] + rooms[-1][2] // 2
    else:
        # Find farthest floor tile from player
        best = floor_tiles[-1]
        best_dist = 0
        for ft in floor_tiles:
            d = abs(ft[0] - pr) + abs(ft[1] - pc)
            if d > best_dist:
                best_dist = d
                best = ft
        sr, sc = best
    stairs_pos = (sr, sc)
    tiles[sr][sc] = T_STAIRS
    used.add(stairs_pos)

    # Available tiles (not player, not stairs)
    avail = [t for t in floor_tiles if t not in used]
    random.shuffle(avail)

    # Place monsters
    monster_types = _floor_monsters(floor)
    num_monsters = random.randint(3, 6)
    monsters = {}
    for i in range(min(num_monsters, len(avail))):
        pos = avail.pop()
        mt = random.choice(monster_types)
        mi = MONSTERS[mt]
        monsters[pos] = {
            "type": mt, "hp": mi["hp"], "max_hp": mi["hp"],
            "atk": mi["atk"], "xp": mi["xp"], "name": mi["name"],
        }

    # Place items
    num_items = random.randint(1, 3)
    items = {}
    item_types = ["potion", "sword", "shield", "gold"]
    for i in range(min(num_items, len(avail))):
        pos = avail.pop()
        items[pos] = random.choice(item_types)

    return tiles, monsters, items, stairs_pos, player_pos


# -- game ------------------------------------------------------------------

class DungeonGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.mode = "idle"  # idle | playing | gameover

        # Player stats
        self.hp = 20
        self.max_hp = 20
        self.atk = 3
        self.defense = 1
        self.level = 1
        self.xp = 0
        self.gold = 0
        self.floor = 1

        # Dungeon state
        self.tiles = []
        self.monsters = {}
        self.items = {}
        self.stairs_pos = (0, 0)
        self.player_pos = (0, 0)
        self.revealed = set()  # tiles the player has seen
        self.action_log = ""

        # Pre-rendered static tiles (generated once, reused)
        self._img_fog = _render_fog()
        self._img_stairs = _render_stairs()

    def set_key(self, pos, img):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # -- fog of war --------------------------------------------------------

    def _is_visible(self, r, c):
        """Check if tile is within Manhattan distance 3 of player."""
        pr, pc = self.player_pos
        return abs(r - pr) + abs(c - pc) <= 3

    def _reveal_around_player(self):
        """Mark tiles near player as revealed."""
        pr, pc = self.player_pos
        for dr in range(-3, 4):
            for dc in range(-3, 4):
                if abs(dr) + abs(dc) <= 3:
                    nr, nc = pr + dr, pc + dc
                    if 0 <= nr < DUNGEON_H and 0 <= nc < DUNGEON_W:
                        self.revealed.add((nr, nc))

    # -- view window -------------------------------------------------------

    def _view_origin(self):
        """Calculate the top-left corner of the view window."""
        pr, pc = self.player_pos
        # Player should appear at PLAYER_SCREEN_ROW, PLAYER_SCREEN_COL
        vr = pr - PLAYER_SCREEN_ROW
        vc = pc - PLAYER_SCREEN_COL
        # Clamp to dungeon bounds
        vr = max(0, min(vr, DUNGEON_H - VIEW_ROWS))
        vc = max(0, min(vc, DUNGEON_W - VIEW_COLS))
        return vr, vc

    def _world_to_screen(self, wr, wc):
        """Convert world coords to screen coords (or None if off-screen)."""
        vr, vc = self._view_origin()
        sr = wr - vr
        sc = wc - vc
        if 0 <= sr < VIEW_ROWS and 0 <= sc < VIEW_COLS:
            return sr, sc
        return None

    def _screen_to_world(self, sr, sc):
        """Convert screen coords to world coords."""
        vr, vc = self._view_origin()
        return vr + sr, vc + sc

    # -- rendering ---------------------------------------------------------

    def _render_tile_at(self, wr, wc):
        """Render a single world tile as a PIL image."""
        visible = self._is_visible(wr, wc)
        seen = (wr, wc) in self.revealed

        if not visible and not seen:
            return self._img_fog

        dim = not visible  # seen but not currently visible

        # Player
        if (wr, wc) == self.player_pos:
            return _render_player()

        # Monster
        if (wr, wc) in self.monsters:
            if visible:
                m = self.monsters[(wr, wc)]
                frac = m["hp"] / m["max_hp"] if m["max_hp"] > 0 else 0
                return _render_monster(m["type"], frac)
            else:
                # Don't show monsters in fog (only seen tiles)
                tile_type = self.tiles[wr][wc]
                if tile_type == T_WALL:
                    return _render_wall(dim=True)
                return _render_floor(dim=True)

        # Item
        if (wr, wc) in self.items:
            if visible:
                return _render_item(self.items[(wr, wc)])
            else:
                tile_type = self.tiles[wr][wc]
                if tile_type == T_WALL:
                    return _render_wall(dim=True)
                return _render_floor(dim=True)

        # Stairs
        if self.tiles[wr][wc] == T_STAIRS:
            if visible or seen:
                return self._img_stairs
            return self._img_fog

        # Wall
        if self.tiles[wr][wc] == T_WALL:
            return _render_wall(dim=dim)

        # Floor
        return _render_floor(dim=dim)

    def _render_game_grid(self):
        """Render the entire visible portion of the dungeon."""
        for sr in range(VIEW_ROWS):
            for sc in range(VIEW_COLS):
                wr, wc = self._screen_to_world(sr, sc)
                if 0 <= wr < DUNGEON_H and 0 <= wc < DUNGEON_W:
                    img = self._render_tile_at(wr, wc)
                else:
                    img = self._img_fog
                pos = rc_to_pos(sr, sc)
                self.set_key(pos, img)

    def _render_hud(self):
        """Render HUD on keys 1-7 (key 0 is arcade BACK)."""
        self.set_key(1, _render_hud_hp(self.hp, self.max_hp))
        self.set_key(2, _render_hud_atk_def(self.atk, self.defense))
        self.set_key(3, _render_hud_level(self.level, self.xp, 20))
        self.set_key(4, _render_hud_floor(self.floor))
        self.set_key(5, _render_hud_gold(self.gold))
        self.set_key(6, _render_hud_log(self.action_log))
        self.set_key(7, _render_hud_empty())

    def _render_all(self):
        self._reveal_around_player()
        self._render_hud()
        self._render_game_grid()

    # -- idle screen -------------------------------------------------------

    def show_idle(self):
        self.running = False
        self.mode = "idle"

        best = scores.load_best("dungeon", 0)

        # HUD row
        self.set_key(1, _render_title("DUNGEON", "CRAWL"))
        for k in range(2, 8):
            if k == 2 and best > 0:
                self.set_key(k, _render_title(f"BEST", f"Floor {best}"))
            else:
                self.set_key(k, _render_hud_empty())

        # Game area — dark
        for k in range(8, 32):
            self.set_key(k, self._img_fog)

        # Start button at key 20 (row 1, col 4 in game = deck row 2, col 4)
        self.set_key(20, _render_btn("START", "GAME"))

    # -- start game --------------------------------------------------------

    def _start_game(self):
        self.hp = 20
        self.max_hp = 20
        self.atk = 3
        self.defense = 1
        self.level = 1
        self.xp = 0
        self.gold = 0
        self.floor = 1
        self.action_log = "Entered!"
        self.revealed = set()

        self._generate_floor()
        self.running = True
        self.mode = "playing"

        play_sfx("stairs")
        play_voice("start")
        self._render_all()

    def _generate_floor(self):
        """Generate dungeon for current floor."""
        self.tiles, self.monsters, self.items, self.stairs_pos, self.player_pos = \
            _generate_dungeon(self.floor)
        self.revealed = set()
        self._reveal_around_player()

    # -- movement & interaction --------------------------------------------

    def _try_move(self, target_r, target_c):
        """Attempt to move player to target world position."""
        # Bounds check
        if not (0 <= target_r < DUNGEON_H and 0 <= target_c < DUNGEON_W):
            return

        # Wall check
        if self.tiles[target_r][target_c] == T_WALL:
            return

        # Monster check
        if (target_r, target_c) in self.monsters:
            self._combat(target_r, target_c)
            return

        # Item check
        if (target_r, target_c) in self.items:
            self._pickup_item(target_r, target_c)
            return

        # Stairs check
        if self.tiles[target_r][target_c] == T_STAIRS:
            self._descend()
            return

        # Move to floor tile
        self.player_pos = (target_r, target_c)
        play_sfx("step")
        self._render_all()

    def _combat(self, mr, mc):
        """Resolve combat with monster at (mr, mc)."""
        monster = self.monsters[(mr, mc)]

        # Player attacks monster
        damage_to_monster = self.atk
        monster["hp"] -= damage_to_monster
        play_sfx("attack")

        if monster["hp"] <= 0:
            # Monster killed
            name = monster["name"]
            xp_gain = monster["xp"]
            del self.monsters[(mr, mc)]
            self.xp += xp_gain
            self.gold += random.randint(1, 5)
            self.action_log = f"Slew {name} +{xp_gain}XP"
            self.player_pos = (mr, mc)
            play_voice("kill")

            # Check level up
            self._check_levelup()
        else:
            # Monster survives, hits back
            damage_to_player = max(0, monster["atk"] - self.defense)
            self.hp -= damage_to_player
            name = monster["name"]
            self.action_log = f"Hit {name} -{damage_to_player}HP"
            play_sfx("hit")

            if self.hp <= 0:
                self._game_over()
                return

        self._render_all()

    def _pickup_item(self, ir, ic):
        """Pick up item at (ir, ic)."""
        item_type = self.items[(ir, ic)]
        del self.items[(ir, ic)]

        if item_type == "potion":
            heal = min(8, self.max_hp - self.hp)
            self.hp = min(self.max_hp, self.hp + 8)
            self.action_log = f"Potion +{heal}HP"
        elif item_type == "sword":
            self.atk += 1
            self.action_log = f"Sword! ATK+1"
        elif item_type == "shield":
            self.defense += 1
            self.action_log = f"Shield! DEF+1"
        elif item_type == "gold":
            amount = random.randint(10, 30)
            self.gold += amount
            self.action_log = f"Gold +{amount}!"

        self.player_pos = (ir, ic)
        play_sfx("pickup")
        self._render_all()

    def _descend(self):
        """Go down stairs to next floor."""
        self.floor += 1
        self.action_log = f"Floor {self.floor}!"
        self.revealed = set()
        self._generate_floor()

        play_sfx("stairs")
        self._render_all()

    def _check_levelup(self):
        """Check and process level-up."""
        new_level = 1 + self.xp // 20
        if new_level > self.level:
            levels_gained = new_level - self.level
            self.level = new_level
            self.max_hp += 5 * levels_gained
            self.atk += 1 * levels_gained
            self.hp = self.max_hp  # Full heal on level up
            self.action_log = f"LEVEL {self.level}!"
            play_sfx("levelup")
            play_voice("levelup")

    # -- game over ---------------------------------------------------------

    def _game_over(self):
        self.running = False
        self.mode = "gameover"

        # Save best floor
        best = scores.load_best("dungeon", 0)
        if self.floor > best:
            scores.save_best("dungeon", self.floor)

        play_sfx("death")
        play_voice("gameover")

        # Render game over screen
        self.set_key(1, _render_title("GAME", "OVER"))
        self.set_key(2, _render_hud_hp(0, self.max_hp))
        self.set_key(3, _render_title(f"LV{self.level}", f"{self.xp}XP"))
        self.set_key(4, _render_hud_floor(self.floor))
        self.set_key(5, _render_hud_gold(self.gold))

        new_best = self.floor > best
        if new_best:
            self.set_key(6, _render_title("NEW", "BEST!"))
        else:
            self.set_key(6, _render_title("BEST", f"Floor {max(best, self.floor)}"))
        self.set_key(7, _render_hud_empty())

        # Clear game area
        for k in range(8, 32):
            self.set_key(k, self._img_fog)

        # Restart button
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
            elif self.mode == "gameover":
                self._on_gameover(key)

    def _on_idle(self, key):
        if key == 20:
            self._start_game()

    def _on_gameover(self, key):
        if key == 20:
            self._start_game()

    def _on_playing(self, key):
        # Only game grid keys (rows 1-3 on deck = game rows 0-2)
        if key < ROW_OFFSET * COLS or key >= (ROW_OFFSET + ROWS) * COLS:
            return

        sr, sc = pos_to_rc(key)
        if sr < 0 or sr >= VIEW_ROWS or sc < 0 or sc >= VIEW_COLS:
            return

        # Find where the player is on screen
        player_screen = self._world_to_screen(*self.player_pos)
        if player_screen is None:
            return

        psr, psc = player_screen

        # Check if the pressed tile is adjacent to player (4-directional)
        dr = sr - psr
        dc = sc - psc
        if abs(dr) + abs(dc) != 1:
            return  # Not adjacent

        # Convert screen tap to world coords
        wr, wc = self._screen_to_world(sr, sc)
        self._try_move(wr, wc)


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
    print("DUNGEON CRAWLER -- explore the depths!")

    game = DungeonGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nExiting...")
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()

if __name__ == "__main__":
    main()
