"""Snake — Stream Deck mini-game.

Classic snake on a 3x8 grid. Tap any cell on the game field
to steer — snake turns toward the tapped cell.

Usage:
    uv run python scripts/snake_game.py
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

# Direction vectors: (d_row, d_col)
DIR_LEFT = (0, -1)
DIR_RIGHT = (0, 1)
DIR_UP = (-1, 0)
DIR_DOWN = (1, 0)

# Tick speed
TICK_START = 0.8   # seconds per move
TICK_MIN = 0.3     # fastest
TICK_SPEEDUP = 0.03  # seconds faster per food eaten

START_KEY = 20  # center-ish button for START

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    # Snake = Kerrigan (StarCraft — zerg queen, fits the slithering theme)
    "start": [
        "sc_kerrigan/sounds/ImReady.mp3",
        "sc_kerrigan/sounds/GotAJobToDo.mp3",
        "sc_kerrigan/sounds/KerriganReporting.mp3",
        "sc_kerrigan/sounds/WaitingOnYou.mp3",
    ],
    "milestone": [
        "sc_kerrigan/sounds/IGotcha.mp3",
        "sc_kerrigan/sounds/BeAPleasure.mp3",
        "sc_kerrigan/sounds/ThinkingSameThing.mp3",
    ],
    "death": [
        "sc_kerrigan/sounds/Death1.mp3",
        "sc_kerrigan/sounds/Death2.mp3",
        "sc_kerrigan/sounds/AnnoyingPeople.mp3",
    ],
    "newbest": [
        "sc_kerrigan/sounds/EasilyAmused.mp3",
        "sc_kerrigan/sounds/IReadYou.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="snake-sfx-")
    v = SFX_VOLUME

    # EAT -- happy rising blip (C5->E5->G5)
    s = (_square(523, 0.04, v * 0.5, 0.25) +
         _square(659, 0.04, v * 0.6, 0.25) +
         _triangle(784, 0.06, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "eat.wav"), s)
    _sfx_cache["eat"] = os.path.join(_sfx_dir, "eat.wav")

    # DIE -- sad descending (A4->F4->D4->A3)
    s = (_square(440, 0.1, v * 0.5, 0.5) +
         _square(349, 0.1, v * 0.45, 0.5) +
         _square(294, 0.12, v * 0.4, 0.5) +
         _square(220, 0.25, v * 0.35, 0.5))
    _write_wav(os.path.join(_sfx_dir, "die.wav"), s)
    _sfx_cache["die"] = os.path.join(_sfx_dir, "die.wav")

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

def render_snake_head(direction: tuple[int, int], size=SIZE) -> Image.Image:
    """Snake head -- bright green with eyes."""
    img = Image.new("RGB", size, "#22c55e")
    d = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    # Draw two black dots for eyes, positioned based on direction
    dr, dc = direction
    if dc == 1:       # moving right
        d.ellipse([60, 25, 72, 37], fill="black")
        d.ellipse([60, 55, 72, 67], fill="black")
    elif dc == -1:    # moving left
        d.ellipse([24, 25, 36, 37], fill="black")
        d.ellipse([24, 55, 36, 67], fill="black")
    elif dr == -1:    # moving up
        d.ellipse([25, 24, 37, 36], fill="black")
        d.ellipse([55, 24, 67, 36], fill="black")
    else:             # moving down
        d.ellipse([25, 60, 37, 72], fill="black")
        d.ellipse([55, 60, 67, 72], fill="black")
    return img


def render_snake_body(size=SIZE) -> Image.Image:
    """Snake body segment -- solid green."""
    img = Image.new("RGB", size, "#16a34a")
    d = ImageDraw.Draw(img)
    # Subtle inner border for segment look
    d.rectangle([4, 4, 91, 91], outline="#15803d", width=2)
    return img


def render_food(size=SIZE) -> Image.Image:
    """Food -- red circle with highlight."""
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    # Red apple
    d.ellipse([20, 20, 76, 76], fill="#ef4444")
    # Small highlight
    d.ellipse([30, 28, 42, 40], fill="#fca5a5")
    return img


def render_empty(size=SIZE) -> Image.Image:
    """Empty cell."""
    return Image.new("RGB", size, "#0f172a")


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "SNAKE", font=_font(18), fill="#22c55e", anchor="mt")
    # Small snake icon
    d.rectangle([30, 58, 66, 66], fill="#16a34a")
    d.ellipse([60, 55, 72, 69], fill="#22c55e")
    return img


def render_hud_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(score), font=_font(32), fill="#fbbf24", anchor="mt")
    return img


def render_hud_best(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(best), font=_font(28), fill="#34d399", anchor="mt")
    return img


def render_hud_speed(tick: float, size=SIZE) -> Image.Image:
    """Speed indicator bar."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 16), "SPEED", font=_font(12), fill="#9ca3af", anchor="mt")
    pct = 1.0 - (tick - TICK_MIN) / (TICK_START - TICK_MIN)
    pct = max(0.0, min(1.0, pct))
    bar_w = 60
    bar_h = 12
    x0 = (96 - bar_w) // 2
    y0 = 40
    d.rectangle([x0, y0, x0 + bar_w, y0 + bar_h], outline="#4b5563")
    fill_w = int(bar_w * pct)
    if fill_w > 0:
        clr = "#ef4444" if pct > 0.7 else "#fbbf24" if pct > 0.4 else "#22c55e"
        d.rectangle([x0, y0, x0 + fill_w, y0 + bar_h], fill=clr)
    d.text((48, 62), f"{tick:.2f}s", font=_font(16), fill="#e5e7eb", anchor="mt")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
    return img


def render_game_over(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white", anchor="mm", align="center")
    return img


# -- game logic ------------------------------------------------------------

class SnakeGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.best = scores.load_best("snake")
        self.running = False
        self.game_over = False
        self.lock = threading.Lock()
        self.tick_timer = None
        # Snake state
        self.snake: list[tuple[int, int]] = []  # list of (row, col), head is [0]
        self.direction: tuple[int, int] = DIR_RIGHT
        self.next_direction: tuple[int, int] = DIR_RIGHT
        self.food: tuple[int, int] | None = None
        self.tick_speed = TICK_START
        # Pre-render reusable images
        self.img_body = render_snake_body()
        self.img_food = render_food()
        self.img_empty = render_empty()
        self.img_start = render_start()
        self.img_hud_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_game_over = render_game_over()

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
        self.set_key(3, self.img_hud_empty)
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
            self.tick_speed = TICK_START
            self.running = True
            self.game_over = False
            # Snake starts at center of grid, length 3, moving right
            mid_row = ROWS // 2  # row 1 (middle)
            mid_col = COLS // 2  # col 4
            self.snake = [
                (mid_row, mid_col),      # head
                (mid_row, mid_col - 1),  # body
                (mid_row, mid_col - 2),  # tail
            ]
            self.direction = DIR_RIGHT
            self.next_direction = DIR_RIGHT
            self.food = None

        play_sfx("start")
        play_orc("start")

        # Clear game area
        for k in GAME_KEYS:
            self.set_key(k, self.img_empty)

        self._spawn_food()
        self._draw_board()
        self._update_hud()

        # Start game tick
        self._schedule_tick()

    # -- tick (auto-move) --------------------------------------------------

    def _schedule_tick(self):
        """Schedule the next auto-move."""
        self._cancel_tick()
        if not self.running:
            return
        self.tick_timer = threading.Timer(self.tick_speed, self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    def _cancel_tick(self):
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None

    def _tick(self):
        """One game tick: move snake forward."""
        if not self.running:
            return
        with self.lock:
            self._move_snake()
        if self.running:
            self._schedule_tick()

    def _move_snake(self):
        """Move snake one step in current direction. Must hold lock."""
        # Apply buffered direction
        self.direction = self.next_direction
        head_r, head_c = self.snake[0]
        dr, dc = self.direction
        new_r = head_r + dr
        new_c = head_c + dc

        # Wall collision
        if new_r < 0 or new_r >= ROWS or new_c < 0 or new_c >= COLS:
            self._die()
            return

        # Self collision
        if (new_r, new_c) in self.snake:
            self._die()
            return

        new_head = (new_r, new_c)
        ate_food = (self.food is not None and new_head == self.food)

        # Move: insert new head
        self.snake.insert(0, new_head)

        if ate_food:
            # Grow -- don't remove tail
            self.score += 1
            self.food = None
            # Speed up
            self.tick_speed = max(TICK_MIN, TICK_START - self.score * TICK_SPEEDUP)
            play_sfx("eat")
            # Milestone check
            if self.score % 5 == 0:
                play_orc("milestone")
            # Spawn new food
            self._spawn_food()
            self._update_hud()
        else:
            # Remove tail -- clear its button
            tail = self.snake.pop()
            self.set_key(rc_to_pos(tail[0], tail[1]), self.img_empty)

        # Draw new head and update old head to body
        self.set_key(rc_to_pos(new_head[0], new_head[1]),
                      render_snake_head(self.direction))
        if len(self.snake) > 1:
            old_head = self.snake[1]
            self.set_key(rc_to_pos(old_head[0], old_head[1]), self.img_body)

        # Redraw food (in case tail cleared it or it just spawned)
        if self.food is not None:
            self.set_key(rc_to_pos(self.food[0], self.food[1]), self.img_food)


    # -- food --------------------------------------------------------------

    def _spawn_food(self):
        """Place food on a random empty cell."""
        occupied = set(self.snake)
        empty = [(r, c) for r in range(ROWS) for c in range(COLS)
                 if (r, c) not in occupied]
        if empty:
            self.food = random.choice(empty)
        else:
            # Board is full -- you win (unlikely on 3x8)
            self.food = None

    # -- death -------------------------------------------------------------

    def _die(self):
        """Handle game over. Must hold lock."""
        self.running = False
        self.game_over = True
        self._cancel_tick()

        new_best = self.score > self.best
        if new_best:
            self.best = self.score
            scores.save_best("snake", self.best)

        if new_best and self.score > 0:
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_sfx("die")
            play_orc("death")

        self._show_game_over()

    def _show_game_over(self):
        """Display game over screen."""
        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, self.img_hud_empty)
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Flash snake red briefly, then show game over
        for r, c in self.snake:
            img = Image.new("RGB", SIZE, "#dc2626")
            self.set_key(rc_to_pos(r, c), img)

        # Show game over + restart button
        for k in GAME_KEYS:
            pos_rc = pos_to_rc(k)
            if pos_rc in self.snake:
                continue  # keep red flash
            if k == START_KEY:
                self.set_key(k, self.img_start)
            elif k in (19, 20, 21):
                self.set_key(k, self.img_game_over if k != START_KEY else self.img_start)
            else:
                self.set_key(k, self.img_empty)

        # After a brief flash, clear snake and show proper game over
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

    # -- drawing -----------------------------------------------------------

    def _draw_board(self):
        """Redraw the entire game area."""
        snake_set = set(self.snake)
        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                if (r, c) == self.snake[0]:
                    self.set_key(pos, render_snake_head(self.direction))
                elif (r, c) in snake_set:
                    self.set_key(pos, self.img_body)
                elif self.food and (r, c) == self.food:
                    self.set_key(pos, self.img_food)
                else:
                    self.set_key(pos, self.img_empty)

    def _update_hud(self):
        """Update HUD displays."""
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_speed(self.tick_speed))

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
                head_r, head_c = self.snake[0]
                dr = tap_r - head_r
                dc = tap_c - head_c

                if dr == 0 and dc == 0:
                    return  # tapped on head

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

                # Prevent 180-degree reversal
                nr, nc = new_dir
                if (cur_dr + nr, cur_dc + nc) != (0, 0):
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
    print("SNAKE! Press the center button to start.")
    print("Controls: Tap any cell on the grid to steer.")

    game = SnakeGame(deck)
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
