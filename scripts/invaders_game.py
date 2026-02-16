"""Space Invaders — Stream Deck mini-game (horizontal full-field).

Full 4x8 grid. Player on left column, aliens march from right.
Tap any row to move player there + fire right.

Usage:
    uv run python scripts/invaders_game.py
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

# -- config ---------------------------------------------------------------
ROWS = 4
COLS = 8
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

TICK_START = 1.2
TICK_MIN = 0.30
TICK_SPEEDUP = 0.05

PLAYER_COL = 0  # player is on the leftmost column
START_KEY = 16   # middle-left (row 2, col 0)

# Aliens spawn in rightmost 3 columns, all 4 rows
ALIEN_SPAWN_COLS = range(5, 8)  # columns 5, 6, 7
ALIEN_SPAWN_ROWS = range(0, 4)  # all rows

# Colors
CLR_ALIEN = "#a855f7"
CLR_PLAYER = "#06b6d4"
CLR_EMPTY = "#0f172a"
CLR_SHOT = "#fbbf24"
CLR_EXPLOSION = "#f97316"

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
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="invaders-sfx-")
    v = SFX_VOLUME

    s = (_square(659, 0.02, v * 0.4, 0.25) + _triangle(880, 0.04, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "shoot.wav"), s)
    _sfx_cache["shoot"] = os.path.join(_sfx_dir, "shoot.wav")

    s = (_square(600, 0.03, v * 0.6, 0.3) + _square(400, 0.04, v * 0.5, 0.4) +
         _square(250, 0.05, v * 0.4, 0.5) + _square(150, 0.06, v * 0.3, 0.5))
    _write_wav(os.path.join(_sfx_dir, "explode.wav"), s)
    _sfx_cache["explode"] = os.path.join(_sfx_dir, "explode.wav")

    s = (_triangle(523, 0.06, v * 0.4) + _triangle(659, 0.06, v * 0.45) +
         _triangle(784, 0.06, v * 0.5) + _triangle(1047, 0.18, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "wave.wav"), s)
    _sfx_cache["wave"] = os.path.join(_sfx_dir, "wave.wav")

    s = (_square(440, 0.1, v * 0.5, 0.5) + _square(349, 0.1, v * 0.45, 0.5) +
         _square(294, 0.12, v * 0.4, 0.5) + _square(220, 0.25, v * 0.35, 0.5))
    _write_wav(os.path.join(_sfx_dir, "die.wav"), s)
    _sfx_cache["die"] = os.path.join(_sfx_dir, "die.wav")

    s = (_triangle(523, 0.08, v * 0.5) + _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) + _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "newbest.wav"), s)
    _sfx_cache["newbest"] = os.path.join(_sfx_dir, "newbest.wav")


def play_sfx(name: str):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos: int) -> tuple[int, int]:
    return pos // COLS, pos % COLS


def rc_to_pos(row: int, col: int) -> int:
    return row * COLS + col


# -- renderers -------------------------------------------------------------

def render_alien(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, CLR_ALIEN)
    d = ImageDraw.Draw(img)
    d.line([30, 8, 38, 24], fill="#e9d5ff", width=3)
    d.line([66, 8, 58, 24], fill="#e9d5ff", width=3)
    d.ellipse([26, 4, 34, 12], fill="#e9d5ff")
    d.ellipse([62, 4, 70, 12], fill="#e9d5ff")
    d.rectangle([28, 34, 44, 50], fill="white")
    d.rectangle([52, 34, 68, 50], fill="white")
    d.rectangle([34, 38, 42, 48], fill="black")
    d.rectangle([56, 38, 64, 48], fill="black")
    d.line([32, 62, 40, 58, 48, 64, 56, 58, 64, 62], fill="#1e1b4b", width=2)
    return img


def render_player(score: int = 0, wave_num: int = 0, size=SIZE) -> Image.Image:
    """Player ship pointing RIGHT with score overlay."""
    img = Image.new("RGB", size, CLR_EMPTY)
    d = ImageDraw.Draw(img)
    # Ship body — triangle pointing right
    d.polygon([(82, 48), (14, 18), (14, 78)], fill=CLR_PLAYER)
    # Cockpit
    d.ellipse([30, 38, 50, 58], fill="#0e7490")
    # Engine glow at left
    d.rectangle([8, 34, 18, 62], fill="#67e8f9")
    d.rectangle([4, 40, 12, 56], fill="#a5f3fc")
    # Score overlay (top, small)
    if score > 0:
        d.text((48, 6), str(score), font=_font(13), fill="#fbbf24", anchor="mt")
    # Wave overlay (bottom, tiny)
    if wave_num > 0:
        d.text((48, 88), f"W{wave_num}", font=_font(10), fill="#c084fc", anchor="mb")
    return img


def render_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, CLR_EMPTY)


def render_shot(size=SIZE) -> Image.Image:
    """Shot — horizontal yellow bolt going right."""
    img = Image.new("RGB", size, CLR_EMPTY)
    d = ImageDraw.Draw(img)
    d.ellipse([50, 38, 70, 58], fill=CLR_SHOT)
    d.ellipse([54, 42, 66, 54], fill="#fef08a")
    # Trail going left
    d.rectangle([30, 44, 50, 52], fill="#fbbf24")
    d.rectangle([18, 46, 30, 50], fill="#f59e0b")
    return img


def render_explosion(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, CLR_EXPLOSION)
    d = ImageDraw.Draw(img)
    cx, cy = 48, 48
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x2 = cx + int(38 * math.cos(rad))
        y2 = cy + int(38 * math.sin(rad))
        d.line([cx, cy, x2, y2], fill="#fef08a", width=4)
    d.ellipse([28, 28, 68, 68], fill="#fdba74")
    d.ellipse([36, 36, 60, 60], fill="#fef08a")
    d.ellipse([42, 42, 54, 54], fill="white")
    return img


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


def render_idle_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#1e1b4b")
    d = ImageDraw.Draw(img)
    d.text((48, 22), "SPACE", font=_font(16), fill="#a855f7", anchor="mt")
    d.text((48, 42), "INVADERS", font=_font(14), fill="#c084fc", anchor="mt")
    d.ellipse([36, 64, 44, 72], fill="#a855f7")
    d.ellipse([52, 64, 60, 72], fill="#a855f7")
    d.rectangle([40, 70, 56, 78], fill="#a855f7")
    return img


def render_idle_info(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#1e1b4b")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(12), fill="#9ca3af", anchor="mt")
    d.text((48, 40), str(best), font=_font(24), fill="#34d399", anchor="mt")
    d.text((48, 74), ">>>>>>", font=_font(10), fill="#60a5fa", anchor="mt")
    return img


def render_score_tile(score: int, wave_num: int, best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 10), f"W{wave_num}", font=_font(12), fill="#c084fc", anchor="mt")
    d.text((48, 32), str(score), font=_font(28), fill="#fbbf24", anchor="mt")
    d.text((48, 68), f"BEST {best}", font=_font(11), fill="#34d399", anchor="mt")
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
        self.player_row = 1  # player row (0-3), starts mid
        # Alien state: set of (row, col) positions
        self.aliens: set[tuple[int, int]] = set()
        self.alien_dir = 1  # +1 = moving down, -1 = moving up
        self.explosions: dict[tuple[int, int], float] = {}
        self.tick_speed = TICK_START
        # Pre-render
        self.img_alien = render_alien()
        self.img_empty = render_empty()
        self.img_shot = render_shot()
        self.img_explosion = render_explosion()
        self.img_start = render_start()
        self.img_game_over = render_game_over()

    def set_key(self, pos: int, img: Image.Image):
        if pos < 0 or pos > 31:
            return
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # -- idle --------------------------------------------------------------

    def show_idle(self):
        self.running = False
        self.game_over = False
        self._cancel_tick()

        self.set_key(0, render_idle_title())
        self.set_key(8, render_idle_info(self.best))
        for k in range(32):
            if k in (0, 8):
                continue
            if k == START_KEY:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, self.img_empty)

    # -- game start --------------------------------------------------------

    def start_game(self):
        with self.lock:
            self.score = 0
            self.wave_num = 0
            self.tick_speed = TICK_START
            self.running = True
            self.game_over = False
            self.player_row = 1
            self.aliens = set()
            self.alien_dir = 1
            self.explosions = {}

        play_sfx("wave")
        play_orc("start")

        for k in range(32):
            self.set_key(k, self.img_empty)

        self._spawn_wave()
        self._draw_board()
        self._schedule_tick()

    # -- wave spawn --------------------------------------------------------

    def _spawn_wave(self):
        """Spawn aliens in rightmost 3 columns, all 4 rows (12 aliens)."""
        self.wave_num += 1
        self.aliens = set()
        self.alien_dir = 1  # start moving down
        self.explosions = {}
        for r in ALIEN_SPAWN_ROWS:
            for c in ALIEN_SPAWN_COLS:
                self.aliens.add((r, c))
        self.tick_speed = max(TICK_MIN,
                              TICK_START - (self.wave_num - 1) * TICK_SPEEDUP)

    # -- tick (auto-move aliens) -------------------------------------------

    def _schedule_tick(self):
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
        if not self.running:
            return
        with self.lock:
            self._move_aliens()
        if self.running:
            self._schedule_tick()

    def _move_aliens(self):
        """Horizontal invaders: aliens move up/down, shift LEFT on edge hit."""
        if not self.aliens:
            return

        # Clear expired explosions
        now = time.monotonic()
        expired = [k for k, v in self.explosions.items() if now >= v]
        for k in expired:
            del self.explosions[k]
            self.set_key(rc_to_pos(k[0], k[1]), self.img_empty)

        # Check if any alien would go out of bounds vertically
        need_shift = False
        for r, c in self.aliens:
            new_r = r + self.alien_dir
            if new_r < 0 or new_r >= ROWS:
                need_shift = True
                break

        if need_shift:
            # Shift all aliens LEFT one column, reverse vertical direction
            new_aliens = set()
            for r, c in self.aliens:
                new_aliens.add((r, c - 1))
            self.alien_dir *= -1
            # Clear old positions
            for r, c in self.aliens:
                if (r, c) not in new_aliens:
                    self.set_key(rc_to_pos(r, c), self.img_empty)
            self.aliens = new_aliens
        else:
            # Move all aliens vertically
            new_aliens = set()
            for r, c in self.aliens:
                new_aliens.add((r + self.alien_dir, c))
            for r, c in self.aliens:
                if (r, c) not in new_aliens:
                    self.set_key(rc_to_pos(r, c), self.img_empty)
            self.aliens = new_aliens

        # Check if any alien reached the player column
        for r, c in self.aliens:
            if c <= PLAYER_COL:
                self._die()
                return

        # Draw aliens
        for r, c in self.aliens:
            if 0 <= r < ROWS and 0 <= c < COLS:
                self.set_key(rc_to_pos(r, c), self.img_alien)

        # Redraw player
        self._draw_player()

        # Speed adapts to remaining alien count
        remaining = len(self.aliens)
        total = len(ALIEN_SPAWN_ROWS) * len(ALIEN_SPAWN_COLS)
        if 0 < remaining < total:
            ratio = remaining / total
            speed_boost = (1.0 - ratio) * 0.4
            self.tick_speed = max(TICK_MIN,
                                  TICK_START - (self.wave_num - 1) * TICK_SPEEDUP
                                  - speed_boost)

    # -- drawing -----------------------------------------------------------

    def _draw_player(self):
        img = render_player(self.score, self.wave_num)
        self.set_key(rc_to_pos(self.player_row, PLAYER_COL), img)

    def _draw_board(self):
        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                if (r, c) in self.aliens:
                    self.set_key(pos, self.img_alien)
                elif c == PLAYER_COL and r == self.player_row:
                    pass  # drawn separately
                elif (r, c) in self.explosions:
                    self.set_key(pos, self.img_explosion)
                else:
                    self.set_key(pos, self.img_empty)
        self._draw_player()

    # -- firing (horizontal — shoot RIGHT) ---------------------------------

    def _fire(self):
        with self.lock:
            if not self.running:
                return
            row = self.player_row

            # Find nearest alien to the right in this row
            target = None
            for c in range(PLAYER_COL + 1, COLS):
                if (row, c) in self.aliens:
                    target = (row, c)
                    break

            play_sfx("shoot")

            if target is None:
                # Miss — flash shot to the right of player
                shot_c = PLAYER_COL + 1
                if shot_c < COLS and (row, shot_c) not in self.aliens:
                    self.set_key(rc_to_pos(row, shot_c), self.img_shot)

                    def _clear_miss():
                        with self.lock:
                            if self.running and (row, shot_c) not in self.aliens:
                                self.set_key(rc_to_pos(row, shot_c), self.img_empty)
                    threading.Timer(0.15, _clear_miss).start()
                return

            # Hit!
            self.aliens.discard(target)
            self.score += 1

            self.set_key(rc_to_pos(target[0], target[1]), self.img_explosion)
            self.explosions[target] = time.monotonic() + 0.3
            play_sfx("explode")

            self._draw_player()

            def _clear_explosion(pos=target):
                with self.lock:
                    if pos in self.explosions:
                        del self.explosions[pos]
                    if self.running and pos not in self.aliens:
                        self.set_key(rc_to_pos(pos[0], pos[1]), self.img_empty)
            threading.Timer(0.3, _clear_explosion).start()

            # Wave clear?
            if not self.aliens:
                play_sfx("wave")
                play_orc("wave_clear")

                def _next_wave():
                    with self.lock:
                        if not self.running:
                            return
                        self._spawn_wave()
                        self._draw_board()
                threading.Timer(0.8, _next_wave).start()

    # -- death -------------------------------------------------------------

    def _die(self):
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
        # Flash aliens red
        for r, c in self.aliens:
            if 0 <= r < ROWS and 0 <= c < COLS:
                img = Image.new("RGB", SIZE, "#dc2626")
                self.set_key(rc_to_pos(r, c), img)

        # Score + restart
        for k in range(32):
            rc = pos_to_rc(k)
            if rc in self.aliens:
                continue
            if k == START_KEY:
                self.set_key(k, self.img_start)
            elif k in (17, 18):
                self.set_key(k, self.img_game_over)
            elif k in (9, 10):
                self.set_key(k, render_score_tile(self.score, self.wave_num, self.best))
            else:
                self.set_key(k, self.img_empty)

        def _clear_flash():
            if self.game_over:
                for k in range(32):
                    if k == START_KEY:
                        self.set_key(k, self.img_start)
                    elif k in (17, 18):
                        self.set_key(k, self.img_game_over)
                    elif k in (9, 10):
                        self.set_key(k, render_score_tile(self.score, self.wave_num, self.best))
                    else:
                        self.set_key(k, self.img_empty)
        threading.Timer(0.5, _clear_flash).start()

    # -- input -------------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        if key == START_KEY and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        if key < 0 or key > 31:
            return

        tap_r, tap_c = pos_to_rc(key)

        if tap_c == PLAYER_COL:
            # Left column — move player to this row
            with self.lock:
                if not self.running:
                    return
                old_row = self.player_row
                if tap_r == old_row:
                    pass  # same row — fire
                else:
                    if (tap_r, PLAYER_COL) in self.aliens:
                        self._die()
                        return
                    self.player_row = tap_r
                    self.set_key(rc_to_pos(old_row, PLAYER_COL), self.img_empty)
                    self._draw_player()
                    return
            # Tapped same row — fire
            self._fire_at(tap_r)
        else:
            # Any other column — move to that row + fire
            self._fire_at(tap_r)

    def _fire_at(self, row: int):
        with self.lock:
            if not self.running:
                return
            old_row = self.player_row
            if old_row != row:
                if (row, PLAYER_COL) in self.aliens:
                    self._die()
                    return
                self.player_row = row
                self.set_key(rc_to_pos(old_row, PLAYER_COL), self.img_empty)
                self._draw_player()
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

    try:
        _generate_sfx()
        print("Sound effects: ON")
    except Exception:
        print("Sound effects: OFF (generation failed)")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("SPACE INVADERS (horizontal)! Press start to play.")
    print("Controls: Tap any key to move to that row + fire right.")

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
