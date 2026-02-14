"""Pac-Man — Stream Deck mini-game.

Classic Pac-Man on a 3x8 grid. Tap any cell to steer.
Eat all dots, avoid ghosts, grab power pellets to fight back!

Usage:
    uv run python scripts/pacman_game.py
"""

import math
import os
import random
import struct
import subprocess
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

# -- config ---------------------------------------------------------------
GAME_KEYS = list(range(8, 32))  # rows 2-4 = game area
HUD_KEYS = list(range(0, 8))   # row 1 = HUD
ROWS = 3
COLS = 8
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

# Direction vectors: (d_row, d_col)
DIR_LEFT = (0, -1)
DIR_RIGHT = (0, 1)
DIR_UP = (-1, 0)
DIR_DOWN = (1, 0)
DIR_NONE = (0, 0)

# Tick speeds
PAC_TICK_START = 0.6   # seconds per Pac-Man move
GHOST_TICK_START = 0.8  # seconds per ghost move
GHOST_TICK_MIN = 0.4    # fastest ghost speed
GHOST_SPEEDUP = 0.05    # seconds faster per level
SCARED_DURATION = 5.0   # seconds ghosts stay scared

# Lives
MAX_LIVES = 3

# Start button
START_KEY = 20  # center-ish button for START

# Corner positions for power pellets (in grid coords)
POWER_PELLET_POSITIONS = [
    (0, 0),   # button 8
    (0, 7),   # button 15
    (2, 0),   # button 24
    (2, 7),   # button 31
]

# Ghost spawn position (center of grid)
GHOST_SPAWN = (1, 3)
GHOST_SPAWN_2 = (1, 4)

# Pac-Man start position (bottom-left)
PAC_START = (2, 0)

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "hd2_helldiver/sounds/ReportingForDuty1.mp3",
        "hd2_helldiver/sounds/ReadyToLiberate1.mp3",
        "hd2_helldiver/sounds/DemocracyHasLanded.mp3",
    ],
    "power_pellet": [
        "hd2_helldiver/sounds/SayHelloToDemocracy.mp3",
        "hd2_helldiver/sounds/GetSome.mp3",
        "hd2_helldiver/sounds/ALittleShotOfLiberty.mp3",
        "hd2_helldiver/sounds/PointMeToTheEnemy.mp3",
    ],
    "death": [
        "hd2_helldiver/sounds/Negative.mp3",
        "hd2_helldiver/sounds/ImSorry.mp3",
        "hd2_helldiver/sounds/CancelThat.mp3",
    ],
    "level_clear": [
        "hd2_helldiver/sounds/ObjectiveCompleted.mp3",
        "hd2_helldiver/sounds/DemocracyForAll.mp3",
        "hd2_helldiver/sounds/FreedomNeverSleeps.mp3",
    ],
    "newbest": [
        "hd2_helldiver/sounds/ForSuperEarth.mp3",
        "hd2_helldiver/sounds/IFightForSuperEarth.mp3",
    ],
}
_last_orc_time: float = 0
ORC_COOLDOWN = 4.0


def play_orc(event: str):
    """Play a random orc voice line -- with cooldown to avoid spam."""
    global _last_orc_time
    now = time.monotonic()
    if now - _last_orc_time < ORC_COOLDOWN:
        return
    paths = ORC_VOICES.get(event, [])
    if not paths:
        return
    random.shuffle(paths)
    for rel in paths:
        full = os.path.join(PEON_DIR, rel)
        if os.path.exists(full):
            _last_orc_time = now
            sound_engine.play_voice(full)
            return


# -- font ------------------------------------------------------------------
def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


# -- 8-bit sound engine ----------------------------------------------------
SAMPLE_RATE = 22050
_sfx_cache: dict[str, str] = {}
_sfx_dir: str = ""


def _triangle(freq: float, dur: float, vol: float = 1.0) -> list[float]:
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
            tail = max(0.0, 1.0 - (i / n) * 0.5)
            samples.append(val * env * tail)
    return samples


def _square(freq: float, dur: float, vol: float = 1.0, duty: float = 0.5) -> list[float]:
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


def _write_wav(path: str, samples: list[float]):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for s in samples:
            s = max(-0.95, min(0.95, s))
            w.writeframes(struct.pack("<h", int(s * 32767)))


def _generate_sfx():
    """Generate all game sound effects as WAV files."""
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="pacman-sfx-")
    v = SFX_VOLUME

    # CHOMP -- waka-waka blip (alternating pitch)
    s = (_square(440, 0.03, v * 0.5, 0.3) +
         _square(349, 0.03, v * 0.4, 0.3) +
         _square(440, 0.03, v * 0.5, 0.3))
    _write_wav(os.path.join(_sfx_dir, "chomp.wav"), s)
    _sfx_cache["chomp"] = os.path.join(_sfx_dir, "chomp.wav")

    # GHOST_EAT -- rising victory note (C5->E5->G5->C6)
    s = (_triangle(523, 0.05, v * 0.5) +
         _triangle(659, 0.05, v * 0.55) +
         _triangle(784, 0.05, v * 0.6) +
         _triangle(1047, 0.1, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "ghost_eat.wav"), s)
    _sfx_cache["ghost_eat"] = os.path.join(_sfx_dir, "ghost_eat.wav")

    # DEATH -- descending (A4->F4->D4->A3)
    s = (_square(440, 0.1, v * 0.5, 0.5) +
         _square(349, 0.1, v * 0.45, 0.5) +
         _square(294, 0.12, v * 0.4, 0.5) +
         _square(220, 0.25, v * 0.35, 0.5))
    _write_wav(os.path.join(_sfx_dir, "death.wav"), s)
    _sfx_cache["death"] = os.path.join(_sfx_dir, "death.wav")

    # POWER -- dramatic chord (C4+E4+G4 sustained)
    s_c = _triangle(262, 0.3, v * 0.35)
    s_e = _triangle(330, 0.3, v * 0.35)
    s_g = _triangle(392, 0.3, v * 0.4)
    combined = []
    for i in range(len(s_c)):
        combined.append(s_c[i] + s_e[i] + s_g[i])
    _write_wav(os.path.join(_sfx_dir, "power.wav"), combined)
    _sfx_cache["power"] = os.path.join(_sfx_dir, "power.wav")

    # START -- exciting power-up (E4->G4->B4->E5)
    s = (_triangle(330, 0.06, v * 0.4) +
         _triangle(392, 0.06, v * 0.45) +
         _triangle(494, 0.06, v * 0.5) +
         _triangle(659, 0.12, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # NEW BEST -- victory jingle (C5->E5->G5->C6)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) +
         _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "newbest.wav"), s)
    _sfx_cache["newbest"] = os.path.join(_sfx_dir, "newbest.wav")


def play_sfx(name: str):
    """Play sound non-blocking via afplay."""
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos: int) -> tuple[int, int]:
    """Button position (8-31) -> (row, col) in 3x8 grid."""
    return (pos - 8) // COLS, (pos - 8) % COLS


def rc_to_pos(row: int, col: int) -> int:
    """(row, col) in 3x8 grid -> button position (8-31)."""
    return 8 + row * COLS + col


# -- renderers -------------------------------------------------------------

BG_COLOR = "#0f172a"
PAC_COLOR = "#fbbf24"
GHOST_RED = "#ef4444"
GHOST_PINK = "#ec4899"
GHOST_SCARED = "#3b82f6"
DOT_COLOR = "#fbbf24"


def render_pacman(direction: tuple[int, int], size=SIZE) -> Image.Image:
    """Pac-Man -- yellow circle with mouth (pie-slice cutout facing direction)."""
    img = Image.new("RGB", size, BG_COLOR)
    d = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    r = 34  # radius

    # Determine mouth angle based on direction
    dr, dc = direction
    if dc == 1:       # right
        start_angle = 35
    elif dc == -1:    # left
        start_angle = 215
    elif dr == -1:    # up
        start_angle = 305
    elif dr == 1:     # down
        start_angle = 125
    else:             # default right
        start_angle = 35

    end_angle = start_angle + 290
    d.pieslice(
        [cx - r, cy - r, cx + r, cy + r],
        start=start_angle, end=end_angle,
        fill=PAC_COLOR,
    )
    # Eye -- small black dot offset from center toward top-forward
    eye_offset_x = 0
    eye_offset_y = -12
    if dc == 1:
        eye_offset_x = 8
    elif dc == -1:
        eye_offset_x = -8
    elif dr == -1:
        eye_offset_y = -8
        eye_offset_x = 8
    elif dr == 1:
        eye_offset_y = 8
        eye_offset_x = 8
    ex = cx + eye_offset_x
    ey = cy + eye_offset_y
    d.ellipse([ex - 4, ey - 4, ex + 4, ey + 4], fill="black")
    return img


def render_dot(size=SIZE) -> Image.Image:
    """Small yellow dot on dark background."""
    img = Image.new("RGB", size, BG_COLOR)
    d = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    r = 6
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DOT_COLOR)
    return img


def render_power_pellet(pulse_phase: float = 0.0, size=SIZE) -> Image.Image:
    """Large bright yellow power pellet with pulsing effect."""
    img = Image.new("RGB", size, BG_COLOR)
    d = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    # Pulse between radius 11 and 17
    base_r = 14
    pulse_r = base_r + int(3 * math.sin(pulse_phase * math.pi * 2))
    d.ellipse(
        [cx - pulse_r, cy - pulse_r, cx + pulse_r, cy + pulse_r],
        fill="#facc15",
    )
    # Bright center highlight
    hr = pulse_r // 3
    d.ellipse(
        [cx - hr, cy - hr, cx + hr, cy + hr],
        fill="#fef08a",
    )
    return img


def _draw_ghost_body(d: ImageDraw.Draw, cx: int, cy: int, color: str, r: int = 30):
    """Draw a ghost shape: rounded top, wavy bottom, with eyes."""
    # Body -- rounded rectangle top, wavy bottom
    top = cy - r
    bottom = cy + r
    left = cx - r
    right = cx + r

    # Top half: semicircle
    d.pieslice([left, top, right, top + 2 * r], start=180, end=0, fill=color)
    # Bottom half: rectangle
    d.rectangle([left, cy, right, bottom], fill=color)
    # Wavy bottom edge: 3 scallops
    wave_h = 8
    seg_w = (2 * r) // 3
    for i in range(3):
        sx = left + i * seg_w
        d.pieslice(
            [sx, bottom - wave_h, sx + seg_w, bottom + wave_h],
            start=0, end=180,
            fill=BG_COLOR,
        )

    # Eyes -- white ovals with blue pupils
    eye_w = 10
    eye_h = 12
    eye_y = cy - 6
    # Left eye
    lex = cx - 10
    d.ellipse([lex - eye_w // 2, eye_y - eye_h // 2,
               lex + eye_w // 2, eye_y + eye_h // 2], fill="white")
    d.ellipse([lex - 3, eye_y - 2, lex + 3, eye_y + 4], fill="#3b82f6")
    # Right eye
    rex = cx + 10
    d.ellipse([rex - eye_w // 2, eye_y - eye_h // 2,
               rex + eye_w // 2, eye_y + eye_h // 2], fill="white")
    d.ellipse([rex - 3, eye_y - 2, rex + 3, eye_y + 4], fill="#3b82f6")


def render_ghost(color: str, size=SIZE) -> Image.Image:
    """Normal ghost with specified color."""
    img = Image.new("RGB", size, BG_COLOR)
    d = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    _draw_ghost_body(d, cx, cy, color)
    return img


def render_scared_ghost(size=SIZE) -> Image.Image:
    """Blue scared ghost."""
    img = Image.new("RGB", size, BG_COLOR)
    d = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    _draw_ghost_body(d, cx, cy, GHOST_SCARED)
    return img


def render_empty(size=SIZE) -> Image.Image:
    """Empty cell (dot eaten)."""
    return Image.new("RGB", size, BG_COLOR)


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "PAC", font=_font(22), fill=PAC_COLOR, anchor="mt")
    # Small pac-man icon
    d.pieslice([32, 52, 64, 72], start=35, end=325, fill=PAC_COLOR)
    return img


def render_hud_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(score), font=_font(32), fill=PAC_COLOR, anchor="mt")
    return img


def render_hud_best(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(best), font=_font(28), fill="#34d399", anchor="mt")
    return img


def render_hud_lives(lives: int, size=SIZE) -> Image.Image:
    """Show remaining lives as small pac-man icons."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 16), "LIVES", font=_font(12), fill="#9ca3af", anchor="mt")
    # Draw small pac-man icons for each life
    y = 50
    total_w = lives * 22 + (lives - 1) * 4 if lives > 0 else 0
    start_x = (96 - total_w) // 2
    for i in range(lives):
        x = start_x + i * 26
        d.pieslice([x, y, x + 20, y + 20], start=35, end=325, fill=PAC_COLOR)
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#92400e")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill=PAC_COLOR, anchor="mm")
    return img


def render_game_over(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white", anchor="mm", align="center")
    return img


def render_level_clear(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "LEVEL", font=_font(18), fill="white", anchor="mm")
    d.text((48, 60), "CLEAR!", font=_font(16), fill="#34d399", anchor="mm")
    return img


# -- ghost data class ------------------------------------------------------

class Ghost:
    def __init__(self, row: int, col: int, color: str):
        self.row = row
        self.col = col
        self.color = color
        self.scared = False
        self.eaten = False  # eaten during scared mode, respawning
        self.spawn_row = row
        self.spawn_col = col

    def respawn(self):
        self.row = self.spawn_row
        self.col = self.spawn_col
        self.scared = False
        self.eaten = False


# -- game logic ------------------------------------------------------------

class PacmanGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.best = scores.load_best("pacman")
        self.lives = MAX_LIVES
        self.level = 1
        self.running = False
        self.game_over = False
        self.lock = threading.Lock()
        self.pac_timer = None
        self.ghost_timer = None
        self.pellet_timer = None
        self.scared_timer = None
        # Pac-Man state
        self.pac_row = PAC_START[0]
        self.pac_col = PAC_START[1]
        self.direction: tuple[int, int] = DIR_RIGHT
        self.next_direction: tuple[int, int] = DIR_RIGHT
        # Dots and power pellets
        self.dots: set[tuple[int, int]] = set()
        self.power_pellets: set[tuple[int, int]] = set()
        # Ghosts
        self.ghosts: list[Ghost] = []
        self.ghost_speed = GHOST_TICK_START
        self.scared_until: float = 0.0
        # Pellet pulse animation
        self._pulse_phase: float = 0.0
        # Pre-render reusable images
        self.img_dot = render_dot()
        self.img_empty = render_empty()
        self.img_start = render_start()
        self.img_hud_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_game_over = render_game_over()
        self.img_level_clear = render_level_clear()
        self.img_ghost_red = render_ghost(GHOST_RED)
        self.img_ghost_pink = render_ghost(GHOST_PINK)
        self.img_ghost_scared = render_scared_ghost()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # -- idle / menu -------------------------------------------------------

    def show_idle(self):
        """Show start screen."""
        self.running = False
        self.game_over = False
        self._cancel_all_timers()
        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(0))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_lives(MAX_LIVES))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)
        # Game area: show dots pattern with start button
        for k in GAME_KEYS:
            if k == START_KEY:
                self.set_key(k, self.img_start)
            else:
                rc = pos_to_rc(k)
                if rc in POWER_PELLET_POSITIONS:
                    self.set_key(k, render_power_pellet(0.0))
                else:
                    self.set_key(k, self.img_dot)

    # -- game start --------------------------------------------------------

    def start_game(self):
        """Initialize and start a new game."""
        with self.lock:
            self.score = 0
            self.lives = MAX_LIVES
            self.level = 1
            self.ghost_speed = GHOST_TICK_START
            self.running = True
            self.game_over = False
            self._init_level()

        play_sfx("start")
        play_orc("start")

        self._draw_board()
        self._update_hud()
        self._schedule_pac_tick()
        self._schedule_ghost_tick()
        self._schedule_pellet_pulse()

    def _init_level(self):
        """Set up dots, pellets, Pac-Man, and ghosts for current level."""
        # Pac-Man position
        self.pac_row = PAC_START[0]
        self.pac_col = PAC_START[1]
        self.direction = DIR_RIGHT
        self.next_direction = DIR_RIGHT
        # All cells start with dots
        self.dots = set()
        self.power_pellets = set()
        for r in range(ROWS):
            for c in range(COLS):
                if (r, c) in POWER_PELLET_POSITIONS:
                    self.power_pellets.add((r, c))
                elif (r, c) != (self.pac_row, self.pac_col):
                    self.dots.add((r, c))
        # Ghosts
        self.ghosts = [
            Ghost(GHOST_SPAWN[0], GHOST_SPAWN[1], GHOST_RED),
            Ghost(GHOST_SPAWN_2[0], GHOST_SPAWN_2[1], GHOST_PINK),
        ]
        self.scared_until = 0.0

    # -- timers ------------------------------------------------------------

    def _cancel_all_timers(self):
        self._cancel_pac_tick()
        self._cancel_ghost_tick()
        self._cancel_pellet_pulse()
        if self.scared_timer:
            self.scared_timer.cancel()
            self.scared_timer = None

    def _schedule_pac_tick(self):
        self._cancel_pac_tick()
        if not self.running:
            return
        self.pac_timer = threading.Timer(PAC_TICK_START, self._pac_tick)
        self.pac_timer.daemon = True
        self.pac_timer.start()

    def _cancel_pac_tick(self):
        if self.pac_timer:
            self.pac_timer.cancel()
            self.pac_timer = None

    def _schedule_ghost_tick(self):
        self._cancel_ghost_tick()
        if not self.running:
            return
        self.ghost_timer = threading.Timer(self.ghost_speed, self._ghost_tick)
        self.ghost_timer.daemon = True
        self.ghost_timer.start()

    def _cancel_ghost_tick(self):
        if self.ghost_timer:
            self.ghost_timer.cancel()
            self.ghost_timer = None

    def _schedule_pellet_pulse(self):
        if self.pellet_timer:
            self.pellet_timer.cancel()
            self.pellet_timer = None
        if not self.running:
            return
        self.pellet_timer = threading.Timer(0.25, self._pellet_pulse_tick)
        self.pellet_timer.daemon = True
        self.pellet_timer.start()

    def _cancel_pellet_pulse(self):
        if self.pellet_timer:
            self.pellet_timer.cancel()
            self.pellet_timer = None

    # -- Pac-Man tick (auto-move) ------------------------------------------

    def _pac_tick(self):
        if not self.running:
            return
        with self.lock:
            self._move_pacman()
        if self.running:
            self._schedule_pac_tick()

    def _move_pacman(self):
        """Move Pac-Man one step. Must hold lock."""
        self.direction = self.next_direction
        if self.direction == DIR_NONE:
            return

        dr, dc = self.direction
        new_r = (self.pac_row + dr) % ROWS
        new_c = (self.pac_col + dc) % COLS

        # Clear old position
        old_r, old_c = self.pac_row, self.pac_col
        self.pac_row = new_r
        self.pac_col = new_c

        # Redraw old position
        self._draw_cell(old_r, old_c)

        # Check for dot eating
        if (new_r, new_c) in self.dots:
            self.dots.discard((new_r, new_c))
            self.score += 1
            play_sfx("chomp")
        elif (new_r, new_c) in self.power_pellets:
            self.power_pellets.discard((new_r, new_c))
            self.score += 1
            self._activate_scared_mode()
            play_sfx("power")
            play_orc("power_pellet")

        # Check ghost collision
        if self._check_ghost_collision():
            return

        # Draw Pac-Man at new position
        self.set_key(rc_to_pos(new_r, new_c), render_pacman(self.direction))
        self._update_hud()

        # Check level clear
        if not self.dots and not self.power_pellets:
            self._level_clear()

    def _activate_scared_mode(self):
        """Make all ghosts scared for SCARED_DURATION seconds."""
        self.scared_until = time.monotonic() + SCARED_DURATION
        for g in self.ghosts:
            if not g.eaten:
                g.scared = True
        # Cancel any existing scared timer
        if self.scared_timer:
            self.scared_timer.cancel()
        self.scared_timer = threading.Timer(SCARED_DURATION, self._end_scared_mode)
        self.scared_timer.daemon = True
        self.scared_timer.start()
        # Redraw ghosts as scared
        for g in self.ghosts:
            if g.scared:
                self.set_key(rc_to_pos(g.row, g.col), self.img_ghost_scared)

    def _end_scared_mode(self):
        """End scared mode for all ghosts."""
        with self.lock:
            self.scared_until = 0.0
            for g in self.ghosts:
                g.scared = False
            # Redraw ghosts in normal colors
            for g in self.ghosts:
                self._draw_ghost(g)

    def _check_ghost_collision(self) -> bool:
        """Check if Pac-Man collides with any ghost. Returns True if died."""
        for g in self.ghosts:
            if g.row == self.pac_row and g.col == self.pac_col:
                if g.scared:
                    # Eat the ghost
                    self.score += 5
                    g.eaten = True
                    g.scared = False
                    g.respawn()
                    play_sfx("ghost_eat")
                    self._draw_ghost(g)
                    self._update_hud()
                else:
                    # Pac-Man dies
                    self._lose_life()
                    return True
        return False

    # -- ghost tick ---------------------------------------------------------

    def _ghost_tick(self):
        if not self.running:
            return
        with self.lock:
            self._move_ghosts()
        if self.running:
            self._schedule_ghost_tick()

    def _move_ghosts(self):
        """Move all ghosts one step toward Pac-Man. Must hold lock."""
        for g in self.ghosts:
            old_r, old_c = g.row, g.col

            # Ghost AI: move toward Pac-Man's row/col
            dr = 0
            dc = 0
            row_diff = self.pac_row - g.row
            col_diff = self.pac_col - g.col

            if g.scared:
                # Run away from Pac-Man
                row_diff = -row_diff
                col_diff = -col_diff

            abs_row = abs(row_diff)
            abs_col = abs(col_diff)

            if abs_row > abs_col:
                dr = 1 if row_diff > 0 else -1
            elif abs_col > abs_row:
                dc = 1 if col_diff > 0 else -1
            else:
                # Tied -- random choice
                if random.random() < 0.5:
                    dr = 1 if row_diff > 0 else (-1 if row_diff < 0 else 0)
                else:
                    dc = 1 if col_diff > 0 else (-1 if col_diff < 0 else 0)

            # If still no movement (same pos as target), pick random direction
            if dr == 0 and dc == 0:
                choices = [(0, 1), (0, -1), (1, 0), (-1, 0)]
                dr, dc = random.choice(choices)

            new_r = (g.row + dr) % ROWS
            new_c = (g.col + dc) % COLS

            # Check if another ghost is already there
            blocked = False
            for other in self.ghosts:
                if other is not g and other.row == new_r and other.col == new_c:
                    blocked = True
                    break

            if not blocked:
                g.row = new_r
                g.col = new_c

            # Redraw old cell
            self._draw_cell(old_r, old_c)
            # Draw ghost at new position
            self._draw_ghost(g)

        # Check ghost collision after all moves
        if self._check_ghost_collision():
            return

    def _draw_ghost(self, g: Ghost):
        """Draw a ghost at its current position."""
        if g.scared:
            self.set_key(rc_to_pos(g.row, g.col), self.img_ghost_scared)
        elif g.color == GHOST_RED:
            self.set_key(rc_to_pos(g.row, g.col), self.img_ghost_red)
        else:
            self.set_key(rc_to_pos(g.row, g.col), self.img_ghost_pink)

    # -- pellet pulse animation -------------------------------------------

    def _pellet_pulse_tick(self):
        if not self.running:
            return
        with self.lock:
            self._pulse_phase += 0.25
            if self._pulse_phase >= 1.0:
                self._pulse_phase = 0.0
            for pp in self.power_pellets:
                # Only redraw if no ghost or pac-man is on this cell
                if (pp[0], pp[1]) != (self.pac_row, self.pac_col):
                    ghost_here = False
                    for g in self.ghosts:
                        if g.row == pp[0] and g.col == pp[1]:
                            ghost_here = True
                            break
                    if not ghost_here:
                        self.set_key(
                            rc_to_pos(pp[0], pp[1]),
                            render_power_pellet(self._pulse_phase),
                        )
        if self.running:
            self._schedule_pellet_pulse()

    # -- life loss ---------------------------------------------------------

    def _lose_life(self):
        """Pac-Man caught by a ghost. Must hold lock."""
        self.lives -= 1
        self._cancel_all_timers()

        if self.lives <= 0:
            self.running = False
            self.game_over = True
            new_best = self.score > self.best
            if new_best:
                self.best = self.score
                scores.save_best("pacman", self.best)
            if new_best and self.score > 0:
                play_sfx("newbest")
                play_orc("newbest")
            else:
                play_sfx("death")
                play_orc("death")
            self._show_game_over()
        else:
            play_sfx("death")
            play_orc("death")
            # Brief flash, then reset positions
            self.set_key(
                rc_to_pos(self.pac_row, self.pac_col),
                Image.new("RGB", SIZE, "#dc2626"),
            )

            def _respawn():
                with self.lock:
                    if not self.running and not self.game_over:
                        return
                    # Reset positions only
                    self.pac_row = PAC_START[0]
                    self.pac_col = PAC_START[1]
                    self.direction = DIR_RIGHT
                    self.next_direction = DIR_RIGHT
                    for g in self.ghosts:
                        g.respawn()
                    self.scared_until = 0.0
                    self.running = True
                self._draw_board()
                self._update_hud()
                self._schedule_pac_tick()
                self._schedule_ghost_tick()
                self._schedule_pellet_pulse()

            threading.Timer(1.0, _respawn).start()

    # -- level clear -------------------------------------------------------

    def _level_clear(self):
        """All dots eaten -- advance to next level."""
        self._cancel_all_timers()
        self.running = False

        play_sfx("power")
        play_orc("level_clear")

        # Flash level clear
        for k in GAME_KEYS:
            self.set_key(k, self.img_level_clear if k in (19, 20, 21) else self.img_empty)
        self._update_hud()

        def _next_level():
            with self.lock:
                self.level += 1
                self.ghost_speed = max(
                    GHOST_TICK_MIN,
                    GHOST_TICK_START - (self.level - 1) * GHOST_SPEEDUP,
                )
                self.running = True
                self._init_level()
            self._draw_board()
            self._update_hud()
            self._schedule_pac_tick()
            self._schedule_ghost_tick()
            self._schedule_pellet_pulse()

        threading.Timer(2.0, _next_level).start()

    # -- game over ---------------------------------------------------------

    def _show_game_over(self):
        """Display game over screen."""
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_lives(0))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Flash Pac-Man red
        self.set_key(
            rc_to_pos(self.pac_row, self.pac_col),
            Image.new("RGB", SIZE, "#dc2626"),
        )

        def _show_over():
            if self.game_over:
                for k in GAME_KEYS:
                    if k == START_KEY:
                        self.set_key(k, self.img_start)
                    elif k in (18, 19, 21):
                        self.set_key(k, self.img_game_over)
                    else:
                        self.set_key(k, self.img_empty)

        threading.Timer(0.5, _show_over).start()

    # -- drawing -----------------------------------------------------------

    def _draw_cell(self, r: int, c: int):
        """Redraw a single cell based on game state."""
        pos = rc_to_pos(r, c)
        # Check if Pac-Man is here
        if r == self.pac_row and c == self.pac_col:
            self.set_key(pos, render_pacman(self.direction))
            return
        # Check if a ghost is here
        for g in self.ghosts:
            if g.row == r and g.col == c:
                self._draw_ghost(g)
                return
        # Check for power pellet
        if (r, c) in self.power_pellets:
            self.set_key(pos, render_power_pellet(self._pulse_phase))
            return
        # Check for dot
        if (r, c) in self.dots:
            self.set_key(pos, self.img_dot)
            return
        # Empty
        self.set_key(pos, self.img_empty)

    def _draw_board(self):
        """Redraw the entire game area."""
        for r in range(ROWS):
            for c in range(COLS):
                self._draw_cell(r, c)

    def _update_hud(self):
        """Update HUD displays."""
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_lives(self.lives))

    # -- input -------------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        # Tap-to-steer: tap any game cell to change direction
        if 8 <= key <= 31:
            tap_r, tap_c = pos_to_rc(key)
            with self.lock:
                dr = tap_r - self.pac_row
                dc = tap_c - self.pac_col

                if dr == 0 and dc == 0:
                    return  # tapped on pac-man

                cur_dr, cur_dc = self.direction

                if cur_dr != 0:
                    # Moving vertically — prefer horizontal turn
                    if dc > 0:
                        new_dir = DIR_RIGHT
                    elif dc < 0:
                        new_dir = DIR_LEFT
                    elif dr > 0:
                        new_dir = DIR_DOWN
                    else:
                        new_dir = DIR_UP
                else:
                    # Moving horizontally — prefer vertical turn
                    if dr > 0:
                        new_dir = DIR_DOWN
                    elif dr < 0:
                        new_dir = DIR_UP
                    elif dc > 0:
                        new_dir = DIR_RIGHT
                    else:
                        new_dir = DIR_LEFT

                self.next_direction = new_dir


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

    # Generate 8-bit sound effects
    try:
        _generate_sfx()
        print("Sound effects: ON")
    except Exception:
        print("Sound effects: OFF (generation failed)")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("PAC-MAN! Press the center button to start.")
    print("Controls: Tap any cell on the grid to steer.")

    game = PacmanGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(f"\nBye! Final score: {game.score} Best: {game.best}")
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
