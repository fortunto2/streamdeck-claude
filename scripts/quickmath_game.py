"""Quick Math — Stream Deck mini-game.

Solve arithmetic equations as fast as possible! Correct answers score points,
wrong answers or timeouts lose lives. Speed increases every 5 correct answers.

Layout (8x4 = 32 keys):
  Row 1 (0-7):   HUD — title, score, lives, timer bar, empty
  Row 2 (8-15):  Equation display — large numbers and operator
  Row 3 (16-23): Level info / empty (START button at key 20)
  Row 4 (24-31): 4 answer options centered on keys 26-29

Usage:
    uv run python scripts/quickmath_game.py
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

import scores
import sound_engine

# ── config ───────────────────────────────────────────────────────────
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

HUD_KEYS = list(range(0, 8))       # row 1
EQ_KEYS = list(range(8, 16))       # row 2 — equation
MID_KEYS = list(range(16, 24))     # row 3 — level / start
OPT_KEYS = list(range(24, 32))     # row 4 — answer options
ANSWER_KEYS = [26, 27, 28, 29]     # 4 centered answer buttons

START_KEY = 20
MAX_LIVES = 3

TIMER_START = 8.0       # initial seconds per question
TIMER_DECREASE = 0.3    # seconds shaved every 5 correct
TIMER_MIN = 3.0         # fastest allowed timer

BG_DARK = "#1e293b"
BG_HUD = "#111827"
BG_OPTION = "#065f46"
BG_OPTION_WRONG = "#7c2d12"
BG_OPTION_RIGHT = "#14532d"

OP_COLORS = {
    "+": "#22c55e",   # green
    "-": "#ef4444",   # red
    "\u00d7": "#3b82f6",   # blue  (multiplication)
    "\u00f7": "#eab308",   # yellow (division)
}

# ── orc voice lines (peon-ping packs) ───────────────────────────────
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "peon/sounds/PeonReady1.wav",
        "peon/sounds/PeonWhat1.wav",
        "peon/sounds/PeonWhat2.wav",
    ],
    "correct": [
        "peon/sounds/PeonYes1.wav",
        "peon/sounds/PeonYes2.wav",
        "peon/sounds/PeonYes3.wav",
        "peon/sounds/PeonYes4.wav",
    ],
    "gameover": [
        "peon/sounds/PeonAngry1.wav",
        "peon/sounds/PeonAngry2.wav",
        "peon/sounds/PeonDeath.wav",
    ],
    "newbest": [
        "peon/sounds/PeonWarcry1.wav",
        "peon/sounds/PeonYesAttack1.wav",
    ],
}

_last_orc_time: float = 0
ORC_COOLDOWN = 4.0


def play_orc(event: str):
    """Play a random orc voice line — with cooldown to avoid spam."""
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


# ── 8-bit sound engine ───────────────────────────────────────────────
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
    _sfx_dir = tempfile.mkdtemp(prefix="quickmath-sfx-")
    v = SFX_VOLUME

    # CORRECT — happy rising two-note (E5 -> G5)
    s = _triangle(659, 0.08, v * 0.5) + _triangle(784, 0.12, v * 0.6)
    _write_wav(os.path.join(_sfx_dir, "correct.wav"), s)
    _sfx_cache["correct"] = os.path.join(_sfx_dir, "correct.wav")

    # WRONG — sad descending (A4 -> E4)
    s = _square(440, 0.1, v * 0.35, 0.5) + _square(330, 0.15, v * 0.3, 0.5)
    _write_wav(os.path.join(_sfx_dir, "wrong.wav"), s)
    _sfx_cache["wrong"] = os.path.join(_sfx_dir, "wrong.wav")

    # START — bright blip (C5 quick)
    s = _triangle(523, 0.06, v * 0.5) + _triangle(659, 0.06, v * 0.55)
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # NEWBEST — victory jingle (C5 -> E5 -> G5 -> C6)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) +
         _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "newbest.wav"), s)
    _sfx_cache["newbest"] = os.path.join(_sfx_dir, "newbest.wav")

    # TICK — short warning beep when timer is low (A5 square, staccato)
    s = _square(880, 0.04, v * 0.3, 0.25)
    _write_wav(os.path.join(_sfx_dir, "tick.wav"), s)
    _sfx_cache["tick"] = os.path.join(_sfx_dir, "tick.wav")


def play_sfx(name: str):
    """Play sound non-blocking via afplay."""
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# ── equation generator ───────────────────────────────────────────────

def _generate_equation(level: int) -> tuple[str, list[str], int, str]:
    """Generate an equation based on level.

    Returns: (display_parts, option_labels, correct_index, operator_symbol)
    display_parts: list of strings for each equation element, e.g. ["7", "x", "8", "=", "?"]
    option_labels: list of 4 string answers
    correct_index: index 0-3 of the correct answer in option_labels
    operator_symbol: the operator string for coloring
    """
    if level <= 3:
        # Addition: a + b, numbers 1-20
        a = random.randint(1, 20)
        b = random.randint(1, 20)
        answer = a + b
        op = "+"
        parts = [str(a), "+", str(b), "=", "?"]
    elif level <= 6:
        # Subtraction: a - b, result >= 0, numbers 1-30
        a = random.randint(1, 30)
        b = random.randint(1, min(a, 30))
        answer = a - b
        op = "-"
        parts = [str(a), "\u2212", str(b), "=", "?"]
    elif level <= 9:
        # Multiplication: a * b, factors 2-12
        a = random.randint(2, 12)
        b = random.randint(2, 12)
        answer = a * b
        op = "\u00d7"
        parts = [str(a), "\u00d7", str(b), "=", "?"]
    elif level <= 12:
        # Division: a / b, evenly divisible
        b = random.randint(2, 12)
        answer = random.randint(1, 12)
        a = b * answer
        op = "\u00f7"
        parts = [str(a), "\u00f7", str(b), "=", "?"]
    else:
        # Mixed: two-step operations (a + b * c) or bigger numbers
        variant = random.randint(0, 2)
        if variant == 0:
            # a + b * c
            b = random.randint(2, 10)
            c = random.randint(2, 10)
            a = random.randint(1, 20)
            answer = a + b * c
            op = "+"
            parts = [str(a), "+", f"{b}\u00d7{c}", "=", "?"]
        elif variant == 1:
            # a * b - c
            a = random.randint(2, 12)
            b = random.randint(2, 12)
            c = random.randint(1, a * b - 1)
            answer = a * b - c
            op = "\u00d7"
            parts = [f"{a}\u00d7{b}", "\u2212", str(c), "=", "?"]
        else:
            # Large addition/subtraction
            a = random.randint(20, 99)
            b = random.randint(10, 50)
            if random.random() < 0.5:
                answer = a + b
                op = "+"
                parts = [str(a), "+", str(b), "=", "?"]
            else:
                if b > a:
                    a, b = b, a
                answer = a - b
                op = "-"
                parts = [str(a), "\u2212", str(b), "=", "?"]

    # Generate wrong answers (close to correct, but unique)
    wrongs: set[int] = set()
    attempts = 0
    while len(wrongs) < 3 and attempts < 100:
        attempts += 1
        offset = random.choice([-3, -2, -1, 1, 2, 3, -5, 5, -10, 10])
        wrong = answer + offset
        if wrong != answer and wrong >= 0 and wrong not in wrongs:
            wrongs.add(wrong)
    # Fallback if we somehow can't generate enough
    fallback = 1
    while len(wrongs) < 3:
        if answer + fallback not in wrongs and answer + fallback != answer:
            wrongs.add(answer + fallback)
        fallback += 1

    options = list(wrongs) + [answer]
    random.shuffle(options)
    correct_index = options.index(answer)
    option_labels = [str(o) for o in options]

    # Map operator for coloring
    op_display = op
    if op == "-":
        op_display = "\u2212"

    return parts, option_labels, correct_index, op_display


# ── renderers ────────────────────────────────────────────────────────

def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, BG_HUD)


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, BG_HUD)
    d = ImageDraw.Draw(img)
    d.text((48, 34), "QUICK", font=_font(14), fill="#f59e0b", anchor="mm")
    d.text((48, 56), "MATH", font=_font(16), fill="#fbbf24", anchor="mm")
    return img


def render_hud_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, BG_HUD)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(score), font=_font(28), fill="#34d399", anchor="mt")
    return img


def render_hud_lives(lives: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, BG_HUD)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "LIVES", font=_font(14), fill="#9ca3af", anchor="mt")
    hearts = "\u2764" * lives + "\u2661" * (MAX_LIVES - lives)
    clr = "#ef4444" if lives <= 1 else "#f87171"
    d.text((48, 54), hearts, font=_font(18), fill=clr, anchor="mt")
    return img


def render_hud_timer(fraction: float, size=SIZE) -> Image.Image:
    """Timer bar — fraction from 0.0 (empty) to 1.0 (full).

    Green when > 0.375, yellow when > 0.1875, red otherwise.
    """
    img = Image.new("RGB", size, BG_HUD)
    d = ImageDraw.Draw(img)
    d.text((48, 14), "TIME", font=_font(12), fill="#9ca3af", anchor="mt")

    # Bar dimensions
    bar_x = 10
    bar_y = 36
    bar_w = 76
    bar_h = 20

    # Bar outline
    d.rectangle([bar_x, bar_y, bar_x + bar_w, bar_y + bar_h],
                outline="#4b5563", width=1)

    # Fill
    fill_w = max(0, int(bar_w * fraction))
    if fraction > 0.375:
        fill_color = "#22c55e"  # green
    elif fraction > 0.1875:
        fill_color = "#eab308"  # yellow
    else:
        fill_color = "#ef4444"  # red

    if fill_w > 0:
        d.rectangle([bar_x + 1, bar_y + 1, bar_x + fill_w, bar_y + bar_h - 1],
                     fill=fill_color)

    # Seconds text below bar
    d.text((48, 66), f"{max(0.0, fraction * 100):.0f}%", font=_font(14),
           fill=fill_color, anchor="mt")
    return img


def render_eq_element(text: str, is_operator: bool = False, op_char: str = "",
                      size=SIZE) -> Image.Image:
    """Render an equation element — number or operator on dark bg."""
    img = Image.new("RGB", size, BG_DARK)
    d = ImageDraw.Draw(img)

    if text == "?":
        d.text((48, 48), "?", font=_font(44), fill="#eab308", anchor="mm")
    elif is_operator:
        # Use operator color
        color = OP_COLORS.get(op_char, "#ffffff")
        d.text((48, 48), text, font=_font(40), fill=color, anchor="mm")
    else:
        # Number — large white text
        fsize = 36 if len(text) <= 2 else 28 if len(text) <= 3 else 22
        d.text((48, 48), text, font=_font(fsize), fill="white", anchor="mm")
    return img


def render_eq_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, BG_DARK)


def render_option(text: str, size=SIZE) -> Image.Image:
    """Answer option button — white text on green bg."""
    img = Image.new("RGB", size, BG_OPTION)
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#059669", width=2)
    fsize = 34 if len(text) <= 2 else 26 if len(text) <= 3 else 20
    d.text((48, 48), text, font=_font(fsize), fill="white", anchor="mm")
    return img


def render_option_correct(text: str, size=SIZE) -> Image.Image:
    """Correct answer flash — bright green."""
    img = Image.new("RGB", size, BG_OPTION_RIGHT)
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#4ade80", width=3)
    fsize = 34 if len(text) <= 2 else 26 if len(text) <= 3 else 20
    d.text((48, 48), text, font=_font(fsize), fill="#4ade80", anchor="mm")
    return img


def render_option_wrong(text: str, size=SIZE) -> Image.Image:
    """Wrong answer flash — red."""
    img = Image.new("RGB", size, BG_OPTION_WRONG)
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#ef4444", width=3)
    fsize = 34 if len(text) <= 2 else 26 if len(text) <= 3 else 20
    d.text((48, 48), text, font=_font(fsize), fill="#fca5a5", anchor="mm")
    return img


def render_mid_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, BG_DARK)


def render_level_info(level: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, BG_DARK)
    d = ImageDraw.Draw(img)
    d.text((48, 38), "LEVEL", font=_font(12), fill="#9ca3af", anchor="mm")
    d.text((48, 58), str(level), font=_font(22), fill="#60a5fa", anchor="mm")
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
    d.text((48, 38), "GAME", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "OVER", font=_font(16), fill="#fca5a5", anchor="mm")
    return img


def render_final_score(score: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, BG_HUD)
    d = ImageDraw.Draw(img)
    d.text((48, 18), "FINAL", font=_font(12), fill="#9ca3af", anchor="mt")
    d.text((48, 40), str(score), font=_font(30), fill="#34d399", anchor="mt")
    d.text((48, 76), "pts", font=_font(12), fill="#6b7280", anchor="mt")
    return img


def render_best_score(best: int, is_new: bool, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, BG_HUD)
    d = ImageDraw.Draw(img)
    header = "NEW!" if is_new else "BEST"
    hdr_clr = "#fbbf24" if is_new else "#9ca3af"
    d.text((48, 18), header, font=_font(14), fill=hdr_clr, anchor="mt")
    label = str(best) if best > 0 else "--"
    d.text((48, 42), label, font=_font(28), fill="#34d399", anchor="mt")
    d.text((48, 76), "pts", font=_font(12), fill="#6b7280", anchor="mt")
    return img


def render_new_best(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, BG_HUD)
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
    return img


# ── game logic ───────────────────────────────────────────────────────

class QuickMathGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.lives = MAX_LIVES
        self.level = 1
        self.best = scores.load_best("quickmath")  # best score across games
        self.running = False
        self.lock = threading.Lock()

        # Current equation state
        self.options: list[str] = []
        self.correct_index = 0       # 0-3 index into ANSWER_KEYS
        self.accepting_input = False

        # Timer state
        self.timer_total = TIMER_START
        self.timer_start: float = 0.0
        self.timer_thread: threading.Timer | None = None
        self._tick_running = False
        self._tick_thread: threading.Thread | None = None
        self._last_tick_time: float = 0.0

        # Pre-render reusable images
        self.img_hud_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_eq_empty = render_eq_empty()
        self.img_mid_empty = render_mid_empty()
        self.img_start = render_start()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # ── timer ─────────────────────────────────────────────────────

    def _get_timer_limit(self) -> float:
        """Timer limit based on score — decreases every 5 correct."""
        decrease = (self.score // 5) * TIMER_DECREASE
        return max(TIMER_MIN, TIMER_START - decrease)

    def _start_timer(self):
        """Start the countdown timer for the current question."""
        self._cancel_timer()
        self.timer_total = self._get_timer_limit()
        self.timer_start = time.monotonic()
        self._last_tick_time = 0.0

        # Start the tick updater thread
        self._tick_running = True
        self._tick_thread = threading.Thread(target=self._timer_tick_loop, daemon=True)
        self._tick_thread.start()

        # Set the timeout timer
        self.timer_thread = threading.Timer(self.timer_total, self._on_timeout)
        self.timer_thread.daemon = True
        self.timer_thread.start()

    def _cancel_timer(self):
        """Cancel active timers."""
        self._tick_running = False
        if self.timer_thread:
            self.timer_thread.cancel()
            self.timer_thread = None

    def _timer_tick_loop(self):
        """Update the timer bar periodically and play tick sounds."""
        while self._tick_running:
            elapsed = time.monotonic() - self.timer_start
            remaining = max(0.0, self.timer_total - elapsed)
            fraction = remaining / self.timer_total if self.timer_total > 0 else 0

            # Update timer bar on HUD
            try:
                self.set_key(3, render_hud_timer(fraction))
            except Exception:
                pass

            # Play tick sound when timer is low (< 3s) — once per second
            if remaining < 3.0 and remaining > 0.1:
                sec_bucket = int(remaining)
                if sec_bucket != self._last_tick_time:
                    self._last_tick_time = sec_bucket
                    play_sfx("tick")

            if remaining <= 0 or not self._tick_running:
                break

            time.sleep(0.15)

    def _on_timeout(self):
        """Timer ran out — treat as wrong answer."""
        with self.lock:
            if not self.running or not self.accepting_input:
                return
            self.accepting_input = False
            self.lives -= 1
            lives_left = self.lives

        self._tick_running = False
        play_sfx("wrong")

        # Flash the correct answer
        correct_key = ANSWER_KEYS[self.correct_index]
        self.set_key(correct_key, render_option_correct(self.options[self.correct_index]))

        # Flash all wrong options red
        for i, key in enumerate(ANSWER_KEYS):
            if i != self.correct_index:
                self.set_key(key, render_option_wrong(self.options[i]))

        self._update_hud()

        def _after_timeout():
            time.sleep(1.0)
            if lives_left <= 0:
                self._game_over()
            else:
                self._next_question()

        threading.Thread(target=_after_timeout, daemon=True).start()

    # ── HUD ───────────────────────────────────────────────────────

    def _update_hud(self):
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_lives(self.lives))
        # Timer bar is updated by the tick loop, but set initial state
        elapsed = time.monotonic() - self.timer_start if self.timer_start > 0 else 0
        remaining = max(0.0, self.timer_total - elapsed)
        fraction = remaining / self.timer_total if self.timer_total > 0 else 1.0
        self.set_key(3, render_hud_timer(fraction))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

    # ── display ───────────────────────────────────────────────────

    def _show_equation(self, parts: list[str], op_display: str):
        """Show equation parts across row 2 (keys 8-15), centered."""
        # Clear row 2
        for k in EQ_KEYS:
            self.set_key(k, self.img_eq_empty)

        # Center the parts across 8 buttons
        n = len(parts)
        start = 8 + (8 - n) // 2  # center horizontally

        for i, part in enumerate(parts):
            key = start + i
            if key > 15:
                break
            # Determine if this is an operator
            is_op = part in ("+", "\u2212", "\u00d7", "\u00f7", "=")
            # Map display operator to OP_COLORS key
            op_key = part
            if part == "\u2212":
                op_key = "-"
            elif part == "=":
                op_key = "="
            self.set_key(key, render_eq_element(part, is_operator=is_op, op_char=op_key))

    def _show_options(self):
        """Show 4 answer options on row 4 (keys 26-29)."""
        # Clear entire row 4
        for k in OPT_KEYS:
            if k in ANSWER_KEYS:
                idx = ANSWER_KEYS.index(k)
                if idx < len(self.options):
                    self.set_key(k, render_option(self.options[idx]))
                else:
                    self.set_key(k, self.img_mid_empty)
            else:
                self.set_key(k, self.img_mid_empty)

    def _clear_mid_row(self):
        """Clear row 3 and optionally show level info."""
        for k in MID_KEYS:
            if k == 18:
                self.set_key(k, render_level_info(self.level))
            else:
                self.set_key(k, self.img_mid_empty)

    # ── game flow ─────────────────────────────────────────────────

    def show_idle(self):
        """Show start screen."""
        self.running = False
        self.accepting_input = False

        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(0))
        self.set_key(2, render_hud_lives(MAX_LIVES))
        self.set_key(3, render_hud_timer(1.0))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Equation row — empty
        for k in EQ_KEYS:
            self.set_key(k, self.img_eq_empty)

        # Mid row — start button
        for k in MID_KEYS:
            if k == START_KEY:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, self.img_mid_empty)

        # Options row — empty
        for k in OPT_KEYS:
            self.set_key(k, self.img_mid_empty)

    def start_game(self):
        """Start a new game."""
        with self.lock:
            self.score = 0
            self.lives = MAX_LIVES
            self.level = 1
            self.running = True
            self.accepting_input = False

        play_sfx("start")
        play_orc("start")

        self._next_question()

    def _next_question(self):
        """Generate and display the next equation."""
        with self.lock:
            if not self.running:
                return
            # Calculate level from score
            self.level = (self.score // 5) + 1

        # Generate equation
        parts, option_labels, correct_idx, op_display = _generate_equation(self.level)

        with self.lock:
            self.options = option_labels
            self.correct_index = correct_idx
            self.accepting_input = True

        # Display
        self._show_equation(parts, op_display)
        self._clear_mid_row()
        self._show_options()
        self._update_hud()

        # Start timer
        self._start_timer()

    def _game_over(self):
        """Handle game over."""
        with self.lock:
            self.running = False
            self.accepting_input = False
            final_score = self.score
            is_new_best = final_score > self.best
            if is_new_best:
                self.best = final_score
                scores.save_best("quickmath", self.best)

        self._cancel_timer()

        if is_new_best and final_score > 0:
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_sfx("wrong")
            play_orc("gameover")

        # Update HUD with final state
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(final_score))
        self.set_key(2, render_hud_lives(0))
        self.set_key(3, render_hud_timer(0.0))
        if is_new_best and final_score > 0:
            self.set_key(4, render_new_best())
        for k in range(5 if (is_new_best and final_score > 0) else 4, 8):
            self.set_key(k, self.img_hud_empty)

        # Clear equation row
        for k in EQ_KEYS:
            self.set_key(k, self.img_eq_empty)

        # Game over display on mid row
        for k in MID_KEYS:
            if k == START_KEY:
                self.set_key(k, self.img_start)
            elif k in (18, 19):
                self.set_key(k, render_game_over())
            elif k == 21:
                self.set_key(k, render_final_score(final_score))
            elif k == 22:
                self.set_key(k, render_best_score(self.best, is_new_best))
            else:
                self.set_key(k, self.img_mid_empty)

        # Clear options row
        for k in OPT_KEYS:
            self.set_key(k, self.img_mid_empty)

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        # Check if an answer key was pressed
        if key not in ANSWER_KEYS:
            return

        with self.lock:
            if not self.accepting_input:
                return
            self.accepting_input = False
            option_idx = ANSWER_KEYS.index(key)
            is_correct = (option_idx == self.correct_index)

        self._cancel_timer()

        if is_correct:
            # Correct answer!
            with self.lock:
                self.score += 1
                current_score = self.score

            play_sfx("correct")
            # Play orc voice every 5 correct answers
            if current_score % 5 == 0:
                play_orc("correct")

            # Flash correct
            self.set_key(key, render_option_correct(self.options[option_idx]))
            self._update_hud()

            def _after_correct():
                time.sleep(0.5)
                self._next_question()

            threading.Thread(target=_after_correct, daemon=True).start()

        else:
            # Wrong answer — lose a life
            with self.lock:
                self.lives -= 1
                lives_left = self.lives

            play_sfx("wrong")

            # Flash wrong on pressed key
            self.set_key(key, render_option_wrong(self.options[option_idx]))
            # Show correct answer
            correct_key = ANSWER_KEYS[self.correct_index]
            self.set_key(correct_key, render_option_correct(self.options[self.correct_index]))
            self._update_hud()

            def _after_wrong():
                time.sleep(1.0)
                if lives_left <= 0:
                    self._game_over()
                else:
                    self._next_question()

            threading.Thread(target=_after_wrong, daemon=True).start()


# ── main ─────────────────────────────────────────────────────────────

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
    print("QUICK MATH! Press the center button to start.")

    game = QuickMathGame(deck)
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
