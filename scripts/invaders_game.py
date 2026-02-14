"""Space Invaders — Stream Deck mini-game.

Classic space invaders on a 3x8 grid. Shoot down alien waves!
Tap bottom row to move, tap upper rows to fire at that column.

Usage:
    uv run python scripts/invaders_game.py
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

# Tick speed
TICK_START = 1.2    # seconds per alien move
TICK_MIN = 0.35     # fastest
TICK_SPEEDUP = 0.05 # seconds faster per wave

START_KEY = 20  # center-ish button for START

# Alien grid: top 2 rows
ALIEN_ROWS = 2
ALIEN_COLS = 8

# Colors
CLR_ALIEN = "#a855f7"
CLR_PLAYER = "#06b6d4"
CLR_EMPTY = "#0f172a"
CLR_SHOT = "#fbbf24"
CLR_EXPLOSION = "#f97316"
CLR_HUD_BG = "#111827"

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "ra2_kirov/sounds/KirovReporting.mp3",
        "ra2_kirov/sounds/AirshipReady.mp3",
        "ra2_kirov/sounds/BombardiersToYourStations.mp3",
    ],
    "wave_clear": [
        "ra2_kirov/sounds/TargetAcquired.mp3",
        "ra2_kirov/sounds/BombingBaysReady.mp3",
        "ra2_kirov/sounds/ClosingOnTarget.mp3",
    ],
    "gameover": [
        "ra2_kirov/sounds/MaydayMayday.mp3",
        "ra2_kirov/sounds/WereLosingAltitude.mp3",
        "ra2_kirov/sounds/ShesGoingToBlow.mp3",
    ],
    "newbest": [
        "ra2_kirov/sounds/HeliumMixOptimal.mp3",
        "ra2_kirov/sounds/ManeuverPropsEngaged.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="invaders-sfx-")
    v = SFX_VOLUME

    # SHOOT -- quick rising blip (E5->A5)
    s = (_square(659, 0.02, v * 0.4, 0.25) +
         _triangle(880, 0.04, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "shoot.wav"), s)
    _sfx_cache["shoot"] = os.path.join(_sfx_dir, "shoot.wav")

    # EXPLODE -- noise burst (descending square wave)
    s = (_square(600, 0.03, v * 0.6, 0.3) +
         _square(400, 0.04, v * 0.5, 0.4) +
         _square(250, 0.05, v * 0.4, 0.5) +
         _square(150, 0.06, v * 0.3, 0.5))
    _write_wav(os.path.join(_sfx_dir, "explode.wav"), s)
    _sfx_cache["explode"] = os.path.join(_sfx_dir, "explode.wav")

    # WAVE -- fanfare (C5->E5->G5->C6 with triangle)
    s = (_triangle(523, 0.06, v * 0.4) +
         _triangle(659, 0.06, v * 0.45) +
         _triangle(784, 0.06, v * 0.5) +
         _triangle(1047, 0.18, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "wave.wav"), s)
    _sfx_cache["wave"] = os.path.join(_sfx_dir, "wave.wav")

    # DIE -- sad descent (A4->F4->D4->A3)
    s = (_square(440, 0.1, v * 0.5, 0.5) +
         _square(349, 0.1, v * 0.45, 0.5) +
         _square(294, 0.12, v * 0.4, 0.5) +
         _square(220, 0.25, v * 0.35, 0.5))
    _write_wav(os.path.join(_sfx_dir, "die.wav"), s)
    _sfx_cache["die"] = os.path.join(_sfx_dir, "die.wav")

    # NEW BEST -- victory jingle (C5->E5->G5->C6 extended)
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

def render_alien(size=SIZE) -> Image.Image:
    """Alien -- purple with pixel-art face (eyes + antennae)."""
    img = Image.new("RGB", size, CLR_ALIEN)
    d = ImageDraw.Draw(img)
    # Antennae -- two thin lines from top
    d.line([30, 8, 38, 24], fill="#e9d5ff", width=3)
    d.line([66, 8, 58, 24], fill="#e9d5ff", width=3)
    # Antenna tips -- small circles
    d.ellipse([26, 4, 34, 12], fill="#e9d5ff")
    d.ellipse([62, 4, 70, 12], fill="#e9d5ff")
    # Eyes -- two white squares with black pupils
    d.rectangle([28, 34, 44, 50], fill="white")
    d.rectangle([52, 34, 68, 50], fill="white")
    # Pupils
    d.rectangle([34, 38, 42, 48], fill="black")
    d.rectangle([56, 38, 64, 48], fill="black")
    # Mouth -- small jagged line
    d.line([32, 62, 40, 58, 48, 64, 56, 58, 64, 62], fill="#1e1b4b", width=2)
    return img


def render_player(size=SIZE) -> Image.Image:
    """Player ship -- cyan triangle/ship shape."""
    img = Image.new("RGB", size, CLR_EMPTY)
    d = ImageDraw.Draw(img)
    # Ship body -- triangle pointing up
    d.polygon([
        (48, 12),   # top point
        (18, 80),   # bottom left
        (78, 80),   # bottom right
    ], fill=CLR_PLAYER)
    # Cockpit window
    d.ellipse([38, 32, 58, 48], fill="#0e7490")
    # Engine glow at bottom
    d.rectangle([34, 74, 62, 84], fill="#67e8f9")
    d.rectangle([40, 80, 56, 90], fill="#a5f3fc")
    return img


def render_empty(size=SIZE) -> Image.Image:
    """Empty cell."""
    return Image.new("RGB", size, CLR_EMPTY)


def render_shot(size=SIZE) -> Image.Image:
    """Shot -- yellow dot on dark background."""
    img = Image.new("RGB", size, CLR_EMPTY)
    d = ImageDraw.Draw(img)
    # Bright yellow bolt
    d.ellipse([38, 30, 58, 50], fill=CLR_SHOT)
    d.ellipse([42, 34, 54, 46], fill="#fef08a")
    # Trail
    d.rectangle([44, 50, 52, 66], fill="#fbbf24")
    d.rectangle([46, 66, 50, 76], fill="#f59e0b")
    return img


def render_explosion(size=SIZE) -> Image.Image:
    """Explosion -- orange burst."""
    img = Image.new("RGB", size, CLR_EXPLOSION)
    d = ImageDraw.Draw(img)
    # Starburst pattern
    cx, cy = 48, 48
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x2 = cx + int(38 * math.cos(rad))
        y2 = cy + int(38 * math.sin(rad))
        d.line([cx, cy, x2, y2], fill="#fef08a", width=4)
    # Center glow
    d.ellipse([28, 28, 68, 68], fill="#fdba74")
    d.ellipse([36, 36, 60, 60], fill="#fef08a")
    d.ellipse([42, 42, 54, 54], fill="white")
    return img


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 22), "SPACE", font=_font(14), fill="#a855f7", anchor="mt")
    d.text((48, 38), "INVADERS", font=_font(13), fill="#c084fc", anchor="mt")
    # Small alien icon
    d.ellipse([36, 60, 44, 68], fill="#a855f7")
    d.ellipse([52, 60, 60, 68], fill="#a855f7")
    d.rectangle([40, 66, 56, 74], fill="#a855f7")
    return img


def render_hud_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(score), font=_font(32), fill="#fbbf24", anchor="mt")
    return img


def render_hud_best(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(best), font=_font(28), fill="#34d399", anchor="mt")
    return img


def render_hud_wave(wave_num: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, CLR_HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "WAVE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(wave_num), font=_font(28), fill="#c084fc", anchor="mt")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, CLR_HUD_BG)


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


# -- game logic ------------------------------------------------------------

class InvadersGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.best = scores.load_best("invaders")
        self.wave_num = 0
        self.running = False
        self.game_over = False
        self.lock = threading.Lock()
        self.tick_timer = None
        # Player state
        self.player_col = COLS // 2  # player column on bottom row (row 2)
        # Alien state: set of (row, col) positions
        self.aliens: set[tuple[int, int]] = set()
        self.alien_dir = 1   # +1 = moving right, -1 = moving left
        # Shots: list of (row, col)
        self.shots: list[tuple[int, int]] = []
        # Explosions: dict of (row, col) -> expire_time
        self.explosions: dict[tuple[int, int], float] = {}
        self.tick_speed = TICK_START
        # Pre-render reusable images
        self.img_alien = render_alien()
        self.img_player = render_player()
        self.img_empty = render_empty()
        self.img_shot = render_shot()
        self.img_explosion = render_explosion()
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
        self.set_key(3, render_hud_wave(0))
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
            self.wave_num = 0
            self.tick_speed = TICK_START
            self.running = True
            self.game_over = False
            self.player_col = COLS // 2
            self.aliens = set()
            self.alien_dir = 1
            self.shots = []
            self.explosions = {}

        play_sfx("wave")
        play_orc("start")

        # Clear game area
        for k in GAME_KEYS:
            self.set_key(k, self.img_empty)

        self._spawn_wave()
        self._draw_board()
        self._update_hud()

        # Start game tick
        self._schedule_tick()

    # -- wave spawn --------------------------------------------------------

    def _spawn_wave(self):
        """Spawn a new wave of aliens in top 2 rows."""
        self.wave_num += 1
        self.aliens = set()
        self.alien_dir = 1
        self.shots = []
        self.explosions = {}
        for r in range(ALIEN_ROWS):
            for c in range(ALIEN_COLS):
                self.aliens.add((r, c))
        # Speed up each wave
        self.tick_speed = max(TICK_MIN,
                              TICK_START - (self.wave_num - 1) * TICK_SPEEDUP)

    # -- tick (auto-move aliens) -------------------------------------------

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
        """One game tick: move aliens."""
        if not self.running:
            return
        with self.lock:
            self._move_aliens()
        if self.running:
            self._schedule_tick()

    def _move_aliens(self):
        """Move all aliens one step. Classic invader pattern:
        shift one column in current direction; when any alien hits
        the edge, shift all down one row and reverse direction.
        Must hold lock."""
        if not self.aliens:
            return

        # Clear expired explosions
        now = time.monotonic()
        expired = [k for k, v in self.explosions.items() if now >= v]
        for k in expired:
            del self.explosions[k]
            self.set_key(rc_to_pos(k[0], k[1]), self.img_empty)

        # Check if any alien would go out of bounds horizontally
        need_drop = False
        for r, c in self.aliens:
            new_c = c + self.alien_dir
            if new_c < 0 or new_c >= COLS:
                need_drop = True
                break

        if need_drop:
            # Move all aliens down one row, reverse direction
            new_aliens = set()
            for r, c in self.aliens:
                new_aliens.add((r + 1, c))
            self.alien_dir *= -1
            # Clear old alien positions
            for r, c in self.aliens:
                if (r, c) not in new_aliens:
                    self.set_key(rc_to_pos(r, c), self.img_empty)
            self.aliens = new_aliens
        else:
            # Shift all aliens horizontally
            new_aliens = set()
            for r, c in self.aliens:
                new_aliens.add((r, c + self.alien_dir))
            # Clear old positions that are no longer occupied
            for r, c in self.aliens:
                if (r, c) not in new_aliens:
                    self.set_key(rc_to_pos(r, c), self.img_empty)
            self.aliens = new_aliens

        # Check if any alien reached the bottom row (row 2 = player row)
        player_row = ROWS - 1
        for r, c in self.aliens:
            if r >= player_row:
                self._die()
                return

        # Draw aliens in new positions
        for r, c in self.aliens:
            self.set_key(rc_to_pos(r, c), self.img_alien)

        # Redraw player (might have been overwritten)
        self.set_key(rc_to_pos(player_row, self.player_col), self.img_player)

        # Redraw any active shots
        for sr, sc in self.shots:
            self.set_key(rc_to_pos(sr, sc), self.img_shot)

        # Speed adapts to remaining alien count
        remaining = len(self.aliens)
        total = ALIEN_ROWS * ALIEN_COLS
        if remaining > 0 and remaining < total:
            ratio = remaining / total
            speed_boost = (1.0 - ratio) * 0.4
            self.tick_speed = max(TICK_MIN,
                                  TICK_START - (self.wave_num - 1) * TICK_SPEEDUP
                                  - speed_boost)

    # -- firing ------------------------------------------------------------

    def _fire(self):
        """Player fires a shot upward. Instant column scan: destroys
        the lowest alien in the player's column."""
        with self.lock:
            if not self.running:
                return
            col = self.player_col
            player_row = ROWS - 1

            # Find the lowest alien in this column
            target = None
            for r in range(player_row - 1, -1, -1):
                if (r, col) in self.aliens:
                    target = (r, col)
                    break

            play_sfx("shoot")

            if target is None:
                # No alien in column -- show shot briefly at row above player
                if player_row - 1 >= 0:
                    shot_pos = (player_row - 1, col)
                    self.set_key(rc_to_pos(shot_pos[0], shot_pos[1]),
                                 self.img_shot)
                    # Clear after brief flash
                    def _clear_miss():
                        with self.lock:
                            if self.running and shot_pos not in self.aliens:
                                self.set_key(rc_to_pos(shot_pos[0], shot_pos[1]),
                                             self.img_empty)
                    threading.Timer(0.15, _clear_miss).start()
                return

            # Hit! Remove alien
            self.aliens.discard(target)
            self.score += 1

            # Show explosion
            self.set_key(rc_to_pos(target[0], target[1]), self.img_explosion)
            self.explosions[target] = time.monotonic() + 0.3
            play_sfx("explode")

            # Clear explosion after delay
            def _clear_explosion(pos=target):
                with self.lock:
                    if pos in self.explosions:
                        del self.explosions[pos]
                    if self.running and pos not in self.aliens:
                        self.set_key(rc_to_pos(pos[0], pos[1]), self.img_empty)
            threading.Timer(0.3, _clear_explosion).start()

            self._update_hud()

            # Check wave clear
            if not self.aliens:
                play_sfx("wave")
                play_orc("wave_clear")
                # Brief pause then new wave
                def _next_wave():
                    with self.lock:
                        if not self.running:
                            return
                        self._spawn_wave()
                        self._draw_board()
                        self._update_hud()
                threading.Timer(0.8, _next_wave).start()

    # -- death -------------------------------------------------------------

    def _die(self):
        """Handle game over. Must hold lock."""
        self.running = False
        self.game_over = True
        self._cancel_tick()

        new_best = self.score > self.best
        if new_best:
            self.best = self.score
            scores.save_best("invaders", self.best)

        if new_best and self.score > 0:
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_sfx("die")
            play_orc("gameover")

        self._show_game_over()

    def _show_game_over(self):
        """Display game over screen."""
        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_wave(self.wave_num))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Flash aliens red briefly
        player_row = ROWS - 1
        for r, c in self.aliens:
            if 0 <= r < ROWS and 0 <= c < COLS:
                img = Image.new("RGB", SIZE, "#dc2626")
                self.set_key(rc_to_pos(r, c), img)

        # Show game over + restart button
        for k in GAME_KEYS:
            rc = pos_to_rc(k)
            if rc in self.aliens:
                continue  # keep red flash
            if k == START_KEY:
                self.set_key(k, self.img_start)
            elif k in (18, 19, 21):
                self.set_key(k, self.img_game_over)
            else:
                self.set_key(k, self.img_empty)

        # After a brief flash, clear and show proper game over
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
        player_row = ROWS - 1
        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                if (r, c) in self.aliens:
                    self.set_key(pos, self.img_alien)
                elif r == player_row and c == self.player_col:
                    self.set_key(pos, self.img_player)
                elif (r, c) in self.explosions:
                    self.set_key(pos, self.img_explosion)
                else:
                    self.set_key(pos, self.img_empty)

    def _update_hud(self):
        """Update HUD displays."""
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_wave(self.wave_num))

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
        player_row = ROWS - 1

        if tap_r == player_row:
            # Bottom row tap — move player there
            with self.lock:
                if not self.running:
                    return
                old_col = self.player_col
                if tap_c == old_col:
                    # Already here — fire instead
                    pass
                else:
                    if (player_row, tap_c) in self.aliens:
                        self._die()
                        return
                    self.player_col = tap_c
                    self.set_key(rc_to_pos(player_row, old_col), self.img_empty)
                    self.set_key(rc_to_pos(player_row, tap_c), self.img_player)
                    return
            # Fall through to fire if tapped on self
            self._fire_at(tap_c)
        else:
            # Upper rows tap — fire at that column
            self._fire_at(tap_c)

    def _fire_at(self, col: int):
        """Fire at a specific column (instant scan)."""
        with self.lock:
            if not self.running:
                return
            # Move player to that column first
            player_row = ROWS - 1
            old_col = self.player_col
            if old_col != col:
                if (player_row, col) in self.aliens:
                    self._die()
                    return
                self.player_col = col
                self.set_key(rc_to_pos(player_row, old_col), self.img_empty)
                self.set_key(rc_to_pos(player_row, col), self.img_player)
        self._fire()


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
    print("SPACE INVADERS! Press the center button to start.")
    print("Controls: Tap bottom row to move, tap upper rows to fire.")

    game = InvadersGame(deck)
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
