"""Breakout — Stream Deck mini-game.

Classic Arkanoid on a 3x8 grid. Smash bricks with a bouncing ball!
Tap bottom row to move paddle, tap anywhere to launch ball.

Usage:
    uv run python scripts/breakout_game.py
"""

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

import sound_engine
import scores

# -- config ---------------------------------------------------------------
GAME_KEYS = list(range(8, 32))  # rows 2-4 = game area
HUD_KEYS = list(range(0, 8))   # row 1 = HUD
ROWS = 3
COLS = 8
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

# Tick speed
TICK_START = 0.5    # seconds per ball move
TICK_MIN = 0.20     # fastest
TICK_SPEEDUP = 0.03 # seconds faster per level

START_KEY = 20  # center-ish button for START
LIVES_START = 3

# Brick colors -- rainbow across 8 columns
BRICK_COLORS = [
    "#ef4444",  # red
    "#f97316",  # orange
    "#eab308",  # yellow
    "#22c55e",  # green
    "#06b6d4",  # cyan
    "#3b82f6",  # blue
    "#8b5cf6",  # purple
    "#ec4899",  # pink
]

COLOR_PADDLE = "#e5e7eb"
COLOR_BALL = "#fbbf24"
COLOR_EMPTY = "#0f172a"
COLOR_HUD_BG = "#111827"

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "tf2_engineer/sounds/Engineer_battlecry01.mp3",
        "tf2_engineer/sounds/Engineer_battlecry03.mp3",
        "tf2_engineer/sounds/Engineer_autobuildingsentry01.mp3",
    ],
    "level_clear": [
        "tf2_engineer/sounds/Engineer_cheers03.mp3",
        "tf2_engineer/sounds/Engineer_cheers04.mp3",
        "tf2_engineer/sounds/Engineer_specialcompleted02.mp3",
    ],
    "gameover": [
        "tf2_engineer/sounds/Engineer_autodejectedtie01.mp3",
        "tf2_engineer/sounds/Engineer_autodejectedtie02.mp3",
        "tf2_engineer/sounds/Engineer_helpme01.mp3",
    ],
    "newbest": [
        "tf2_engineer/sounds/Eng_quest_complete_easy_01.mp3",
        "tf2_engineer/sounds/Eng_quest_complete_easy_02.mp3",
        "tf2_engineer/sounds/Engineer_specialcompleted08.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="breakout-sfx-")
    v = SFX_VOLUME

    # BOUNCE -- short blip (high ping)
    s = (_triangle(880, 0.03, v * 0.5) +
         _triangle(1100, 0.03, v * 0.4))
    _write_wav(os.path.join(_sfx_dir, "bounce.wav"), s)
    _sfx_cache["bounce"] = os.path.join(_sfx_dir, "bounce.wav")

    # BREAK -- satisfying crunch (noise burst + low thud)
    s = (_square(220, 0.03, v * 0.6, 0.3) +
         _square(180, 0.03, v * 0.5, 0.4) +
         _square(140, 0.04, v * 0.4, 0.5) +
         _triangle(100, 0.05, v * 0.3))
    _write_wav(os.path.join(_sfx_dir, "break.wav"), s)
    _sfx_cache["break"] = os.path.join(_sfx_dir, "break.wav")

    # LOSE_LIFE -- sad descending tone
    s = (_square(440, 0.1, v * 0.5, 0.5) +
         _square(349, 0.1, v * 0.45, 0.5) +
         _square(294, 0.15, v * 0.4, 0.5) +
         _square(220, 0.25, v * 0.3, 0.5))
    _write_wav(os.path.join(_sfx_dir, "lose_life.wav"), s)
    _sfx_cache["lose_life"] = os.path.join(_sfx_dir, "lose_life.wav")

    # LAUNCH -- whoosh (rising sweep)
    s = (_triangle(300, 0.03, v * 0.3) +
         _triangle(450, 0.03, v * 0.4) +
         _triangle(650, 0.03, v * 0.45) +
         _triangle(900, 0.04, v * 0.5) +
         _triangle(1200, 0.04, v * 0.4))
    _write_wav(os.path.join(_sfx_dir, "launch.wav"), s)
    _sfx_cache["launch"] = os.path.join(_sfx_dir, "launch.wav")

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

    # LEVEL CLEAR -- ascending fanfare
    s = (_triangle(523, 0.06, v * 0.4) +
         _triangle(659, 0.06, v * 0.45) +
         _triangle(784, 0.06, v * 0.5) +
         _triangle(1047, 0.08, v * 0.55) +
         _triangle(1319, 0.15, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "level_clear.wav"), s)
    _sfx_cache["level_clear"] = os.path.join(_sfx_dir, "level_clear.wav")


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

def render_brick(color: str, size=SIZE) -> Image.Image:
    """Brick -- colored block with subtle border."""
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    # Bright highlight on top-left edge
    d.line([(4, 4), (91, 4)], fill="#ffffff", width=2)
    d.line([(4, 4), (4, 91)], fill="#ffffff", width=1)
    # Dark shadow on bottom-right edge
    d.line([(4, 91), (91, 91)], fill="#00000080", width=2)
    d.line([(91, 4), (91, 91)], fill="#00000080", width=1)
    return img


def render_ball(size=SIZE) -> Image.Image:
    """Ball -- bright yellow dot on dark background."""
    img = Image.new("RGB", size, COLOR_EMPTY)
    d = ImageDraw.Draw(img)
    # Bright ball circle
    d.ellipse([24, 24, 72, 72], fill=COLOR_BALL)
    # Small highlight
    d.ellipse([32, 30, 44, 42], fill="#fde68a")
    return img


def render_paddle(size=SIZE) -> Image.Image:
    """Paddle segment -- white bar on dark background."""
    img = Image.new("RGB", size, COLOR_EMPTY)
    d = ImageDraw.Draw(img)
    # Paddle bar (full width, centered vertically)
    d.rounded_rectangle([6, 28, 90, 68], radius=10, fill=COLOR_PADDLE)
    # Subtle highlight
    d.line([(12, 34), (84, 34)], fill="#f9fafb", width=2)
    return img


def render_paddle_ball(size=SIZE) -> Image.Image:
    """Paddle segment with ball sitting on top (before launch)."""
    img = Image.new("RGB", size, COLOR_EMPTY)
    d = ImageDraw.Draw(img)
    # Paddle bar
    d.rounded_rectangle([6, 48, 90, 82], radius=10, fill=COLOR_PADDLE)
    d.line([(12, 54), (84, 54)], fill="#f9fafb", width=2)
    # Ball on top of paddle
    d.ellipse([30, 8, 66, 44], fill=COLOR_BALL)
    d.ellipse([38, 14, 48, 24], fill="#fde68a")
    return img


def render_empty(size=SIZE) -> Image.Image:
    """Empty cell."""
    return Image.new("RGB", size, COLOR_EMPTY)


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, COLOR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 30), "BREAK", font=_font(17), fill="#fbbf24", anchor="mt")
    d.text((48, 52), "OUT", font=_font(17), fill="#f97316", anchor="mt")
    return img


def render_hud_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, COLOR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(score), font=_font(32), fill="#fbbf24", anchor="mt")
    return img


def render_hud_best(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, COLOR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(best), font=_font(28), fill="#34d399", anchor="mt")
    return img


def render_hud_lives(lives: int, size=SIZE) -> Image.Image:
    """Lives display -- hearts."""
    img = Image.new("RGB", size, COLOR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 16), "LIVES", font=_font(12), fill="#9ca3af", anchor="mt")
    hearts = "\u2764" * lives
    d.text((48, 52), hearts, font=_font(22), fill="#ef4444", anchor="mt")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, COLOR_HUD_BG)


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
    return img


def render_game_over(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white", anchor="mm",
           align="center")
    return img


def render_level_clear(level: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "LEVEL", font=_font(16), fill="white", anchor="mm")
    d.text((48, 55), str(level), font=_font(28), fill="#34d399", anchor="mm")
    return img


# -- game logic ------------------------------------------------------------

class BreakoutGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.best = scores.load_best("breakout")
        self.lives = LIVES_START
        self.level = 1
        self.running = False
        self.game_over = False
        self.ball_launched = False
        self.lock = threading.Lock()
        self.tick_timer = None

        # Ball state: (row, col), direction (row_dir, col_dir)
        self.ball_r = 0
        self.ball_c = 0
        self.ball_dr = 0   # -1 = up, +1 = down
        self.ball_dc = 0   # -1 = left, +1 = right

        # Paddle: center column (paddle spans center-1, center, center+1)
        self.paddle_center = 4  # 0-based col

        # Bricks: set of (row, col) positions that still exist
        self.bricks: set[tuple[int, int]] = set()
        # Brick color map for rendering
        self.brick_colors: dict[tuple[int, int], str] = {}

        self.tick_speed = TICK_START

        # Pre-render reusable images
        self.img_empty = render_empty()
        self.img_ball = render_ball()
        self.img_paddle = render_paddle()
        self.img_paddle_ball = render_paddle_ball()
        self.img_start = render_start()
        self.img_hud_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_game_over = render_game_over()

        # Pre-render brick images
        self.brick_images: dict[str, Image.Image] = {}
        for color in BRICK_COLORS:
            self.brick_images[color] = render_brick(color)


    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # -- idle / menu -------------------------------------------------------

    def show_idle(self):
        """Show start screen."""
        self.running = False
        self.game_over = False
        self._cancel_tick()
        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(0))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_lives(LIVES_START))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)
        # Game area
        for k in GAME_KEYS:
            if k == START_KEY:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, self.img_empty)

    # -- game start --------------------------------------------------------

    def start_game(self):
        """Initialize and start a new game."""
        with self.lock:
            self.score = 0
            self.lives = LIVES_START
            self.level = 1
            self.tick_speed = TICK_START
            self.running = True
            self.game_over = False
            self.ball_launched = False
            self.paddle_center = 4
            self._init_bricks()
            self._reset_ball()

        play_sfx("start")
        play_orc("start")

        self._draw_board()
        self._update_hud()

    def _init_bricks(self):
        """Set up bricks on row 0 (buttons 8-15)."""
        self.bricks.clear()
        self.brick_colors.clear()
        for c in range(COLS):
            self.bricks.add((0, c))
            self.brick_colors[(0, c)] = BRICK_COLORS[c]

    def _reset_ball(self):
        """Place ball on paddle, waiting for launch. Caller must hold lock."""
        self.ball_launched = False
        # Ball sits on the paddle center
        self.ball_r = 1  # row above paddle (row 1 = middle)
        self.ball_c = self.paddle_center
        self.ball_dr = -1  # will go up on launch
        self.ball_dc = random.choice([-1, 1])

    # -- tick (auto-move) --------------------------------------------------

    def _schedule_tick(self):
        """Schedule the next ball move."""
        self._cancel_tick()
        if not self.running or not self.ball_launched:
            return
        self.tick_timer = threading.Timer(self.tick_speed, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    def _cancel_tick(self):
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None

    def _tick(self):
        """One game tick: move ball."""
        if not self.running or not self.ball_launched:
            return
        with self.lock:
            self._move_ball()
        if self.running and self.ball_launched:
            self._schedule_tick()

    def _move_ball(self):
        """Move ball one step. Must hold lock."""
        old_r, old_c = self.ball_r, self.ball_c
        new_r = self.ball_r + self.ball_dr
        new_c = self.ball_c + self.ball_dc

        # -- wall bounces (left/right) --
        if new_c < 0:
            new_c = 0
            self.ball_dc = 1
            play_sfx("bounce")
        elif new_c >= COLS:
            new_c = COLS - 1
            self.ball_dc = -1
            play_sfx("bounce")

        # -- ceiling bounce (top) --
        if new_r < 0:
            new_r = 0
            self.ball_dr = 1
            play_sfx("bounce")

        # -- brick collision --
        if (new_r, new_c) in self.bricks:
            # Remember which brick we destroyed
            broken_r, broken_c = new_r, new_c
            self.bricks.discard((broken_r, broken_c))
            self.score += 10
            play_sfx("break")

            # Bounce: reverse row direction
            self.ball_dr = -self.ball_dr
            new_r = old_r  # stay in current row

            # If the column-adjacent cell also has a brick
            # (corner hit), also reverse col direction
            if (old_r, new_c) in self.bricks:
                self.ball_dc = -self.ball_dc
                new_c = old_c

            # Clear the destroyed brick visually
            self._draw_cell(broken_r, broken_c)

            # Check level clear
            if not self.bricks:
                self._level_clear()
                return

            self._update_hud()

        # -- paddle row (row 2) --
        if new_r >= 2:
            paddle_cols = self._paddle_cols()
            if new_c in paddle_cols:
                # Ball hit the paddle -- bounce up
                new_r = 1
                self.ball_dr = -1

                # Angle based on where ball hits paddle
                hit_offset = new_c - self.paddle_center
                if hit_offset < 0:
                    self.ball_dc = -1
                elif hit_offset > 0:
                    self.ball_dc = 1
                # else center hit: keep current dc

                play_sfx("bounce")
            else:
                # Ball missed paddle -- fell through
                self._lose_life()
                return

        # Clear old position
        self.set_key(rc_to_pos(old_r, old_c), self._cell_image(old_r, old_c))

        # Update ball position
        self.ball_r = new_r
        self.ball_c = new_c

        # Draw ball at new position
        self.set_key(rc_to_pos(new_r, new_c), self.img_ball)

    def _paddle_cols(self) -> list[int]:
        """Return list of columns the paddle occupies."""
        cols = []
        for offset in (-1, 0, 1):
            c = self.paddle_center + offset
            if 0 <= c < COLS:
                cols.append(c)
        return cols

    def _cell_image(self, row: int, col: int) -> Image.Image:
        """Get the correct image for a cell (excluding ball)."""
        if (row, col) in self.bricks:
            color = self.brick_colors.get((row, col), BRICK_COLORS[col % len(BRICK_COLORS)])
            return self.brick_images[color]
        if row == 2 and col in self._paddle_cols():
            return self.img_paddle
        return self.img_empty

    def _draw_cell(self, row: int, col: int):
        """Redraw a single cell."""
        self.set_key(rc_to_pos(row, col), self._cell_image(row, col))

    # -- life management ---------------------------------------------------

    def _lose_life(self):
        """Ball fell past paddle. Must hold lock."""
        self.lives -= 1
        self._cancel_tick()

        play_sfx("lose_life")

        if self.lives <= 0:
            self._game_over()
            return

        self._update_hud()
        self._reset_ball()
        self._draw_board()

    def _game_over(self):
        """Handle game over. Must hold lock."""
        self.running = False
        self.game_over = True
        self._cancel_tick()

        new_best = self.score > self.best
        if new_best:
            self.best = self.score
            scores.save_best("breakout", self.best)

        if new_best and self.score > 0:
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_sfx("lose_life")
            play_orc("gameover")

        self._show_game_over()

    def _show_game_over(self):
        """Display game over screen."""
        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_lives(0))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Flash remaining bricks red
        for r, c in list(self.bricks):
            img = Image.new("RGB", SIZE, "#dc2626")
            self.set_key(rc_to_pos(r, c), img)

        # Show game over + restart
        for k in GAME_KEYS:
            rc = pos_to_rc(k)
            if rc in self.bricks:
                continue  # keep red flash
            if k == START_KEY:
                self.set_key(k, self.img_start)
            elif k in (19, 20, 21):
                self.set_key(k, self.img_game_over if k != START_KEY else self.img_start)
            else:
                self.set_key(k, self.img_empty)

        # After a brief flash, show proper game over
        def _clear_flash():
            if self.game_over:
                for k in GAME_KEYS:
                    if k == START_KEY:
                        self.set_key(k, self.img_start)
                    elif k in (18, 19, 21):
                        self.set_key(k, self.img_game_over)
                    else:
                        self.set_key(k, self.img_empty)

        threading.Timer(0.5, _clear_flash).start()

    # -- level clear -------------------------------------------------------

    def _level_clear(self):
        """All bricks destroyed -- advance to next level. Must hold lock."""
        self.ball_launched = False
        self._cancel_tick()

        self.level += 1
        self.tick_speed = max(TICK_MIN, TICK_START - (self.level - 1) * TICK_SPEEDUP)

        play_sfx("level_clear")
        play_orc("level_clear")

        # Flash level clear message
        for k in GAME_KEYS:
            if k == START_KEY:
                self.set_key(k, render_level_clear(self.level))
            else:
                self.set_key(k, self.img_empty)

        self._update_hud()

        # After a pause, set up next level
        def _next_level():
            if self.running:
                with self.lock:
                    self._init_bricks()
                    self.paddle_center = 4
                    self._reset_ball()
                    self._draw_board()

        threading.Timer(1.5, _next_level).start()

    # -- drawing -----------------------------------------------------------

    def _draw_board(self):
        """Redraw the entire game area."""
        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                # Ball position
                if (self.ball_launched and r == self.ball_r and c == self.ball_c):
                    self.set_key(pos, self.img_ball)
                # Brick
                elif (r, c) in self.bricks:
                    color = self.brick_colors.get(
                        (r, c), BRICK_COLORS[c % len(BRICK_COLORS)])
                    self.set_key(pos, self.brick_images[color])
                # Paddle
                elif r == 2 and c in self._paddle_cols():
                    if (not self.ball_launched and c == self.paddle_center):
                        self.set_key(pos, self.img_paddle_ball)
                    else:
                        self.set_key(pos, self.img_paddle)
                # Empty
                else:
                    self.set_key(pos, self.img_empty)


    def _update_hud(self):
        """Update HUD displays."""
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_lives(self.lives))

    def _draw_paddle(self):
        """Redraw paddle row (row 2) efficiently."""
        for c in range(COLS):
            pos = rc_to_pos(2, c)
            if c in self._paddle_cols():
                if (not self.ball_launched and c == self.paddle_center):
                    self.set_key(pos, self.img_paddle_ball)
                else:
                    self.set_key(pos, self.img_paddle)
            elif self.ball_launched and self.ball_r == 2 and self.ball_c == c:
                self.set_key(pos, self.img_ball)
            else:
                self.set_key(pos, self.img_empty)

        # Also redraw ball row if ball is not on paddle row
        if not self.ball_launched:
            # Ball is visually on the paddle center, handled above
            # Clear row 1 where ball might have been
            for c in range(COLS):
                if (1, c) not in self.bricks:
                    self.set_key(rc_to_pos(1, c), self.img_empty)

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

        if key < 8 or key > 31:
            return

        tap_r, tap_c = pos_to_rc(key)

        if tap_r == 2:
            # Bottom row — move paddle here
            with self.lock:
                new_center = max(1, min(COLS - 2, tap_c))
                if new_center != self.paddle_center:
                    self.paddle_center = new_center
                    if not self.ball_launched:
                        self.ball_c = self.paddle_center
                    self._draw_paddle()
                elif not self.ball_launched:
                    # Tap on paddle position = launch
                    self.ball_launched = True
                    self.ball_r = 1
                    self.ball_c = self.paddle_center
                    self.ball_dr = -1
                    self.ball_dc = random.choice([-1, 1])
                    play_sfx("launch")
                    self._draw_board()
                    self._schedule_tick()
        else:
            # Upper rows — launch ball if not launched yet
            if not self.ball_launched:
                with self.lock:
                    self.ball_launched = True
                    self.ball_r = 1
                    self.ball_c = self.paddle_center
                    self.ball_dr = -1
                    self.ball_dc = random.choice([-1, 1])
                play_sfx("launch")
                self._draw_board()
                self._schedule_tick()


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
    print("BREAKOUT! Press the center button to start.")
    print("Controls: Tap bottom row to move paddle, tap to launch ball.")

    game = BreakoutGame(deck)
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
