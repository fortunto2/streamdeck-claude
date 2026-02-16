"""Factory Conveyor Puzzle — Stream Deck mini-Factorio.

Route resources from SOURCE tiles to GOAL tiles using conveyor belts
and furnaces. Resources flow along conveyors each tick. Plan your
layout, then press PLAY to watch it run.

Voice pack: GLaDOS

Usage:
    uv run python scripts/factory_game.py
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
TICK_INTERVAL = 2.0

# Directions: (dr, dc) indexed by direction id
DIR_RIGHT = 0
DIR_DOWN = 1
DIR_LEFT = 2
DIR_UP = 3
DIR_DELTAS = [(0, 1), (1, 0), (0, -1), (-1, 0)]
DIR_ARROWS = ["\u2192", "\u2193", "\u2190", "\u2191"]  # right down left up
DIR_NAMES = ["RIGHT", "DOWN", "LEFT", "UP"]

# Tile type constants
TILE_EMPTY = "empty"
TILE_SOURCE = "source"
TILE_GOAL = "goal"
TILE_BELT = "belt"
TILE_FURNACE = "furnace"
TILE_WALL = "wall"

# Resource types
RES_ORE = "ore"
RES_INGOT = "ingot"

# Resource colors
RES_COLORS = {
    RES_ORE: "#8B6914",
    RES_INGOT: "#C0C0C0",
}

# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos):
    return pos // COLS - ROW_OFFSET, pos % COLS


def rc_to_pos(row, col):
    return (row + ROW_OFFSET) * COLS + col


# -- voice pack (GLaDOS) --------------------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "glados/sounds/Hello.mp3",
        "glados/sounds/GoodNews.mp3",
    ],
    "level_complete": [
        "glados/sounds/Congratulations.mp3",
        "glados/sounds/Excellent.mp3",
        "glados/sounds/Fantastic.mp3",
    ],
    "fail": [
        "glados/sounds/WompWomp.mp3",
        "glados/sounds/WhereDidYourLifeGoWrong.mp3",
    ],
    "build": [
        "glados/sounds/Yes.mp3",
        "glados/sounds/KeepDoing.mp3",
    ],
    "win_all": [
        "glados/sounds/Unbelievable.mp3",
        "glados/sounds/Congratulations.mp3",
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
        env = min(1.0, i / (SAMPLE_RATE * 0.003))
        tail = max(0.0, 1.0 - (i / n) * 0.9)
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
    _sfx_dir = tempfile.mkdtemp(prefix="factory-sfx-")
    v = SFX_VOLUME

    # place — short click
    s = _square(600, 0.02, v * 0.4) + _square(800, 0.02, v * 0.5)
    _write_wav(os.path.join(_sfx_dir, "place.wav"), s)
    _sfx_cache["place"] = os.path.join(_sfx_dir, "place.wav")

    # rotate — blip
    s = _triangle(440, 0.03, v * 0.4) + _triangle(660, 0.03, v * 0.5)
    _write_wav(os.path.join(_sfx_dir, "rotate.wav"), s)
    _sfx_cache["rotate"] = os.path.join(_sfx_dir, "rotate.wav")

    # resource_move — soft tick
    s = _square(300, 0.015, v * 0.2, 0.3)
    _write_wav(os.path.join(_sfx_dir, "resource_move.wav"), s)
    _sfx_cache["resource_move"] = os.path.join(_sfx_dir, "resource_move.wav")

    # deliver — cha-ching
    s = (_triangle(880, 0.04, v * 0.5) + _triangle(1320, 0.04, v * 0.55) +
         _triangle(1760, 0.08, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "deliver.wav"), s)
    _sfx_cache["deliver"] = os.path.join(_sfx_dir, "deliver.wav")

    # level_complete — fanfare
    s = (_triangle(523, 0.1, v * 0.5) + _triangle(659, 0.1, v * 0.55) +
         _triangle(784, 0.1, v * 0.6) + _triangle(1047, 0.3, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "level_complete.wav"), s)
    _sfx_cache["level_complete"] = os.path.join(_sfx_dir, "level_complete.wav")

    # error — buzz
    s = _square(150, 0.1, v * 0.3, 0.3) + _square(120, 0.1, v * 0.25, 0.3)
    _write_wav(os.path.join(_sfx_dir, "error.wav"), s)
    _sfx_cache["error"] = os.path.join(_sfx_dir, "error.wav")

    # start
    s = (_triangle(220, 0.05, v * 0.3) + _triangle(330, 0.05, v * 0.35) +
         _triangle(440, 0.05, v * 0.4) + _triangle(554, 0.08, v * 0.45))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

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


# -- level definitions -----------------------------------------------------
# Each level: dict with:
#   "name": str,
#   "target": int (deliveries needed),
#   "belt_budget": int,
#   "furnace_budget": int,
#   "tiles": list of (row, col, tile_type, direction, resource_type_or_None)
#     - For SOURCE: direction = emit direction, resource_type = what it emits
#     - For GOAL: direction ignored, resource_type = what it accepts (None = any)
#     - For WALL: direction/resource ignored

LEVELS = [
    # Level 1: Simple straight line, source left -> goal right
    {
        "name": "BASICS",
        "target": 5,
        "belt_budget": 6,
        "furnace_budget": 0,
        "tiles": [
            (1, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (1, 7, TILE_GOAL, DIR_RIGHT, None),
        ],
    },
    # Level 2: L-shaped path needed
    {
        "name": "L-TURN",
        "target": 5,
        "belt_budget": 8,
        "furnace_budget": 0,
        "tiles": [
            (0, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (2, 3, TILE_GOAL, DIR_RIGHT, None),
        ],
    },
    # Level 3: Need a furnace to convert ore to ingot
    {
        "name": "SMELT",
        "target": 5,
        "belt_budget": 6,
        "furnace_budget": 1,
        "tiles": [
            (1, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (1, 7, TILE_GOAL, DIR_RIGHT, RES_INGOT),
        ],
    },
    # Level 4: Two sources, one goal, merge paths
    {
        "name": "MERGE",
        "target": 8,
        "belt_budget": 10,
        "furnace_budget": 0,
        "tiles": [
            (0, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (2, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (1, 7, TILE_GOAL, DIR_RIGHT, None),
        ],
    },
    # Level 5: Wall obstacle
    {
        "name": "WALLS",
        "target": 8,
        "belt_budget": 10,
        "furnace_budget": 0,
        "tiles": [
            (0, 3, TILE_SOURCE, DIR_DOWN, RES_ORE),
            (2, 3, TILE_GOAL, DIR_RIGHT, None),
            (1, 3, TILE_WALL, 0, None),
            (1, 4, TILE_WALL, 0, None),
        ],
    },
    # Level 6: Two sources, two goals, type matching
    {
        "name": "SORT",
        "target": 10,
        "belt_budget": 12,
        "furnace_budget": 1,
        "tiles": [
            (0, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (2, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (0, 7, TILE_GOAL, DIR_RIGHT, RES_ORE),
            (2, 7, TILE_GOAL, DIR_RIGHT, RES_INGOT),
        ],
    },
    # Level 7: Maze with walls
    {
        "name": "MAZE",
        "target": 8,
        "belt_budget": 14,
        "furnace_budget": 0,
        "tiles": [
            (0, 0, TILE_SOURCE, DIR_DOWN, RES_ORE),
            (2, 7, TILE_GOAL, DIR_RIGHT, None),
            (0, 2, TILE_WALL, 0, None),
            (1, 2, TILE_WALL, 0, None),
            (1, 5, TILE_WALL, 0, None),
            (2, 5, TILE_WALL, 0, None),
        ],
    },
    # Level 8: Double furnace chain
    {
        "name": "CHAIN",
        "target": 8,
        "belt_budget": 10,
        "furnace_budget": 2,
        "tiles": [
            (0, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (2, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (1, 7, TILE_GOAL, DIR_RIGHT, RES_INGOT),
        ],
    },
    # Level 9: Complex routing
    {
        "name": "COMPLEX",
        "target": 10,
        "belt_budget": 16,
        "furnace_budget": 2,
        "tiles": [
            (0, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (2, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (0, 7, TILE_GOAL, DIR_RIGHT, RES_INGOT),
            (2, 7, TILE_GOAL, DIR_RIGHT, RES_ORE),
            (1, 3, TILE_WALL, 0, None),
            (1, 4, TILE_WALL, 0, None),
        ],
    },
    # Level 10: Grand finale
    {
        "name": "FINALE",
        "target": 12,
        "belt_budget": 18,
        "furnace_budget": 3,
        "tiles": [
            (0, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (1, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (2, 0, TILE_SOURCE, DIR_RIGHT, RES_ORE),
            (0, 7, TILE_GOAL, DIR_RIGHT, RES_INGOT),
            (2, 7, TILE_GOAL, DIR_RIGHT, RES_INGOT),
            (1, 3, TILE_WALL, 0, None),
            (0, 4, TILE_WALL, 0, None),
            (2, 4, TILE_WALL, 0, None),
        ],
    },
]


# -- renderers -------------------------------------------------------------

def render_empty_tile(size=SIZE):
    """Dark tile with subtle + mark."""
    img = Image.new("RGB", size, "#1a1a2e")
    d = ImageDraw.Draw(img)
    # Subtle plus
    cx, cy = 48, 48
    d.line([(cx - 8, cy), (cx + 8, cy)], fill="#2d2d52", width=2)
    d.line([(cx, cy - 8), (cx, cy + 8)], fill="#2d2d52", width=2)
    return img


def render_source_tile(direction, res_type, has_resource=False, size=SIZE):
    """Orange tile with resource dot and direction arrow."""
    img = Image.new("RGB", size, "#c2410c")
    d = ImageDraw.Draw(img)
    # Resource dot
    rc = RES_COLORS.get(res_type, "#FFA500")
    d.ellipse([30, 22, 66, 58], fill=rc, outline="#fff", width=1)
    # Direction arrow at bottom
    arrow = DIR_ARROWS[direction]
    d.text((48, 78), arrow, font=_font(20), fill="white", anchor="mm")
    # Label
    d.text((48, 10), "SRC", font=_font(10), fill="#fef3c7", anchor="mm")
    # Resource on tile overlay
    if has_resource:
        _draw_resource_overlay(d, res_type)
    return img


def render_goal_tile(target, collected, want_type=None, size=SIZE):
    """Dark tile with gold star, counter, and optional type indicator."""
    img = Image.new("RGB", size, "#1a1a2e")
    d = ImageDraw.Draw(img)
    # Gold star
    _draw_star(d, 48, 32, 18, "#fbbf24")
    # Type indicator
    if want_type:
        tc = RES_COLORS.get(want_type, "#FFA500")
        d.ellipse([36, 50, 60, 62], fill=tc)
    # Counter
    color = "#34d399" if collected >= target else "#fbbf24"
    d.text((48, 82), f"{collected}/{target}", font=_font(16), fill=color, anchor="mm")
    return img


def _draw_star(draw, cx, cy, r, color):
    """Draw a simple 5-pointed star."""
    points = []
    for i in range(10):
        angle = math.pi / 2 + i * math.pi / 5
        rad = r if i % 2 == 0 else r * 0.4
        points.append((cx + rad * math.cos(angle), cy - rad * math.sin(angle)))
    draw.polygon(points, fill=color)


def _draw_arrow(draw, cx, cy, direction, color="#67e8f9", size=30):
    """Draw a graphical arrow pointing in the given direction."""
    s = size
    hs = s // 2
    if direction == DIR_RIGHT:
        # Shaft + head pointing right
        draw.rectangle([cx - hs, cy - 4, cx + 4, cy + 4], fill=color)
        draw.polygon([(cx + 4, cy - 12), (cx + hs, cy), (cx + 4, cy + 12)], fill=color)
    elif direction == DIR_DOWN:
        draw.rectangle([cx - 4, cy - hs, cx + 4, cy + 4], fill=color)
        draw.polygon([(cx - 12, cy + 4), (cx, cy + hs), (cx + 12, cy + 4)], fill=color)
    elif direction == DIR_LEFT:
        draw.rectangle([cx - 4, cy - 4, cx + hs, cy + 4], fill=color)
        draw.polygon([(cx - 4, cy - 12), (cx - hs, cy), (cx - 4, cy + 12)], fill=color)
    elif direction == DIR_UP:
        draw.rectangle([cx - 4, cy - 4, cx + 4, cy + hs], fill=color)
        draw.polygon([(cx - 12, cy - 4), (cx, cy - hs), (cx + 12, cy - 4)], fill=color)


def render_belt_tile(direction, resource=None, size=SIZE):
    """Dark grey tile with large directional arrow."""
    img = Image.new("RGB", size, "#2d3748")
    d = ImageDraw.Draw(img)
    _draw_arrow(d, 48, 48, direction, "#67e8f9", 36)
    if resource:
        _draw_resource_overlay(d, resource)
    return img


def render_furnace_tile(direction, resource=None, processing=False, size=SIZE):
    """Dark red tile with flame icon and directional indicators."""
    bg = "#7f1d1d" if not processing else "#991b1b"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    # Flame icon (simple triangle + circle)
    d.polygon([(48, 18), (32, 54), (64, 54)], fill="#f97316")
    d.polygon([(48, 28), (40, 54), (56, 54)], fill="#fbbf24")
    # Input/output arrows
    in_dir = direction
    out_dir = (direction + 2) % 4  # opposite side
    # Small direction indicators
    _draw_arrow(d, 30, 78, in_dir, "#9ca3af", 14)
    _draw_arrow(d, 66, 78, out_dir, "#86efac", 14)
    # Processing indicator
    if processing:
        d.rectangle([2, 2, 93, 93], outline="#f97316", width=2)
    if resource:
        _draw_resource_overlay(d, resource)
    return img


def render_wall_tile(size=SIZE):
    """Very dark tile with subtle brick pattern."""
    img = Image.new("RGB", size, "#1f1f1f")
    d = ImageDraw.Draw(img)
    # Brick pattern
    for y in range(0, 96, 16):
        offset = 24 if (y // 16) % 2 else 0
        d.line([(0, y), (96, y)], fill="#2a2a2a", width=1)
        for x in range(offset, 96, 48):
            d.line([(x, y), (x, y + 16)], fill="#2a2a2a", width=1)
    return img


def _draw_resource_overlay(draw, res_type):
    """Draw a small colored dot in bottom-right corner for resource on tile."""
    rc = RES_COLORS.get(res_type, "#FFA500")
    draw.ellipse([70, 4, 90, 24], fill=rc, outline="#fff", width=1)


# -- HUD renderers --------------------------------------------------------

def render_hud_level(level_num, name, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 18), f"LV {level_num}", font=_font(16), fill="#fbbf24", anchor="mm")
    d.text((48, 42), name, font=_font(12), fill="#9ca3af", anchor="mm")
    d.text((48, 68), "LEVEL", font=_font(9), fill="#4b5563", anchor="mm")
    return img


def render_hud_progress(collected, target, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "GOAL", font=_font(10), fill="#9ca3af", anchor="mt")
    color = "#34d399" if collected >= target else "#fbbf24"
    d.text((48, 44), f"{collected}/{target}", font=_font(22), fill=color, anchor="mm")
    # Progress bar
    bar_w = 60
    bar_x = 48 - bar_w // 2
    pct = min(1.0, collected / max(1, target))
    d.rectangle([bar_x, 70, bar_x + bar_w, 78], outline="#4b5563")
    fill_w = int(bar_w * pct)
    if fill_w > 0:
        d.rectangle([bar_x, 70, bar_x + fill_w, 78], fill="#34d399")
    return img


def render_hud_lost(lost_count, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), "LOST", font=_font(10), fill="#9ca3af", anchor="mt")
    color = "#f87171" if lost_count > 0 else "#6b7280"
    d.text((48, 48), str(lost_count), font=_font(26), fill=color, anchor="mm")
    return img


def render_hud_budget(belts_left, furnaces_left, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 8), "BUDGET", font=_font(9), fill="#9ca3af", anchor="mt")
    d.text((48, 32), f"B:{belts_left}", font=_font(16), fill="#67e8f9", anchor="mm")
    d.text((48, 58), f"F:{furnaces_left}", font=_font(16), fill="#f97316", anchor="mm")
    return img


def render_hud_tick(tick_count, size=SIZE):
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 18), "TICK", font=_font(10), fill="#9ca3af", anchor="mm")
    d.text((48, 50), str(tick_count), font=_font(22), fill="#4b5563", anchor="mm")
    return img


def render_hud_play_pause(playing, size=SIZE):
    bg = "#065f46" if not playing else "#7f1d1d"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    if not playing:
        # Play triangle
        d.polygon([(32, 20), (32, 56), (68, 38)], fill="#34d399")
        d.text((48, 76), "PLAY", font=_font(12), fill="#34d399", anchor="mm")
    else:
        # Pause bars
        d.rectangle([32, 20, 42, 56], fill="#f87171")
        d.rectangle([52, 20, 62, 56], fill="#f87171")
        d.text((48, 76), "PAUSE", font=_font(12), fill="#f87171", anchor="mm")
    return img


def render_hud_build(active=False, size=SIZE):
    bg = "#065f46" if not active else "#7f1d1d"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    label = "BUILD" if not active else "CANCEL"
    color = "#34d399" if not active else "#f87171"
    d.text((48, 30), "B", font=_font(28), fill="white", anchor="mm")
    d.text((48, 66), label, font=_font(12), fill=color, anchor="mm")
    return img


def render_build_option(name, selected, build_type, count_left, size=SIZE):
    """Render a build palette option in the HUD."""
    if build_type == TILE_BELT:
        bg = "#2d3748" if count_left > 0 else "#1f2937"
    else:
        bg = "#7f1d1d" if count_left > 0 else "#1f2937"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    if selected:
        d.rectangle([2, 2, 93, 93], outline="#fbbf24", width=3)
    fill = "white" if count_left > 0 else "#6b7280"
    d.text((48, 20), name, font=_font(14), fill=fill, anchor="mm")
    cfill = "#86efac" if count_left > 0 else "#6b7280"
    d.text((48, 48), str(count_left), font=_font(18), fill=cfill, anchor="mm")
    d.text((48, 72), "LEFT", font=_font(9), fill="#6b7280", anchor="mm")
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


def render_celebration_tile(size=SIZE):
    img = Image.new("RGB", size, "#7c3aed")
    d = ImageDraw.Draw(img)
    _draw_star(d, 48, 48, 28, "#fbbf24")
    return img


# -- game ------------------------------------------------------------------

class FactoryGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = False
        self.lock = threading.Lock()
        self.mode = "idle"  # idle | playing | paused | build | level_complete
        self.current_level = 0
        self.tick_count = 0
        self.playing = False  # resource flow active
        self.collected = 0  # resources delivered to goals
        self.lost_count = 0
        self.tick_timer = None
        self.timers = []

        # Grid state: (r, c) -> tile dict
        # tile dict: {"type": str, "dir": int, "res_type": str or None,
        #             "resource": str or None, "processing": bool,
        #             "process_res": str or None}
        self.grid = {}

        # Build state
        self.build_selected = 0  # 0=belt, 1=furnace
        self.belts_placed = 0
        self.furnaces_placed = 0

        # Per-goal tracking for multi-goal levels
        self.goal_collected = {}  # (r,c) -> count

        # Pre-render static tiles
        self.img_empty = render_empty_tile()
        self.img_wall = render_wall_tile()

    def set_key(self, pos, img):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _cancel_all_timers(self):
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    # -- level setup -------------------------------------------------------

    def _load_level(self, level_idx):
        """Set up grid for a level."""
        self.current_level = level_idx
        self.tick_count = 0
        self.collected = 0
        self.lost_count = 0
        self.playing = False
        self.belts_placed = 0
        self.furnaces_placed = 0
        self.goal_collected = {}
        self.grid = {}

        level = LEVELS[level_idx]

        # Place fixed tiles
        for (r, c, ttype, direction, res_type) in level["tiles"]:
            tile = {
                "type": ttype,
                "dir": direction,
                "res_type": res_type,
                "resource": None,
                "processing": False,
                "process_res": None,
            }
            self.grid[(r, c)] = tile
            if ttype == TILE_GOAL:
                self.goal_collected[(r, c)] = 0

    def _level_data(self):
        return LEVELS[self.current_level]

    def _belt_budget_left(self):
        return self._level_data()["belt_budget"] - self.belts_placed

    def _furnace_budget_left(self):
        return self._level_data()["furnace_budget"] - self.furnaces_placed

    # -- idle screen -------------------------------------------------------

    def show_idle(self):
        self.running = False
        self.playing = False
        self.mode = "idle"
        self._cancel_all_timers()
        best = scores.load_best("factory", 0)

        # HUD row
        self.set_key(1, render_title("FACTORY", "CHAIN"))
        for k in range(2, 8):
            self.set_key(k, render_hud_empty())

        if best > 0:
            self.set_key(2, render_title(f"BEST", f"LV {best}"))

        # Game area
        for k in range(8, 32):
            self.set_key(k, self.img_empty)

        self.set_key(20, render_btn("START", "GAME"))

    # -- start game --------------------------------------------------------

    def _start_game(self):
        self.running = True
        self._load_level(0)
        self.mode = "paused"
        play_sfx("start")
        play_voice("start")
        self._render_all()

    def _render_all(self):
        self._render_hud()
        self._render_grid()

    # -- rendering ---------------------------------------------------------

    def _render_hud(self):
        level = self._level_data()
        self.set_key(1, render_hud_level(
            self.current_level + 1, level["name"]))
        self.set_key(2, render_hud_progress(
            self.collected, level["target"]))
        self.set_key(3, render_hud_lost(self.lost_count))
        self.set_key(4, render_hud_budget(
            self._belt_budget_left(), self._furnace_budget_left()))
        self.set_key(5, render_hud_tick(self.tick_count))
        self.set_key(6, render_hud_play_pause(self.playing))
        self.set_key(7, render_hud_build(self.mode == "build"))

    def _render_grid(self):
        for r in range(ROWS):
            for c in range(COLS):
                self._render_tile(r, c)

    def _render_tile(self, r, c):
        pos = rc_to_pos(r, c)
        tile = self.grid.get((r, c))
        if tile is None:
            self.set_key(pos, self.img_empty)
            return

        ttype = tile["type"]
        if ttype == TILE_SOURCE:
            has_res = tile["resource"] is not None
            self.set_key(pos, render_source_tile(
                tile["dir"], tile["res_type"], has_res))
        elif ttype == TILE_GOAL:
            gc = self.goal_collected.get((r, c), 0)
            # Calculate per-goal target
            target = self._goal_target(r, c)
            self.set_key(pos, render_goal_tile(
                target, gc, tile["res_type"]))
        elif ttype == TILE_BELT:
            self.set_key(pos, render_belt_tile(
                tile["dir"], tile["resource"]))
        elif ttype == TILE_FURNACE:
            self.set_key(pos, render_furnace_tile(
                tile["dir"], tile["resource"], tile["processing"]))
        elif ttype == TILE_WALL:
            self.set_key(pos, self.img_wall)

    def _goal_target(self, r, c):
        """Calculate target for a specific goal based on level target split."""
        level = self._level_data()
        goals = [(gr, gc) for (gr, gc, tt, _, _) in level["tiles"]
                 if tt == TILE_GOAL]
        if len(goals) <= 1:
            return level["target"]
        # Split target evenly among goals
        per_goal = level["target"] // len(goals)
        # Give remainder to last goal
        idx = goals.index((r, c))
        if idx == len(goals) - 1:
            return level["target"] - per_goal * (len(goals) - 1)
        return per_goal

    def _render_build_hud(self):
        """Show build palette in HUD."""
        self.set_key(1, render_build_option(
            "BELT", self.build_selected == 0, TILE_BELT,
            self._belt_budget_left()))
        self.set_key(2, render_build_option(
            "FURNACE", self.build_selected == 1, TILE_FURNACE,
            self._furnace_budget_left()))
        for k in range(3, 6):
            self.set_key(k, render_hud_empty())
        self.set_key(6, render_hud_play_pause(self.playing))
        self.set_key(7, render_hud_build(active=True))

    # -- resource simulation -----------------------------------------------

    def _tick(self):
        if not self.running or not self.playing:
            return
        with self.lock:
            self.tick_count += 1
            moved_any = False
            delivered_any = False

            # Phase 1: Furnaces that are processing complete their work
            for (r, c), tile in list(self.grid.items()):
                if tile["type"] == TILE_FURNACE and tile["processing"]:
                    tile["processing"] = False
                    # Output on opposite side
                    out_dir = (tile["dir"] + 2) % 4
                    dr, dc = DIR_DELTAS[out_dir]
                    nr, nc = r + dr, c + dc
                    output_res = RES_INGOT  # furnace converts ore -> ingot
                    if 0 <= nr < ROWS and 0 <= nc < COLS:
                        target = self.grid.get((nr, nc))
                        if target is None:
                            pass  # resource lost (no tile)
                        elif target["type"] == TILE_GOAL:
                            if target["res_type"] is None or target["res_type"] == output_res:
                                self.goal_collected[(nr, nc)] = self.goal_collected.get((nr, nc), 0) + 1
                                self.collected += 1
                                delivered_any = True
                            else:
                                self.lost_count += 1
                        elif target["resource"] is None and target["type"] in (TILE_BELT, TILE_FURNACE, TILE_EMPTY):
                            if target["type"] == TILE_BELT:
                                target["resource"] = output_res
                            elif target["type"] == TILE_FURNACE:
                                if not target["processing"] and target["resource"] is None:
                                    target["resource"] = output_res
                            # TILE_EMPTY: resource lost
                            else:
                                self.lost_count += 1
                            moved_any = True
                        else:
                            self.lost_count += 1
                    else:
                        self.lost_count += 1
                    tile["process_res"] = None

            # Phase 2: Belts push resources (process from farthest to nearest
            # in each belt's direction to avoid cascading issues)
            belt_moves = []
            for (r, c), tile in list(self.grid.items()):
                if tile["type"] == TILE_BELT and tile["resource"] is not None:
                    dr, dc = DIR_DELTAS[tile["dir"]]
                    nr, nc = r + dr, c + dc
                    belt_moves.append((r, c, nr, nc, tile["resource"]))

            # Sort: process belts farthest in their push direction first
            # to prevent double-moves
            for (r, c, nr, nc, res) in belt_moves:
                tile = self.grid.get((r, c))
                if tile is None or tile["resource"] is None:
                    continue  # already moved by another belt
                tile["resource"] = None

                if not (0 <= nr < ROWS and 0 <= nc < COLS):
                    self.lost_count += 1
                    moved_any = True
                    continue

                target = self.grid.get((nr, nc))
                if target is None:
                    # Falls into empty space - lost
                    self.lost_count += 1
                    moved_any = True
                elif target["type"] == TILE_GOAL:
                    if target["res_type"] is None or target["res_type"] == res:
                        self.goal_collected[(nr, nc)] = self.goal_collected.get((nr, nc), 0) + 1
                        self.collected += 1
                        delivered_any = True
                    else:
                        self.lost_count += 1
                    moved_any = True
                elif target["type"] == TILE_BELT:
                    if target["resource"] is None:
                        target["resource"] = res
                        moved_any = True
                    else:
                        self.lost_count += 1
                        moved_any = True
                elif target["type"] == TILE_FURNACE:
                    if not target["processing"] and target["resource"] is None:
                        target["resource"] = res
                        moved_any = True
                    else:
                        self.lost_count += 1
                        moved_any = True
                elif target["type"] == TILE_WALL:
                    self.lost_count += 1
                    moved_any = True
                elif target["type"] == TILE_SOURCE:
                    self.lost_count += 1
                    moved_any = True
                else:
                    self.lost_count += 1
                    moved_any = True

            # Phase 3: Furnaces accept input - start processing
            for (r, c), tile in list(self.grid.items()):
                if tile["type"] == TILE_FURNACE and tile["resource"] is not None and not tile["processing"]:
                    tile["processing"] = True
                    tile["process_res"] = tile["resource"]
                    tile["resource"] = None

            # Phase 4: Sources emit (every 2 ticks)
            if self.tick_count % 2 == 0:
                for (r, c), tile in list(self.grid.items()):
                    if tile["type"] == TILE_SOURCE:
                        dr, dc = DIR_DELTAS[tile["dir"]]
                        nr, nc = r + dr, c + dc
                        if 0 <= nr < ROWS and 0 <= nc < COLS:
                            target = self.grid.get((nr, nc))
                            if target is not None:
                                if target["type"] == TILE_BELT and target["resource"] is None:
                                    target["resource"] = tile["res_type"]
                                    moved_any = True
                                elif target["type"] == TILE_FURNACE and not target["processing"] and target["resource"] is None:
                                    target["resource"] = tile["res_type"]
                                    moved_any = True
                                elif target["type"] == TILE_GOAL:
                                    if target["res_type"] is None or target["res_type"] == tile["res_type"]:
                                        self.goal_collected[(nr, nc)] = self.goal_collected.get((nr, nc), 0) + 1
                                        self.collected += 1
                                        delivered_any = True
                                    else:
                                        self.lost_count += 1
                                # else: target tile occupied or wall, resource lost silently

            if delivered_any:
                play_sfx("deliver")
            elif moved_any:
                play_sfx("resource_move")

            self._render_all()

            # Check level completion
            if self.collected >= self._level_data()["target"]:
                self._level_complete()
                return

        # Schedule next tick
        if self.running and self.playing:
            self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
            self.tick_timer.daemon = True
            self.tick_timer.start()

    def _start_flow(self):
        """Start resource flow."""
        self.playing = True
        self._render_hud()
        self.tick_timer = threading.Timer(TICK_INTERVAL, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    def _pause_flow(self):
        """Pause resource flow."""
        self.playing = False
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None
        self._render_hud()

    # -- level completion --------------------------------------------------

    def _level_complete(self):
        self.playing = False
        self.mode = "level_complete"
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None

        # Save best level
        best = scores.load_best("factory", 0)
        if self.current_level + 1 > best:
            scores.save_best("factory", self.current_level + 1)

        play_sfx("level_complete")
        play_voice("level_complete")

        # Celebration animation
        def _animate():
            for _ in range(3):
                for r in range(ROWS):
                    for c in range(COLS):
                        self.set_key(rc_to_pos(r, c), render_celebration_tile())
                time.sleep(0.4)
                self._render_grid()
                time.sleep(0.4)

            # Check if more levels
            if self.current_level + 1 < len(LEVELS):
                self.set_key(rc_to_pos(1, 3), render_btn("NEXT", "LEVEL"))
                self.set_key(rc_to_pos(1, 4), render_btn("NEXT", "LEVEL"))
            else:
                # All levels complete!
                play_voice("win_all")
                self.set_key(rc_to_pos(1, 2), render_title("ALL", "LEVELS"))
                self.set_key(rc_to_pos(1, 3), render_title("DONE!", ""))
                self.set_key(rc_to_pos(1, 4), render_btn("MENU", ""))

        t = threading.Thread(target=_animate, daemon=True)
        t.start()

    def _advance_level(self):
        """Move to next level."""
        if self.current_level + 1 < len(LEVELS):
            self._load_level(self.current_level + 1)
            self.mode = "paused"
            play_sfx("start")
            self._render_all()
        else:
            self.show_idle()

    # -- build mode --------------------------------------------------------

    def _enter_build(self):
        if self.playing:
            self._pause_flow()
        self.mode = "build"
        self.build_selected = 0
        self._render_build_hud()
        self._render_grid()
        play_sfx("select")

    def _exit_build(self):
        self.mode = "paused"
        self._render_all()

    def _build_at(self, r, c):
        """Place a belt or furnace at the given position."""
        if (r, c) in self.grid:
            return False

        if self.build_selected == 0:
            # Belt
            if self._belt_budget_left() <= 0:
                play_sfx("error")
                return False
            self.grid[(r, c)] = {
                "type": TILE_BELT,
                "dir": DIR_RIGHT,
                "res_type": None,
                "resource": None,
                "processing": False,
                "process_res": None,
            }
            self.belts_placed += 1
        else:
            # Furnace
            if self._furnace_budget_left() <= 0:
                play_sfx("error")
                return False
            self.grid[(r, c)] = {
                "type": TILE_FURNACE,
                "dir": DIR_RIGHT,
                "res_type": None,
                "resource": None,
                "processing": False,
                "process_res": None,
            }
            self.furnaces_placed += 1

        play_sfx("place")
        play_voice("build")
        self._render_tile(r, c)
        self._render_build_hud()
        return True

    def _rotate_tile(self, r, c):
        """Rotate a belt or furnace."""
        tile = self.grid.get((r, c))
        if tile is None:
            return
        if tile["type"] not in (TILE_BELT, TILE_FURNACE):
            return
        tile["dir"] = (tile["dir"] + 1) % 4
        play_sfx("rotate")
        self._render_tile(r, c)

    def _remove_tile(self, r, c):
        """Remove a player-placed tile (belt or furnace)."""
        tile = self.grid.get((r, c))
        if tile is None:
            return
        if tile["type"] == TILE_BELT:
            self.belts_placed -= 1
            del self.grid[(r, c)]
        elif tile["type"] == TILE_FURNACE:
            self.furnaces_placed -= 1
            del self.grid[(r, c)]
        else:
            return  # can't remove fixed tiles
        self._render_tile(r, c)
        self._render_build_hud()

    # -- key handler -------------------------------------------------------

    def on_key(self, _deck, key, pressed):
        if not pressed:
            return
        with self.lock:
            if self.mode == "idle":
                self._on_idle(key)
            elif self.mode == "paused":
                self._on_paused(key)
            elif self.mode == "playing":
                self._on_playing(key)
            elif self.mode == "build":
                self._on_build(key)
            elif self.mode == "level_complete":
                self._on_level_complete(key)

    def _on_idle(self, key):
        if key == 20:
            self._start_game()

    def _on_paused(self, key):
        if key == 6:
            # Play
            self.mode = "playing"
            self._start_flow()
            return
        if key == 7:
            self._enter_build()
            return
        # Game grid: rotate existing belts/furnaces
        if key >= ROW_OFFSET * COLS and key < (ROW_OFFSET + ROWS) * COLS:
            r, c = pos_to_rc(key)
            tile = self.grid.get((r, c))
            if tile and tile["type"] in (TILE_BELT, TILE_FURNACE):
                self._rotate_tile(r, c)

    def _on_playing(self, key):
        if key == 6:
            # Pause
            self.mode = "paused"
            self._pause_flow()
            return
        if key == 7:
            self._enter_build()
            return
        # Game grid: rotate belts/furnaces while playing
        if key >= ROW_OFFSET * COLS and key < (ROW_OFFSET + ROWS) * COLS:
            r, c = pos_to_rc(key)
            tile = self.grid.get((r, c))
            if tile and tile["type"] in (TILE_BELT, TILE_FURNACE):
                self._rotate_tile(r, c)

    def _on_build(self, key):
        if key == 7:
            self._exit_build()
            return
        if key == 1:
            self.build_selected = 0
            self._render_build_hud()
            play_sfx("select")
            return
        if key == 2:
            self.build_selected = 1
            self._render_build_hud()
            play_sfx("select")
            return
        # Game grid
        if key >= ROW_OFFSET * COLS and key < (ROW_OFFSET + ROWS) * COLS:
            r, c = pos_to_rc(key)
            tile = self.grid.get((r, c))
            if tile is None:
                # Empty: place new tile
                self._build_at(r, c)
            elif tile["type"] in (TILE_BELT, TILE_FURNACE):
                # Existing player tile: rotate it
                self._rotate_tile(r, c)

    def _on_level_complete(self, key):
        # Any game grid press advances
        if key >= ROW_OFFSET * COLS and key < (ROW_OFFSET + ROWS) * COLS:
            self._advance_level()
        # Also HUD presses
        elif 1 <= key <= 7:
            self._advance_level()


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
    print("FACTORY CHAIN -- conveyor puzzle game!")

    game = FactoryGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        game._cancel_all_timers()
        game.running = False
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
