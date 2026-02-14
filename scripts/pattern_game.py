"""Pattern Logic — Stream Deck mini-game.

Simplified Raven's Progressive Matrices: figure out the color pattern
and pick the missing piece. Patterns get harder as you level up.

Usage:
    uv run python scripts/pattern_game.py
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
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

ROWS = 4
COLS = 8

HUD_KEYS = list(range(0, 8))       # row 1 = HUD
PATTERN_KEYS = list(range(8, 24))   # rows 2-3 = pattern sequence
OPTION_KEYS = list(range(24, 32))   # row 4 = answer options

START_KEY = 20  # center-ish button for START

PATTERN_COLORS = [
    "#ef4444",  # red
    "#3b82f6",  # blue
    "#22c55e",  # green
    "#eab308",  # yellow
    "#a855f7",  # purple
    "#f97316",  # orange
    "#ec4899",  # pink
    "#06b6d4",  # cyan
]

MAX_LIVES = 3

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "duke_nukem/sounds/DaddysHere.mp3",
        "duke_nukem/sounds/KickAssChewGum.mp3",
    ],
    "correct": [
        "duke_nukem/sounds/Groovy.mp3",
        "duke_nukem/sounds/HellYeah.mp3",
    ],
    "gameover": [
        "duke_nukem/sounds/DamnIt.mp3",
    ],
    "newbest": [
        "duke_nukem/sounds/BallsOfSteel.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="pattern-sfx-")
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

    # LEVEL_UP -- ascending arpeggio (E5 -> G5 -> B5 -> E6)
    s = (
        _triangle(659, 0.06, v * 0.5)
        + _triangle(784, 0.06, v * 0.55)
        + _triangle(988, 0.06, v * 0.6)
        + _triangle(1319, 0.15, v * 0.65)
    )
    _write_wav(os.path.join(_sfx_dir, "level_up.wav"), s)
    _sfx_cache["level_up"] = os.path.join(_sfx_dir, "level_up.wav")


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
    """Button position (0-31) -> (row, col) in 4x8 grid."""
    return pos // COLS, pos % COLS


def rc_to_pos(row: int, col: int) -> int:
    """(row, col) in 4x8 grid -> button position (0-31)."""
    return row * COLS + col


# -- pattern generation ----------------------------------------------------

def _generate_pattern(level: int) -> tuple[list[int], int, list[int]]:
    """Generate a pattern puzzle for the given level.

    Returns:
        (sequence, answer, options)
        - sequence: list of color indices (0-7) with exactly one -1 for the '?' cell
        - answer: correct color index to replace the -1
        - options: shuffled list of 3-4 color indices including the answer
    """
    if level <= 3:
        # Simple alternation: A, B, A, B, A, B, A, ?  -> answer B
        num_colors = 2
        colors = random.sample(range(len(PATTERN_COLORS)), num_colors)
        length = random.randint(6, 8)
        full = [colors[i % num_colors] for i in range(length)]
        answer = full[-1]
        sequence = full[:-1] + [-1]

    elif level <= 6:
        # Three-color repeat: A, B, C, A, B, C, A, B, ?  -> answer C
        num_colors = 3
        colors = random.sample(range(len(PATTERN_COLORS)), num_colors)
        length = random.randint(7, 10)
        full = [colors[i % num_colors] for i in range(length)]
        answer = full[-1]
        sequence = full[:-1] + [-1]

    elif level <= 9:
        # Growing pattern: A, A, B, A, A, B, A, A, ?  -> answer B
        # or A, B, B, A, B, B, A, ?  -> answer B
        num_colors = 2
        colors = random.sample(range(len(PATTERN_COLORS)), num_colors)
        # Decide group structure: e.g., [A, A, B] repeated
        group_a = random.randint(1, 3)
        group_b = random.randint(1, 2)
        group = [colors[0]] * group_a + [colors[1]] * group_b
        repeats = random.randint(2, 3)
        full = (group * (repeats + 1))[:16]  # cap to 16 cells max
        # Make sure we have at least 6 elements
        while len(full) < 6:
            full = full + group
        # Trim to fit in 16 cells
        full = full[:16]
        answer = full[-1]
        sequence = full[:-1] + [-1]

    else:
        # Mixed patterns with more colors
        pattern_type = random.choice(["quad_repeat", "mirror", "step"])

        if pattern_type == "quad_repeat":
            # Four-color repeat: A, B, C, D, A, B, C, D, A, ?  -> answer B
            num_colors = 4
            colors = random.sample(range(len(PATTERN_COLORS)), num_colors)
            length = random.randint(9, 13)
            full = [colors[i % num_colors] for i in range(length)]
            answer = full[-1]
            sequence = full[:-1] + [-1]

        elif pattern_type == "mirror":
            # Mirror: A, B, C, B, A, B, C, B, A, ?  -> answer B
            num_colors = 3
            colors = random.sample(range(len(PATTERN_COLORS)), num_colors)
            half = colors[:]
            group = half + half[-2::-1]  # A, B, C, B, A
            repeats = 2
            full = (group * repeats)[:14]
            while len(full) < 6:
                full = full + group
            full = full[:14]
            answer = full[-1]
            sequence = full[:-1] + [-1]

        else:
            # Step: A, A, B, B, C, C, A, A, B, B, ?  -> answer C
            num_colors = random.randint(3, 4)
            colors = random.sample(range(len(PATTERN_COLORS)), num_colors)
            step_len = random.randint(2, 3)
            group = []
            for c in colors:
                group.extend([c] * step_len)
            repeats = 2
            full = (group * repeats)[:16]
            while len(full) < 6:
                full = full + group
            full = full[:16]
            answer = full[-1]
            sequence = full[:-1] + [-1]

    # Trim sequence to max 16 cells (rows 2-3)
    if len(sequence) > 16:
        sequence = sequence[:15] + [-1]
        answer = full[15] if len(full) > 15 else answer

    # Generate options: answer + 2-3 distractors
    num_options = 3 if level <= 5 else 4
    distractor_pool = [i for i in range(len(PATTERN_COLORS)) if i != answer]
    distractors = random.sample(distractor_pool, min(num_options - 1, len(distractor_pool)))
    options = [answer] + distractors
    random.shuffle(options)

    return sequence, answer, options


# -- renderers -------------------------------------------------------------

def render_pattern_cell(color_idx: int, size=SIZE) -> Image.Image:
    """Solid color cell with white border."""
    color = PATTERN_COLORS[color_idx]
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="white", width=2)
    return img


def render_question_cell(size=SIZE) -> Image.Image:
    """Dark gray '?' cell -- the cell the player must solve."""
    img = Image.new("RGB", size, "#374151")
    d = ImageDraw.Draw(img)
    d.rectangle([3, 3, 92, 92], outline="#6b7280", width=2)
    d.text((48, 48), "?", font=_font(44), fill="#d1d5db", anchor="mm")
    return img


def render_option_cell(color_idx: int, size=SIZE) -> Image.Image:
    """Answer option button with subtle glow effect."""
    color = PATTERN_COLORS[color_idx]
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    # Outer glow
    d.rounded_rectangle([6, 6, 89, 89], radius=10, fill=color, outline="white", width=2)
    # Inner highlight for glow effect
    d.rounded_rectangle([12, 12, 83, 83], radius=8, outline="#ffffff40", width=1)
    return img


def render_correct_flash(size=SIZE) -> Image.Image:
    """Green flash for correct answer."""
    img = Image.new("RGB", size, "#22c55e")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "\u2713", font=_font(40), fill="white", anchor="mm")
    return img


def render_wrong_flash(size=SIZE) -> Image.Image:
    """Red flash for wrong answer on the '?' cell."""
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
    d.text((48, 30), "PATTERN", font=_font(13), fill="#a78bfa", anchor="mt")
    d.text((48, 52), "LOGIC", font=_font(13), fill="#a78bfa", anchor="mt")
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


# -- game logic ------------------------------------------------------------

class PatternGame:
    def __init__(self, deck):
        self.deck = deck
        self.level = 0
        self.score = 0
        self.lives = MAX_LIVES
        self.best = scores.load_best("pattern")
        self.state = "idle"  # idle | playing | gameover
        self.lock = threading.Lock()
        self.accepting_input = False

        # Current puzzle state
        self.sequence: list[int] = []     # color indices, -1 = '?'
        self.answer: int = 0              # correct color index
        self.options: list[int] = []      # option color indices
        self.question_pos: int = -1       # button pos of the '?' cell

        # Map option key -> color index for current round
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
        for k in range(4, 8):
            self.set_key(k, render_empty())

    def _clear_pattern_area(self):
        """Clear rows 2-3 (pattern area)."""
        for k in PATTERN_KEYS:
            self.set_key(k, render_empty())

    def _clear_options(self):
        """Clear row 4 (options area)."""
        for k in OPTION_KEYS:
            self.set_key(k, render_empty())

    def _draw_puzzle(self):
        """Draw the current pattern sequence and answer options."""
        # Draw pattern on rows 2-3 (buttons 8-23)
        self._clear_pattern_area()
        for i, color_idx in enumerate(self.sequence):
            pos = 8 + i  # buttons 8..8+len
            if pos > 23:
                break
            if color_idx == -1:
                self.set_key(pos, render_question_cell())
                self.question_pos = pos
            else:
                self.set_key(pos, render_pattern_cell(color_idx))

        # Draw answer options on row 4 (buttons 24-31)
        self._clear_options()
        self.option_map.clear()
        # Center the options in the bottom row
        num_opts = len(self.options)
        start_col = (COLS - num_opts) // 2
        for i, color_idx in enumerate(self.options):
            key = 24 + start_col + i
            self.set_key(key, render_option_cell(color_idx))
            self.option_map[key] = color_idx

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
        # Pattern area -- all empty except start button
        for k in PATTERN_KEYS:
            if k == START_KEY:
                self.set_key(k, render_start())
            else:
                self.set_key(k, render_empty())
        # Options area -- empty
        self._clear_options()

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
        """Generate and display the next pattern puzzle."""
        with self.lock:
            self.level += 1
            self.accepting_input = False

        self._update_hud()
        self._clear_pattern_area()
        self._clear_options()

        # Brief pause before showing new pattern
        time.sleep(0.4)

        # Generate puzzle
        sequence, answer, options = _generate_pattern(self.level)
        with self.lock:
            self.sequence = sequence
            self.answer = answer
            self.options = options
            self.question_pos = -1

        # Draw the puzzle
        self._draw_puzzle()

        # Level-up sound every 3 levels
        if self.level > 1 and (self.level - 1) % 3 == 0:
            play_sfx("level_up")

        with self.lock:
            self.accepting_input = True

    def _handle_correct(self):
        """Handle correct answer."""
        with self.lock:
            self.score += 1
            self.accepting_input = False

        play_sfx("correct")

        # Flash all pattern cells green briefly
        for i in range(len(self.sequence)):
            pos = 8 + i
            if pos > 23:
                break
            self.set_key(pos, render_correct_flash())

        self._update_hud()

        # Orc voice for correct
        if self.score % 3 == 0:
            play_orc("correct")

        time.sleep(0.6)

        # Next round
        threading.Thread(target=self._next_round, daemon=True).start()

    def _handle_wrong(self):
        """Handle wrong answer."""
        with self.lock:
            self.lives -= 1
            remaining = self.lives
            self.accepting_input = False

        play_sfx("wrong")

        # Flash the '?' cell red
        if self.question_pos >= 0:
            self.set_key(self.question_pos, render_wrong_flash())

        self._update_hud()
        time.sleep(0.6)

        if remaining <= 0:
            # Game over
            self._game_over()
        else:
            # Show the correct answer briefly, then next round
            if self.question_pos >= 0:
                self.set_key(self.question_pos, render_pattern_cell(self.answer))
            time.sleep(0.5)
            threading.Thread(target=self._next_round, daemon=True).start()

    def _game_over(self):
        """Handle game over."""
        with self.lock:
            self.state = "gameover"
            new_best = self.score > self.best

        if new_best:
            self.best = self.score
            scores.save_best("pattern", self.best)
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

        # Clear pattern area, show game over + restart
        self._clear_pattern_area()
        self._clear_options()

        self.set_key(11, render_game_over())
        self.set_key(12, render_game_over())
        self.set_key(START_KEY, render_start())

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
            color_idx = self.option_map.get(key)

        if color_idx is None:
            return

        # Player picked an option
        if color_idx == self.answer:
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
    print("PATTERN LOGIC! Press the center button to start.")

    game = PatternGame(deck)
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
