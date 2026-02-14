"""Sequence Memory (Chimp Test) -- Stream Deck mini-game.

Numbers 1-N appear on random grid cells, then hide behind "?" cards.
Tap them in order: 1, 2, 3, ... N. Each round adds one more number.
Tests working memory / IQ.

Usage:
    uv run python scripts/sequence_game.py
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
GAME_KEYS = list(range(8, 32))  # rows 2-4 = 3x8 game grid (24 buttons)
HUD_KEYS = list(range(0, 8))    # row 1 = HUD
START_KEY = 20                   # center-ish button for start
ROWS = 3
COLS = 8
SIZE = (96, 96)
START_LEVEL = 4                  # begin with 4 numbers
REVEAL_TIME = 1.5                # seconds to show numbers before hiding

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

# 12 distinct colors for number tiles (cycled if N > 12)
NUMBER_COLORS = [
    "#ef4444",  # red
    "#3b82f6",  # blue
    "#22c55e",  # green
    "#eab308",  # yellow
    "#f97316",  # orange
    "#a855f7",  # purple
    "#ec4899",  # pink
    "#06b6d4",  # cyan
    "#84cc16",  # lime
    "#e879f9",  # magenta
    "#14b8a6",  # teal
    "#fb923c",  # coral
]

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "dota2_axe/sounds/AxeIsReady.mp3",
        "dota2_axe/sounds/GoodDayToFight.mp3",
        "dota2_axe/sounds/ToBattle.mp3",
    ],
    "correct_round": [
        "dota2_axe/sounds/Axeactly.mp3",
        "dota2_axe/sounds/CutAbove.mp3",
        "dota2_axe/sounds/AxeGoes.mp3",
    ],
    "gameover": [
        "dota2_axe/sounds/YouGetNothing.mp3",
        "dota2_axe/sounds/FoughtBadly.mp3",
        "dota2_axe/sounds/ISaidGoodDaySir.mp3",
    ],
    "newbest": [
        "dota2_axe/sounds/ComeAndGetIt.mp3",
        "dota2_axe/sounds/LetTheCarnageBegin.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="sequence-sfx-")
    v = SFX_VOLUME

    # SHOW -- rising blip when numbers appear (C5 -> E5 quick)
    s = _triangle(523, 0.06, v * 0.4) + _triangle(659, 0.08, v * 0.5)
    _write_wav(os.path.join(_sfx_dir, "show.wav"), s)
    _sfx_cache["show"] = os.path.join(_sfx_dir, "show.wav")

    # CORRECT -- happy note on correct tap (G5 short)
    s = _triangle(784, 0.08, v * 0.5) + _triangle(880, 0.06, v * 0.45)
    _write_wav(os.path.join(_sfx_dir, "correct.wav"), s)
    _sfx_cache["correct"] = os.path.join(_sfx_dir, "correct.wav")

    # WRONG -- sad descending (A4 -> F4 -> D4)
    s = (_square(440, 0.1, v * 0.4, 0.5) +
         _square(349, 0.1, v * 0.35, 0.5) +
         _square(294, 0.15, v * 0.3, 0.5))
    _write_wav(os.path.join(_sfx_dir, "wrong.wav"), s)
    _sfx_cache["wrong"] = os.path.join(_sfx_dir, "wrong.wav")

    # START -- game begin jingle (C5 -> E5 -> G5)
    s = (_triangle(523, 0.06, v * 0.5) +
         _triangle(659, 0.06, v * 0.55) +
         _triangle(784, 0.1, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # NEWBEST -- victory jingle (C5 -> E5 -> G5 -> C6)
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

def render_number_tile(number: int, color: str, size=SIZE) -> Image.Image:
    """Colored tile with large number text."""
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="white", width=2)
    d.text((48, 48), str(number), font=_font(36), fill="white", anchor="mm")
    return img


def render_hidden_tile(size=SIZE) -> Image.Image:
    """Dark gray tile with '?' -- face-down card."""
    img = Image.new("RGB", size, "#374151")
    d = ImageDraw.Draw(img)
    d.rectangle([4, 4, 91, 91], outline="#4b5563", width=2)
    d.text((48, 48), "?", font=_font(40), fill="#9ca3af", anchor="mm")
    return img


def render_empty_tile(size=SIZE) -> Image.Image:
    """Empty dark tile for unused grid positions."""
    return Image.new("RGB", size, "#0f172a")


def render_correct_flash(size=SIZE) -> Image.Image:
    """Brief green flash for correct tap."""
    img = Image.new("RGB", size, "#22c55e")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "\u2713", font=_font(40), fill="white", anchor="mm")
    return img


def render_wrong_flash(size=SIZE) -> Image.Image:
    """Brief red flash for wrong tap."""
    img = Image.new("RGB", size, "#ef4444")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "\u2717", font=_font(40), fill="white", anchor="mm")
    return img


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "CHIMP", font=_font(15), fill="#f59e0b", anchor="mm")
    d.text((48, 52), "TEST", font=_font(15), fill="#fbbf24", anchor="mm")
    return img


def render_hud_level(level: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "LEVEL", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(level), font=_font(28), fill="#60a5fa", anchor="mt")
    return img


def render_hud_best(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    label = str(best) if best > 0 else "--"
    d.text((48, 52), label, font=_font(28), fill="#34d399", anchor="mt")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_start_btn(size=SIZE) -> Image.Image:
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


def render_score_tile(level: int, size=SIZE) -> Image.Image:
    """Show final score on game over."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(level), font=_font(28), fill="#fbbf24", anchor="mt")
    return img


def render_new_best(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
    return img


# -- game logic ------------------------------------------------------------

class SequenceGame:
    def __init__(self, deck):
        self.deck = deck
        self.level = START_LEVEL
        self.best = scores.load_best("sequence")
        self.running = False
        self.lock = threading.Lock()
        self.accepting_input = False

        # Round state
        # positions: list of grid keys (8-31) that hold numbers this round
        self.positions: list[int] = []
        # number_at: maps grid key -> number (1-based)
        self.number_at: dict[int, int] = {}
        # color_at: maps grid key -> color hex
        self.color_at: dict[int, str] = {}
        # next_expected: the next number the player must tap (1-based)
        self.next_expected = 1
        # hidden: whether numbers are currently hidden behind "?"
        self.hidden = False

        # Timer reference for reveal timeout
        self.reveal_timer: threading.Timer | None = None

        # Pre-render reusable images
        self.img_hud_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_empty = render_empty_tile()
        self.img_hidden = render_hidden_tile()
        self.img_start = render_start_btn()
        self.img_correct = render_correct_flash()
        self.img_wrong = render_wrong_flash()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _update_hud(self):
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_level(self.level))
        self.set_key(2, render_hud_best(self.best))
        # HUD buttons 3-7 are empty
        for k in range(3, 8):
            self.set_key(k, self.img_hud_empty)

    def show_idle(self):
        """Show start screen."""
        self.running = False
        self.accepting_input = False
        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_level(START_LEVEL))
        self.set_key(2, render_hud_best(self.best))
        for k in range(3, 8):
            self.set_key(k, self.img_hud_empty)
        # Game grid -- all empty, start button at START_KEY
        for k in GAME_KEYS:
            if k == START_KEY:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, self.img_empty)

    def start_game(self):
        """Begin a new game at level START_LEVEL."""
        with self.lock:
            self.level = START_LEVEL
            self.running = True
            self.accepting_input = False
        play_sfx("start")
        play_orc("start")
        self._update_hud()
        # Start first round in background
        threading.Thread(target=self._show_numbers, daemon=True).start()

    def _show_numbers(self):
        """Pick N random positions, assign numbers 1-N, display them."""
        with self.lock:
            n = self.level
            self.accepting_input = False
            self.hidden = False
            self.next_expected = 1

            # Pick N unique positions from GAME_KEYS
            self.positions = random.sample(GAME_KEYS, min(n, len(GAME_KEYS)))

            # Assign numbers 1-N to positions
            self.number_at = {}
            self.color_at = {}
            for idx, key in enumerate(self.positions):
                num = idx + 1
                self.number_at[key] = num
                self.color_at[key] = NUMBER_COLORS[(num - 1) % len(NUMBER_COLORS)]

        # Clear the grid first
        for k in GAME_KEYS:
            self.set_key(k, self.img_empty)

        self._update_hud()

        # Show number tiles
        play_sfx("show")
        for key in self.positions:
            num = self.number_at[key]
            color = self.color_at[key]
            self.set_key(key, render_number_tile(num, color))

        # After REVEAL_TIME, hide the numbers
        self.reveal_timer = threading.Timer(REVEAL_TIME, self._hide_numbers)
        self.reveal_timer.daemon = True
        self.reveal_timer.start()

    def _hide_numbers(self):
        """Replace all number tiles with '?' tiles and accept input."""
        with self.lock:
            if not self.running:
                return
            self.hidden = True
            self.accepting_input = True

        # Replace number tiles with hidden tiles
        for key in self.positions:
            # Only hide tiles that haven't been correctly tapped yet
            num = self.number_at.get(key, 0)
            if num >= self.next_expected:
                self.set_key(key, self.img_hidden)

    def _correct_tap(self, key: int):
        """Handle a correct tap -- green flash, advance."""
        # Brief green flash
        self.set_key(key, self.img_correct)
        play_sfx("correct")

        with self.lock:
            self.next_expected += 1
            all_found = self.next_expected > self.level

        def _after_flash():
            time.sleep(0.15)
            # Clear the tapped cell
            self.set_key(key, self.img_empty)

            if all_found:
                self._round_complete()

        threading.Thread(target=_after_flash, daemon=True).start()

    def _round_complete(self):
        """All numbers found -- advance to next level."""
        with self.lock:
            self.level += 1
            self.accepting_input = False

        play_orc("correct_round")
        self._update_hud()

        # Brief pause, then show next round
        time.sleep(0.6)
        if self.running:
            self._show_numbers()

    def _wrong_tap(self, key: int):
        """Handle a wrong tap -- red flash, game over."""
        with self.lock:
            self.running = False
            self.accepting_input = False
            # Score is level - 1 (last completed level)
            score = self.level - 1

        # Cancel any pending reveal timer
        if self.reveal_timer:
            self.reveal_timer.cancel()
            self.reveal_timer = None

        # Red flash on wrong key
        self.set_key(key, self.img_wrong)
        play_sfx("wrong")

        # Reveal all remaining hidden numbers briefly
        for k in self.positions:
            num = self.number_at.get(k, 0)
            color = self.color_at.get(k, "#374151")
            if num >= self.next_expected and k != key:
                self.set_key(k, render_number_tile(num, color))

        # Check for new best
        is_new_best = score > self.best and score > 0
        if is_new_best:
            self.best = score
            scores.save_best("sequence", self.best)
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_orc("gameover")

        def _show_game_over():
            time.sleep(1.2)
            # Clear grid
            for k in GAME_KEYS:
                self.set_key(k, self.img_empty)
            # Show game over display
            self.set_key(18, render_game_over())
            self.set_key(19, render_score_tile(score))
            if is_new_best:
                self.set_key(21, render_new_best())
            # Restart button
            self.set_key(START_KEY, self.img_start)
            # Update HUD with final stats
            self.set_key(0, self.img_hud_title)
            self.set_key(1, render_hud_level(score))
            self.set_key(2, render_hud_best(self.best))
            for k in range(3, 8):
                self.set_key(k, self.img_hud_empty)

        threading.Thread(target=_show_game_over, daemon=True).start()

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        if key not in GAME_KEYS:
            return

        # Determine action under lock, execute outside lock
        action = None  # "correct", "wrong", or None (ignore)
        hide_keys: list[int] = []

        with self.lock:
            if not self.accepting_input:
                return

            # If numbers are still visible (not hidden yet), tapping number 1
            # should trigger hide and count as first correct tap
            if not self.hidden:
                if self.number_at.get(key) == 1:
                    # Cancel the reveal timer, hide numbers now
                    if self.reveal_timer:
                        self.reveal_timer.cancel()
                        self.reveal_timer = None
                    self.hidden = True
                    # Collect keys to hide (will set_key outside lock)
                    for k in self.positions:
                        num = self.number_at.get(k, 0)
                        if num > 1:
                            hide_keys.append(k)
                else:
                    # Tapped wrong cell while numbers visible -- ignore
                    return

            # Determine tap result
            if key not in self.number_at:
                self.accepting_input = False
                action = "wrong"
            elif self.number_at[key] == self.next_expected:
                action = "correct"
            else:
                self.accepting_input = False
                action = "wrong"

        # Outside lock -- apply visual updates
        for k in hide_keys:
            self.set_key(k, self.img_hidden)

        if action == "correct":
            self._correct_tap(key)
        elif action == "wrong":
            self._wrong_tap(key)


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
    print("CHIMP TEST! Press the center button to start.")

    game = SequenceGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(f"\nBye! Best: {game.best}")
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
