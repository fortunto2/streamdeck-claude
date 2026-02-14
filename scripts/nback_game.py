"""N-Back — Stream Deck cognitive test game.

Classic working-memory IQ test. A cell lights up on the 3x8 grid,
then the next one. Press MATCH if the current cell matches the cell
from N steps ago. Start at 1-back, advance after 8 consecutive correct.

Usage:
    uv run python scripts/nback_game.py
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

# -- config ----------------------------------------------------------------
GAME_KEYS = list(range(8, 32))  # rows 2-4 = game area (3x8 grid)
HUD_KEYS = list(range(0, 8))    # row 1 = HUD
ROWS = 3
COLS = 8
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
START_KEY = 20
MATCH_KEY = 3  # HUD key for the MATCH button

# Game tuning
CELL_DISPLAY_TIME = 1.0   # seconds cell stays lit
RESPONSE_WINDOW = 2.0     # seconds player has to respond after cell lights
STEP_INTERVAL = 2.5       # seconds between steps
MATCH_PROBABILITY = 0.30  # 30% chance a step is a match
CORRECT_TO_LEVEL_UP = 8   # consecutive correct to advance N
STARTING_LIVES = 3
STARTING_N = 1

# Bright cell colors
CELL_COLORS = ["#fbbf24", "#ef4444", "#22c55e", "#3b82f6", "#a855f7"]
EMPTY_COLOR = "#0f172a"
HUD_BG = "#111827"

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "glados/sounds/Hello.mp3",
        "glados/sounds/CanYouHearMe.mp3",
    ],
    "correct": [
        "glados/sounds/Excellent.mp3",
        "glados/sounds/Fantastic.mp3",
    ],
    "gameover": [
        "glados/sounds/WompWomp.mp3",
        "glados/sounds/Unbelievable.mp3",
    ],
    "level_up": [
        "glados/sounds/Congratulations.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="nback-sfx-")
    v = SFX_VOLUME

    # SHOW -- soft blip when a cell lights up (C5 quick)
    s = _triangle(523, 0.06, v * 0.4)
    _write_wav(os.path.join(_sfx_dir, "show.wav"), s)
    _sfx_cache["show"] = os.path.join(_sfx_dir, "show.wav")

    # CORRECT -- happy rising ding (C5 -> E5 -> G5)
    s = (_triangle(523, 0.06, v * 0.5) +
         _triangle(659, 0.06, v * 0.55) +
         _triangle(784, 0.1, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "correct.wav"), s)
    _sfx_cache["correct"] = os.path.join(_sfx_dir, "correct.wav")

    # WRONG -- sad descending tone (A4 -> E4 -> C4)
    s = (_square(440, 0.08, v * 0.4, 0.5) +
         _square(330, 0.10, v * 0.35, 0.5) +
         _square(262, 0.14, v * 0.3, 0.5))
    _write_wav(os.path.join(_sfx_dir, "wrong.wav"), s)
    _sfx_cache["wrong"] = os.path.join(_sfx_dir, "wrong.wav")

    # MISS -- low buzz (missed a match you should have caught)
    s = _square(150, 0.3, v * 0.35, 0.5)
    _write_wav(os.path.join(_sfx_dir, "miss.wav"), s)
    _sfx_cache["miss"] = os.path.join(_sfx_dir, "miss.wav")

    # LEVEL_UP -- triumphant jingle (C5 -> E5 -> G5 -> C6)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) +
         _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "level_up.wav"), s)
    _sfx_cache["level_up"] = os.path.join(_sfx_dir, "level_up.wav")

    # START -- short bright chord
    s = (_triangle(523, 0.06, v * 0.4) +
         _triangle(659, 0.06, v * 0.45) +
         _triangle(784, 0.06, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # NEWBEST -- extra victory jingle (C5 -> G5 -> C6 -> E6)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(784, 0.08, v * 0.55) +
         _triangle(1047, 0.08, v * 0.6) +
         _triangle(1319, 0.30, v * 0.7))
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

def render_empty_cell(size=SIZE) -> Image.Image:
    """Dark empty cell on the game grid."""
    return Image.new("RGB", size, EMPTY_COLOR)


def render_lit_cell(color: str, size=SIZE) -> Image.Image:
    """Bright lit cell with a glow effect."""
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    # Inner highlight for a glow look
    d.rectangle([8, 8, 87, 87], outline="white", width=2)
    return img


def render_feedback_cell(ok: bool, size=SIZE) -> Image.Image:
    """Brief green (correct) or red (wrong/miss) flash."""
    bg = "#14532d" if ok else "#7f1d1d"
    fg = "#4ade80" if ok else "#ef4444"
    label = "OK" if ok else "X"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 48), label, font=_font(28), fill=fg, anchor="mm")
    return img


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 30), "N-BACK", font=_font(15), fill="#a78bfa", anchor="mm")
    d.text((48, 55), "TEST", font=_font(11), fill="#7c3aed", anchor="mm")
    return img


def render_hud_level(n: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "LEVEL", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), f"{n}-back", font=_font(20), fill="#60a5fa", anchor="mt")
    return img


def render_hud_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(score), font=_font(26), fill="#fbbf24", anchor="mt")
    return img


def render_hud_lives(lives: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "LIVES", font=_font(14), fill="#9ca3af", anchor="mt")
    hearts = "\u2764" * lives
    clr = "#ef4444" if lives > 1 else "#fca5a5"
    d.text((48, 52), hearts, font=_font(22), fill=clr, anchor="mt")
    return img


def render_match_button(active: bool, size=SIZE) -> Image.Image:
    """MATCH button: green when response window is open, gray otherwise."""
    if active:
        bg, fg = "#065f46", "#4ade80"
    else:
        bg, fg = "#1f2937", "#4b5563"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 48), "MATCH!", font=_font(15), fill=fg, anchor="mm")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, HUD_BG)


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


def render_hud_best(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    label = str(best) if best > 0 else "--"
    d.text((48, 52), label, font=_font(26), fill="#34d399", anchor="mt")
    return img


def render_hud_streak(streak: int, needed: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "STREAK", font=_font(12), fill="#9ca3af", anchor="mt")
    d.text((48, 48), f"{streak}/{needed}", font=_font(22), fill="#a78bfa", anchor="mt")
    return img


def render_level_up(n: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#4c1d95")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "LEVEL UP!", font=_font(14), fill="#c4b5fd", anchor="mm")
    d.text((48, 58), f"{n}-back", font=_font(20), fill="#a78bfa", anchor="mm")
    return img


def render_new_best(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
    return img


def render_final_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "FINAL", font=_font(12), fill="#9ca3af", anchor="mt")
    d.text((48, 45), str(score), font=_font(28), fill="#fbbf24", anchor="mt")
    return img


def render_max_level(n: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, HUD_BG)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "MAX N", font=_font(12), fill="#9ca3af", anchor="mt")
    d.text((48, 45), f"{n}-back", font=_font(20), fill="#60a5fa", anchor="mt")
    return img


# -- game logic ------------------------------------------------------------

class NBackGame:
    def __init__(self, deck):
        self.deck = deck
        self.n = STARTING_N
        self.score = 0
        self.lives = STARTING_LIVES
        self.best = scores.load_best("nback")
        self.streak = 0  # consecutive correct (for level-up)
        self.max_n_reached = STARTING_N
        self.state = "idle"  # idle | playing | response | feedback | gameover
        self.lock = threading.Lock()

        # N-back history: list of grid positions (button keys 8-31)
        self.history: list[int] = []
        self.current_pos = -1  # currently lit cell key
        self.is_match = False  # whether current step is a match
        self.player_responded = False  # did the player press MATCH this step
        self.response_timer: threading.Timer | None = None
        self.step_timer: threading.Timer | None = None

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _clear_grid(self):
        """Set all game grid cells to dark."""
        for k in GAME_KEYS:
            self.set_key(k, render_empty_cell())

    def _update_hud(self):
        self.set_key(0, render_hud_title())
        self.set_key(1, render_hud_level(self.n))
        self.set_key(2, render_hud_score(self.score))
        self.set_key(3, render_match_button(self.state == "response"))
        for k in range(4, 8):
            self.set_key(k, render_hud_empty())

    def _update_hud_full(self):
        """Full HUD with extra info in slots 4-7."""
        self.set_key(0, render_hud_title())
        self.set_key(1, render_hud_level(self.n))
        self.set_key(2, render_hud_score(self.score))
        self.set_key(3, render_match_button(self.state == "response"))
        self.set_key(4, render_hud_lives(self.lives))
        self.set_key(5, render_hud_streak(self.streak, CORRECT_TO_LEVEL_UP))
        self.set_key(6, render_hud_best(self.best))
        self.set_key(7, render_hud_empty())

    def show_idle(self):
        """Show start screen."""
        self.state = "idle"
        # HUD
        self.set_key(0, render_hud_title())
        self.set_key(1, render_hud_level(STARTING_N))
        self.set_key(2, render_hud_score(0))
        self.set_key(3, render_match_button(False))
        self.set_key(4, render_hud_lives(STARTING_LIVES))
        self.set_key(5, render_hud_empty())
        self.set_key(6, render_hud_best(self.best))
        self.set_key(7, render_hud_empty())
        # Game grid
        for k in GAME_KEYS:
            if k == START_KEY:
                self.set_key(k, render_start())
            else:
                self.set_key(k, render_empty_cell())

    def start_game(self):
        """Begin a new game."""
        with self.lock:
            self.n = STARTING_N
            self.score = 0
            self.lives = STARTING_LIVES
            self.streak = 0
            self.max_n_reached = STARTING_N
            self.history = []
            self.current_pos = -1
            self.is_match = False
            self.player_responded = False
            self.state = "playing"

        play_sfx("start")
        play_orc("start")
        self._clear_grid()
        self._update_hud_full()

        # Start first step after a brief pause
        self.step_timer = threading.Timer(1.0, self._next_step)
        self.step_timer.daemon = True
        self.step_timer.start()

    def _pick_cell(self) -> int:
        """Pick next cell position. MATCH_PROBABILITY chance of matching N-back."""
        if len(self.history) >= self.n and random.random() < MATCH_PROBABILITY:
            # Force a match: use the position from N steps ago
            self.is_match = True
            return self.history[-self.n]
        else:
            # Pick a random cell, but try to avoid accidental matches
            self.is_match = False
            candidates = list(GAME_KEYS)
            if len(self.history) >= self.n:
                nback_pos = self.history[-self.n]
                # Remove the N-back position to avoid accidental match
                if nback_pos in candidates:
                    candidates.remove(nback_pos)
            if not candidates:
                candidates = list(GAME_KEYS)
            return random.choice(candidates)

    def _next_step(self):
        """Execute one step: light up a cell, start response window."""
        with self.lock:
            if self.state != "playing":
                return
            self.state = "response"
            self.player_responded = False

        # Pick cell and record to history
        pos = self._pick_cell()
        with self.lock:
            self.current_pos = pos
            self.history.append(pos)

        # Light up the cell with a random bright color
        color = random.choice(CELL_COLORS)
        self.set_key(pos, render_lit_cell(color))
        play_sfx("show")

        # Update MATCH button to active
        self.set_key(3, render_match_button(True))

        # Start response window timer
        self.response_timer = threading.Timer(RESPONSE_WINDOW, self._response_timeout)
        self.response_timer.daemon = True
        self.response_timer.start()

    def _response_timeout(self):
        """Response window closed. Evaluate the step."""
        with self.lock:
            if self.state != "response":
                return
            self.state = "feedback"
            responded = self.player_responded
            was_match = self.is_match
            pos = self.current_pos

        # Turn off the lit cell
        self.set_key(pos, render_empty_cell())
        # Deactivate MATCH button
        self.set_key(3, render_match_button(False))

        if was_match and not responded:
            # MISS: it was a match but player did not press
            self._handle_miss(pos)
        elif not was_match and not responded:
            # Correct rejection: no match, no press
            self._handle_correct_reject(pos)
        # If responded, it was already handled in on_key

    def _handle_correct_hit(self, pos: int):
        """Player correctly identified a match."""
        with self.lock:
            self.score += 1
            self.streak += 1
            streak = self.streak

        play_sfx("correct")
        self.set_key(pos, render_feedback_cell(True))
        self._update_hud_full()

        # Check level up
        if streak >= CORRECT_TO_LEVEL_UP:
            self._level_up(pos)
            return

        # Schedule next step after brief feedback
        self.step_timer = threading.Timer(0.6, self._clear_and_next, args=[pos])
        self.step_timer.daemon = True
        self.step_timer.start()

    def _handle_correct_reject(self, pos: int):
        """Player correctly did NOT press when there was no match."""
        with self.lock:
            self.score += 1
            self.streak += 1
            streak = self.streak

        # No flashy feedback for correct rejections (just update score quietly)
        self._update_hud_full()

        if streak >= CORRECT_TO_LEVEL_UP:
            self._level_up(pos)
            return

        # Schedule next step
        self.step_timer = threading.Timer(0.5, self._next_playing_step)
        self.step_timer.daemon = True
        self.step_timer.start()

    def _handle_false_alarm(self, pos: int):
        """Player pressed MATCH but it was NOT a match. Lose a life."""
        with self.lock:
            self.lives -= 1
            self.streak = 0
            lives = self.lives

        play_sfx("wrong")
        self.set_key(pos, render_feedback_cell(False))
        self._update_hud_full()

        if lives <= 0:
            self._game_over()
            return

        self.step_timer = threading.Timer(0.8, self._clear_and_next, args=[pos])
        self.step_timer.daemon = True
        self.step_timer.start()

    def _handle_miss(self, pos: int):
        """Player missed a match (did not press when it WAS a match). Lose a life."""
        with self.lock:
            self.lives -= 1
            self.streak = 0
            lives = self.lives

        play_sfx("miss")
        self.set_key(pos, render_feedback_cell(False))
        self._update_hud_full()

        if lives <= 0:
            self._game_over()
            return

        self.step_timer = threading.Timer(0.8, self._clear_and_next, args=[pos])
        self.step_timer.daemon = True
        self.step_timer.start()

    def _clear_and_next(self, pos: int):
        """Clear feedback cell and proceed to next step."""
        self.set_key(pos, render_empty_cell())
        self._next_playing_step()

    def _next_playing_step(self):
        """Transition back to playing and schedule next step."""
        with self.lock:
            if self.state == "gameover":
                return
            self.state = "playing"

        # Small pause between steps
        gap = STEP_INTERVAL - RESPONSE_WINDOW
        if gap < 0.3:
            gap = 0.3
        self.step_timer = threading.Timer(gap, self._next_step)
        self.step_timer.daemon = True
        self.step_timer.start()

    def _level_up(self, feedback_pos: int):
        """Advance to next N level."""
        with self.lock:
            self.n += 1
            self.streak = 0
            if self.n > self.max_n_reached:
                self.max_n_reached = self.n

        play_sfx("level_up")
        play_orc("level_up")

        # Show level-up feedback on grid
        self.set_key(feedback_pos, render_empty_cell())
        # Flash level-up message across a few cells
        center_cells = [11, 12, 19, 20]
        for k in center_cells:
            self.set_key(k, render_level_up(self.n))

        self._update_hud_full()

        def _after_level_up():
            time.sleep(1.5)
            for k in center_cells:
                self.set_key(k, render_empty_cell())
            self._next_playing_step()

        threading.Thread(target=_after_level_up, daemon=True).start()

    def _game_over(self):
        """End the game, show results."""
        with self.lock:
            self.state = "gameover"

        # Cancel timers
        if self.response_timer:
            self.response_timer.cancel()
        if self.step_timer:
            self.step_timer.cancel()

        is_new_best = self.score > self.best
        if is_new_best:
            self.best = self.score
            scores.save_best("nback", self.best)

        play_orc("gameover")
        if is_new_best and self.score > 0:
            play_sfx("newbest")

        # Show game over screen
        self._clear_grid()

        # HUD: final stats
        self.set_key(0, render_hud_title())
        self.set_key(1, render_hud_level(self.n))
        self.set_key(2, render_hud_score(self.score))
        self.set_key(3, render_match_button(False))
        self.set_key(4, render_hud_lives(0))
        self.set_key(5, render_hud_empty())
        self.set_key(6, render_hud_best(self.best))
        if is_new_best and self.score > 0:
            self.set_key(7, render_new_best())
        else:
            self.set_key(7, render_hud_empty())

        # Game area: game over + stats + restart
        self.set_key(10, render_game_over())
        self.set_key(11, render_game_over())
        self.set_key(12, render_final_score(self.score))
        self.set_key(13, render_max_level(self.max_n_reached))
        self.set_key(START_KEY, render_start())

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and self.state in ("idle", "gameover"):
            self.start_game()
            return

        # MATCH button press
        if key == MATCH_KEY:
            with self.lock:
                if self.state != "response":
                    return
                if self.player_responded:
                    return  # already responded this step
                self.player_responded = True
                was_match = self.is_match
                pos = self.current_pos
                self.state = "feedback"

            # Cancel response timeout -- we are handling it now
            if self.response_timer:
                self.response_timer.cancel()

            # Turn off the lit cell
            self.set_key(pos, render_empty_cell())
            # Deactivate MATCH button
            self.set_key(3, render_match_button(False))

            if was_match:
                self._handle_correct_hit(pos)
            else:
                self._handle_false_alarm(pos)
            return

        # Any game grid key press during response window also counts as MATCH
        if key in GAME_KEYS:
            with self.lock:
                if self.state != "response":
                    return
                if self.player_responded:
                    return
                self.player_responded = True
                was_match = self.is_match
                pos = self.current_pos
                self.state = "feedback"

            if self.response_timer:
                self.response_timer.cancel()

            self.set_key(pos, render_empty_cell())
            self.set_key(3, render_match_button(False))

            if was_match:
                self._handle_correct_hit(pos)
            else:
                self._handle_false_alarm(pos)
            return


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
    print("N-BACK TEST! Press START to begin.")

    game = NBackGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(f"\nBye! Best score: {game.best}")
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
