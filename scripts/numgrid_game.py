"""Number Grid — Stream Deck mini-game.

Find the missing number in a 3x3 grid where every row and column
sums to the same target. Like a mini Sudoku/KenKen.

Layout (4x8, buttons 0-31):
  Row 1 (0-7):   HUD — title, level, score, lives, col-sum hints (5,6,7)
  Row 2 (8-15):  Grid row 1 — _  N  N  N  ->  SUM  _  _
  Row 3 (16-23): Grid row 2 — _  N  N  ?  ->  SUM  _  _
  Row 4 (24-31): Options     — _  _  O1 O2 O3 O4  _  _

Usage:
    uv run python scripts/numgrid_game.py
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

# -- config ----------------------------------------------------------------
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

HUD_KEYS = list(range(0, 8))

START_KEY = 20  # center-ish button for START
MAX_LIVES = 3

# Layout:
#   Row 1 (0-7):   title(0), level(1), score(2), lives(3), _(4),
#                   col_hint_0(5), col_hint_1(6), col_hint_2(7)
#   Row 2 (8-15):  _(8), N(9), N(10), N(11), =(12), SUM(13), _(14), _(15)
#   Row 3 (16-23): _(16), N(17), N(18), ?(19), =(20), SUM(21), _(22), _(23)
#   Row 4 (24-31): _(24), N(25), N(26), N(27), Opt1(28), Opt2(29), Opt3(30), Opt4(31)
GRID_POS = [
    [9, 10, 11],    # grid row 0
    [17, 18, 19],   # grid row 1
    [25, 26, 27],   # grid row 2
]
ARROW_POS = [12, 20]       # "=" for grid rows 0 and 1 only
ROWSUM_POS = [13, 21]      # row sum for grid rows 0 and 1
COLSUM_HUD = [5, 6, 7]     # column sum hints
OPTION_BTNS = [28, 29, 30, 31]  # answer option buttons


# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "sc_kerrigan/sounds/ImReady.mp3",
        "sc_kerrigan/sounds/WaitingOnYou.mp3",
    ],
    "correct": [
        "sc_kerrigan/sounds/IGotcha.mp3",
        "sc_kerrigan/sounds/BeAPleasure.mp3",
    ],
    "gameover": [
        "sc_kerrigan/sounds/Death1.mp3",
        "sc_kerrigan/sounds/AnnoyingPeople.mp3",
    ],
    "newbest": [
        "sc_kerrigan/sounds/EasilyAmused.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="numgrid-sfx-")
    v = SFX_VOLUME

    # CORRECT -- happy rising (C5 -> E5 -> G5)
    s = (
        _triangle(523, 0.06, v * 0.5)
        + _triangle(659, 0.06, v * 0.55)
        + _triangle(784, 0.1, v * 0.6)
    )
    _write_wav(os.path.join(_sfx_dir, "correct.wav"), s)
    _sfx_cache["correct"] = os.path.join(_sfx_dir, "correct.wav")

    # WRONG -- sad descending (A4 -> E4 -> C4)
    s = (
        _square(440, 0.08, v * 0.4, 0.5)
        + _square(330, 0.10, v * 0.35, 0.5)
        + _square(262, 0.14, v * 0.3, 0.5)
    )
    _write_wav(os.path.join(_sfx_dir, "wrong.wav"), s)
    _sfx_cache["wrong"] = os.path.join(_sfx_dir, "wrong.wav")

    # START -- short bright blip (C5 -> G5)
    s = _triangle(523, 0.06, v * 0.5) + _triangle(784, 0.08, v * 0.6)
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # NEWBEST -- victory jingle (C5 -> E5 -> G5 -> C6)
    s = (
        _triangle(523, 0.08, v * 0.5)
        + _triangle(659, 0.08, v * 0.55)
        + _triangle(784, 0.08, v * 0.6)
        + _triangle(1047, 0.25, v * 0.7)
    )
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


# -- puzzle generation -----------------------------------------------------

def _generate_puzzle(level: int):
    """Generate a 3x3 magic-sum grid puzzle.

    Returns:
        (grid, missing_row, missing_col, correct_answer, options)
        - grid: 3x3 list of ints (the full solved grid)
        - missing_row: row index of hidden cell (0-2)
        - missing_col: col index of hidden cell (0-2)
        - correct_answer: the value of the hidden cell
        - options: list of 4 ints including the correct answer (shuffled)
    """
    # Determine number range based on level
    if level <= 3:
        lo, hi = 1, 5
    elif level <= 6:
        lo, hi = 1, 9
    elif level <= 9:
        lo, hi = 1, 15
    else:
        lo, hi = 1, 20

    # Strategy: build a valid 3x3 grid where all rows and columns sum to S.
    # Method: pick row1 freely, then constrain row2 and row3.
    #
    # Let row sums all equal S.  col sums all equal S too.
    # This means the grand total = 3*S (from rows) = 3*S (from cols), consistent.
    #
    # Pick S first, then fill:
    #   row0 = [a, b, c]  where a+b+c = S
    #   row1 = [d, e, f]  where d+e+f = S
    #   row2 = [S-a-d, S-b-e, S-c-f]  — forced, must all be positive and in range
    #
    # We retry until we get valid numbers.

    for _attempt in range(200):
        # Pick target sum S
        # S must be achievable: 3*lo <= S <= 3*hi
        s_lo = 3 * lo
        s_hi = 3 * hi
        target = random.randint(s_lo, s_hi)

        # Generate row 0: three numbers in [lo, hi] summing to target
        r0 = _random_partition(target, 3, lo, hi)
        if r0 is None:
            continue

        # Generate row 1: three numbers in [lo, hi] summing to target
        r1 = _random_partition(target, 3, lo, hi)
        if r1 is None:
            continue

        # Derive row 2
        r2 = [target - r0[j] - r1[j] for j in range(3)]

        # Validate row 2
        if any(v < lo or v > hi for v in r2):
            continue

        # Verify row 2 sums to target (should be guaranteed by construction)
        if sum(r2) != target:
            continue

        grid = [r0, r1, r2]

        # Pick which cell to hide
        missing_row = random.randint(0, 2)
        missing_col = random.randint(0, 2)
        correct = grid[missing_row][missing_col]

        # Generate 3 distractors (different from correct, in range)
        distractors = set()
        # Nearby values are good distractors
        nearby = [correct - 2, correct - 1, correct + 1, correct + 2,
                  correct - 3, correct + 3]
        for v in nearby:
            if lo <= v <= hi and v != correct:
                distractors.add(v)
        # Add random distractors if needed
        all_vals = list(range(lo, hi + 1))
        random.shuffle(all_vals)
        for v in all_vals:
            if v != correct:
                distractors.add(v)
            if len(distractors) >= 6:
                break

        distractor_list = list(distractors)
        random.shuffle(distractor_list)
        chosen = distractor_list[:3]

        options = [correct] + chosen
        random.shuffle(options)

        return grid, missing_row, missing_col, correct, options, target

    # Fallback: trivial puzzle
    grid = [[1, 2, 3], [3, 1, 2], [2, 3, 1]]
    return grid, 1, 2, 2, [2, 4, 1, 3], 6


def _random_partition(total: int, n: int, lo: int, hi: int):
    """Return a list of n random ints in [lo, hi] that sum to total, or None."""
    if n * lo > total or n * hi < total:
        return None

    # Simple rejection sampling with guided approach
    for _attempt in range(50):
        vals = []
        remaining = total
        for i in range(n - 1):
            slots_left = n - i
            # Bounds for this value
            min_v = max(lo, remaining - (slots_left - 1) * hi)
            max_v = min(hi, remaining - (slots_left - 1) * lo)
            if min_v > max_v:
                break
            v = random.randint(min_v, max_v)
            vals.append(v)
            remaining -= v
        else:
            if lo <= remaining <= hi:
                vals.append(remaining)
                return vals
    return None


# -- renderers -------------------------------------------------------------

def render_grid_number(num: int, size=SIZE) -> Image.Image:
    """Grid cell with a number — white text on dark blue."""
    img = Image.new("RGB", size, "#1e3a5f")
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#2d5a8a", width=2)
    fs = 38 if num < 10 else 30 if num < 100 else 24
    d.text((48, 48), str(num), font=_font(fs), fill="white", anchor="mm")
    return img


def render_question_cell(size=SIZE) -> Image.Image:
    """The hidden cell — yellow '?' on dark gray."""
    img = Image.new("RGB", size, "#374151")
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#6b7280", width=2)
    d.text((48, 48), "?", font=_font(44), fill="#fbbf24", anchor="mm")
    return img


def render_equals(size=SIZE) -> Image.Image:
    """Arrow/equals sign between grid and sum."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "=", font=_font(30), fill="#6b7280", anchor="mm")
    return img


def render_row_sum(target: int, size=SIZE) -> Image.Image:
    """Row sum display — white number on green."""
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#059669", width=2)
    fs = 32 if target < 100 else 24
    d.text((48, 48), str(target), font=_font(fs), fill="white", anchor="mm")
    return img


def render_col_hint(col_idx: int, target: int, size=SIZE) -> Image.Image:
    """Column sum hint on HUD row — small text."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    # Down arrow and sum
    d.text((48, 18), "\u2193", font=_font(18), fill="#6b7280", anchor="mt")
    fs = 24 if target < 100 else 18
    d.text((48, 50), str(target), font=_font(fs), fill="#a78bfa", anchor="mt")
    return img


def render_option(num: int, size=SIZE) -> Image.Image:
    """Answer option button — white number on teal."""
    img = Image.new("RGB", size, "#0d9488")
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([5, 5, 90, 90], radius=10, fill="#0d9488",
                        outline="white", width=2)
    fs = 36 if num < 10 else 28 if num < 100 else 22
    d.text((48, 48), str(num), font=_font(fs), fill="white", anchor="mm")
    return img


def render_correct_flash(size=SIZE) -> Image.Image:
    """Green flash for correct answer."""
    img = Image.new("RGB", size, "#22c55e")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "\u2713", font=_font(40), fill="white", anchor="mm")
    return img


def render_wrong_flash(size=SIZE) -> Image.Image:
    """Red flash for wrong answer."""
    img = Image.new("RGB", size, "#ef4444")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "\u2717", font=_font(40), fill="white", anchor="mm")
    return img


def render_empty(size=SIZE) -> Image.Image:
    """Empty dark cell."""
    return Image.new("RGB", size, "#111827")


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NUM", font=_font(15), fill="#60a5fa", anchor="mt")
    d.text((48, 52), "GRID", font=_font(15), fill="#60a5fa", anchor="mt")
    return img


def render_hud_text(line1: str, line2: str, bg: str = "#111827",
                    c1: str = "#9ca3af", c2: str = "#ffffff",
                    s2: int = 32, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 20), line1, font=_font(14), fill=c1, anchor="mt")
    d.text((48, 52), line2, font=_font(s2), fill=c2, anchor="mt")
    return img


def render_lives(lives: int, size=SIZE) -> Image.Image:
    """Show remaining lives as hearts."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 18), "LIVES", font=_font(14), fill="#9ca3af", anchor="mt")
    hearts = "\u2764" * lives + "\u2661" * (MAX_LIVES - lives)
    clr = "#ef4444" if lives > 1 else "#fca5a5"
    d.text((48, 52), hearts, font=_font(20), fill=clr, anchor="mt")
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


def render_new_best(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
    return img


def render_reveal_answer(num: int, size=SIZE) -> Image.Image:
    """Reveal the correct answer in the '?' cell — gold on dark blue."""
    img = Image.new("RGB", size, "#1e3a5f")
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#fbbf24", width=3)
    fs = 38 if num < 10 else 30 if num < 100 else 24
    d.text((48, 48), str(num), font=_font(fs), fill="#fbbf24", anchor="mm")
    return img


# -- game logic ------------------------------------------------------------

class NumGridGame:
    def __init__(self, deck):
        self.deck = deck
        self.level = 0
        self.score = 0
        self.lives = MAX_LIVES
        self.best = scores.load_best("numgrid")
        self.state = "idle"  # idle | playing | gameover
        self.lock = threading.Lock()
        self.accepting_input = False

        # Current puzzle state
        self.grid: list[list[int]] = []
        self.missing_row = 0
        self.missing_col = 0
        self.correct_answer = 0
        self.options: list[int] = []
        self.target_sum = 0

        # Map option button -> value
        self.option_map: dict[int, int] = {}

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _update_hud(self):
        self.set_key(0, render_hud_title())
        self.set_key(1, render_hud_text("LEVEL", str(self.level)))
        self.set_key(2, render_hud_text("SCORE", str(self.score), c2="#60a5fa"))
        self.set_key(3, render_lives(self.lives))
        self.set_key(4, render_empty())
        # Column sum hints at 5, 6, 7
        for ci in range(3):
            self.set_key(COLSUM_HUD[ci], render_col_hint(ci, self.target_sum))

    def _clear_all_game(self):
        """Clear rows 2-4."""
        for k in range(8, 32):
            self.set_key(k, render_empty())

    def _draw_puzzle(self):
        """Draw the current grid puzzle and answer options."""
        grid = self.grid
        mr, mc = self.missing_row, self.missing_col

        # Draw grid rows
        for r in range(3):
            for c in range(3):
                btn = GRID_POS[r][c]
                if r == mr and c == mc:
                    self.set_key(btn, render_question_cell())
                else:
                    self.set_key(btn, render_grid_number(grid[r][c]))

        # Draw "=" and row sums for rows 0 and 1
        for ri in range(2):
            self.set_key(ARROW_POS[ri], render_equals())
            self.set_key(ROWSUM_POS[ri], render_row_sum(self.target_sum))

        # Draw answer options at buttons 28-31 (right side of row 4)
        self.option_map.clear()
        for i, val in enumerate(self.options):
            btn = OPTION_BTNS[i]
            self.set_key(btn, render_option(val))
            self.option_map[btn] = val

        # Clear unused cells
        for btn in [8, 14, 15, 16, 22, 23, 24]:
            self.set_key(btn, render_empty())

    def show_idle(self):
        """Show start screen."""
        self.state = "idle"
        # HUD
        self.set_key(0, render_hud_title())
        self.set_key(1, render_hud_text("LEVEL", "0"))
        self.set_key(2, render_hud_text("SCORE", "0", c2="#60a5fa"))
        self.set_key(3, render_hud_text("BEST", str(self.best), c2="#34d399"))
        for k in range(4, 8):
            self.set_key(k, render_empty())
        # Game area -- all empty except start button
        for k in range(8, 32):
            if k == START_KEY:
                self.set_key(k, render_start())
            else:
                self.set_key(k, render_empty())

    def start_game(self):
        """Start a new game."""
        with self.lock:
            self.level = 0
            self.score = 0
            self.lives = MAX_LIVES
            self.state = "playing"
            self.accepting_input = False

        play_sfx("start")
        play_orc("start")
        self._update_hud()

        # Start first round
        threading.Thread(target=self._next_round, daemon=True).start()

    def _next_round(self):
        """Generate and display the next puzzle."""
        with self.lock:
            self.level += 1
            self.accepting_input = False

        self._update_hud()
        self._clear_all_game()

        # Brief pause before showing new puzzle
        time.sleep(0.4)

        # Generate puzzle
        result = _generate_puzzle(self.level)
        grid, mr, mc, correct, options, target = result

        with self.lock:
            self.grid = grid
            self.missing_row = mr
            self.missing_col = mc
            self.correct_answer = correct
            self.options = options
            self.target_sum = target

        # Update column hints now that we know the target
        for ci in range(3):
            self.set_key(COLSUM_HUD[ci], render_col_hint(ci, target))

        # Draw the puzzle
        self._draw_puzzle()

        with self.lock:
            self.accepting_input = True

    def _handle_correct(self):
        """Handle correct answer."""
        with self.lock:
            self.score += 1
            self.accepting_input = False

        play_sfx("correct")

        # Flash the question cell green
        btn = GRID_POS[self.missing_row][self.missing_col]
        self.set_key(btn, render_correct_flash())

        self._update_hud()

        # Orc voice every 3 correct
        if self.score % 3 == 0:
            play_orc("correct")

        time.sleep(0.6)

        # Show the number briefly
        self.set_key(btn, render_grid_number(self.correct_answer))
        time.sleep(0.3)

        # Next round
        threading.Thread(target=self._next_round, daemon=True).start()

    def _handle_wrong(self):
        """Handle wrong answer."""
        with self.lock:
            self.lives -= 1
            remaining = self.lives
            self.accepting_input = False

        play_sfx("wrong")

        # Flash the question cell red
        btn = GRID_POS[self.missing_row][self.missing_col]
        self.set_key(btn, render_wrong_flash())

        self._update_hud()
        time.sleep(0.6)

        if remaining <= 0:
            self._game_over()
        else:
            # Show the correct answer briefly, then next round
            self.set_key(btn, render_reveal_answer(self.correct_answer))
            time.sleep(0.7)
            threading.Thread(target=self._next_round, daemon=True).start()

    def _game_over(self):
        """Handle game over."""
        with self.lock:
            self.state = "gameover"
            new_best = self.score > self.best

        if new_best:
            self.best = self.score
            scores.save_best("numgrid", self.best)
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_orc("gameover")

        # Update HUD with final state
        self.set_key(0, render_hud_title())
        self.set_key(1, render_hud_text("LEVEL", str(self.level)))
        self.set_key(2, render_hud_text("SCORE", str(self.score), c2="#60a5fa"))
        self.set_key(3, render_hud_text("BEST", str(self.best), c2="#34d399"))
        if new_best:
            self.set_key(4, render_new_best())
        for k in range(5 if new_best else 4, 8):
            self.set_key(k, render_empty())

        # Show game over + restart
        self._clear_all_game()
        self.set_key(11, render_game_over())
        self.set_key(12, render_game_over())
        self.set_key(START_KEY, render_start())

        # Reveal the full solved grid
        for r in range(3):
            for c in range(3):
                btn = GRID_POS[r][c]
                if r == self.missing_row and c == self.missing_col:
                    self.set_key(btn, render_reveal_answer(self.correct_answer))
                else:
                    self.set_key(btn, render_grid_number(self.grid[r][c]))

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and self.state in ("idle", "gameover"):
            self.start_game()
            return

        if self.state != "playing":
            return

        with self.lock:
            if not self.accepting_input:
                return
            chosen = self.option_map.get(key)

        if chosen is None:
            return

        # Player picked an option
        if chosen == self.correct_answer:
            threading.Thread(target=self._handle_correct, daemon=True).start()
        else:
            threading.Thread(target=self._handle_wrong, daemon=True).start()


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
    print("NUMBER GRID! Press the center button to start.")

    game = NumGridGame(deck)
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
