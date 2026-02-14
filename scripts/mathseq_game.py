"""Math Sequence — Stream Deck mini-game.

Find the next number in the sequence! Sequences get harder each level.
Row 1 = HUD, Row 2-3 = sequence numbers, Row 4 = answer options.

Usage:
    uv run python scripts/mathseq_game.py
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
HUD_KEYS = list(range(0, 8))        # row 1 = HUD
SEQ_ROW2 = list(range(8, 16))       # row 2 = sequence numbers
SEQ_ROW3 = list(range(16, 24))      # row 3 = overflow / empty
OPTION_KEYS = [26, 27, 28, 29]      # row 4 center = answer options
GAME_KEYS = SEQ_ROW2 + SEQ_ROW3 + list(range(24, 32))
SIZE = (96, 96)

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
MAX_LIVES = 3
START_KEY = 20

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [
        "peasant/sounds/PeasantReady1.wav",
        "peasant/sounds/PeasantWhat1.wav",
        "peasant/sounds/PeasantWhat2.wav",
    ],
    "correct": [
        "peasant/sounds/PeasantYes1.wav",
        "peasant/sounds/PeasantYes2.wav",
        "peasant/sounds/PeasantYes3.wav",
    ],
    "gameover": [
        "peasant/sounds/PeasantAngry1.wav",
        "peasant/sounds/PeasantAngry2.wav",
        "peasant/sounds/PeasantAngry3.wav",
    ],
    "newbest": [
        "peasant/sounds/PeasantYesAttack1.wav",
        "peasant/sounds/PeasantYesAttack2.wav",
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
    _sfx_dir = tempfile.mkdtemp(prefix="mathseq-sfx-")
    v = SFX_VOLUME

    # CORRECT -- happy rising two-note (E5 -> G5)
    s = _triangle(659, 0.08, v * 0.5) + _triangle(784, 0.12, v * 0.6)
    _write_wav(os.path.join(_sfx_dir, "correct.wav"), s)
    _sfx_cache["correct"] = os.path.join(_sfx_dir, "correct.wav")

    # WRONG -- sad descending (A4 -> E4)
    s = _square(440, 0.1, v * 0.35, 0.5) + _square(330, 0.15, v * 0.3, 0.5)
    _write_wav(os.path.join(_sfx_dir, "wrong.wav"), s)
    _sfx_cache["wrong"] = os.path.join(_sfx_dir, "wrong.wav")

    # START -- quick rising arpeggio (C5 -> E5 -> G5)
    s = (_triangle(523, 0.06, v * 0.4) +
         _triangle(659, 0.06, v * 0.45) +
         _triangle(784, 0.10, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # NEWBEST -- victory jingle (C5 -> E5 -> G5 -> C6)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) +
         _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "newbest.wav"), s)
    _sfx_cache["newbest"] = os.path.join(_sfx_dir, "newbest.wav")

    # LEVEL_UP -- rising fanfare (G4 -> B4 -> D5 -> G5)
    s = (_triangle(392, 0.06, v * 0.4) +
         _triangle(494, 0.06, v * 0.45) +
         _triangle(587, 0.06, v * 0.5) +
         _triangle(784, 0.15, v * 0.55))
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


# -- sequence generation ---------------------------------------------------

def _generate_sequence(level: int) -> tuple[list[int], int, list[int]]:
    """Generate a number sequence puzzle.

    Returns: (visible_numbers, correct_answer, options_list_of_4)
    """
    if level <= 3:
        # Simple arithmetic: +step
        step = random.choice([2, 3, 4, 5, 6, 7])
        start = random.randint(1, 20)
        length = random.randint(5, 6)
        seq = [start + step * i for i in range(length + 1)]
        visible = seq[:-1]
        answer = seq[-1]

    elif level <= 6:
        # Geometric: *multiplier
        mult = random.choice([2, 3])
        start = random.choice([1, 2, 3])
        length = random.randint(4, 5)
        seq = [start]
        for _ in range(length):
            seq.append(seq[-1] * mult)
        visible = seq[:-1]
        answer = seq[-1]

    elif level <= 9:
        # Fibonacci-like, squares, triangular
        kind = random.choice(["fib", "squares", "triangular"])
        if kind == "fib":
            a, b = random.choice([(1, 1), (1, 2), (2, 3), (1, 3)])
            seq = [a, b]
            for _ in range(5):
                seq.append(seq[-1] + seq[-2])
            visible = seq[:-1]
            answer = seq[-1]
        elif kind == "squares":
            start_n = random.randint(1, 4)
            seq = [(start_n + i) ** 2 for i in range(7)]
            visible = seq[:-1]
            answer = seq[-1]
        else:
            # Triangular: n*(n+1)/2
            start_n = random.randint(1, 3)
            seq = [(start_n + i) * (start_n + i + 1) // 2 for i in range(7)]
            visible = seq[:-1]
            answer = seq[-1]

    elif level <= 12:
        # Two-step patterns: differences increase (+1, +2, +3, +4, ...)
        kind = random.choice(["inc_diff", "dec_diff", "alternating"])
        if kind == "inc_diff":
            start = random.randint(1, 10)
            inc_start = random.randint(1, 3)
            seq = [start]
            d = inc_start
            for _ in range(6):
                seq.append(seq[-1] + d)
                d += 1
            visible = seq[:-1]
            answer = seq[-1]
        elif kind == "dec_diff":
            start = random.randint(50, 100)
            dec_start = random.randint(2, 5)
            seq = [start]
            d = dec_start
            for _ in range(6):
                seq.append(seq[-1] - d)
                d += 1
            visible = seq[:-1]
            answer = seq[-1]
        else:
            # Alternating +a, +b
            a_step = random.randint(2, 5)
            b_step = random.randint(1, 4)
            start = random.randint(1, 10)
            seq = [start]
            for i in range(6):
                step = a_step if i % 2 == 0 else b_step
                seq.append(seq[-1] + step)
            visible = seq[:-1]
            answer = seq[-1]

    else:
        # Level 13+: mixed harder patterns
        kind = random.choice([
            "multiply_add", "power_seq", "double_step", "cubic",
        ])
        if kind == "multiply_add":
            # e.g., *2+1
            mult = random.choice([2, 3])
            add = random.choice([1, -1, 2])
            start = random.randint(1, 5)
            seq = [start]
            for _ in range(5):
                seq.append(seq[-1] * mult + add)
            visible = seq[:-1]
            answer = seq[-1]
        elif kind == "power_seq":
            base = random.choice([2, 3])
            offset = random.randint(0, 3)
            seq = [base ** i + offset for i in range(7)]
            visible = seq[:-1]
            answer = seq[-1]
        elif kind == "double_step":
            # Differences double: +2, +4, +8, +16
            start = random.randint(1, 10)
            d = random.choice([1, 2, 3])
            seq = [start]
            for _ in range(6):
                seq.append(seq[-1] + d)
                d *= 2
            visible = seq[:-1]
            answer = seq[-1]
        else:
            # Cubic: n^3
            start_n = random.randint(1, 3)
            seq = [(start_n + i) ** 3 for i in range(6)]
            visible = seq[:-1]
            answer = seq[-1]

    # Clamp visible length to max 7 to fit rows 2-3
    if len(visible) > 7:
        visible = visible[-7:]

    # Generate 3 wrong options near the correct answer
    wrong = set()
    spread = max(3, abs(answer) // 4 + 1)
    attempts = 0
    while len(wrong) < 3 and attempts < 100:
        offset = random.randint(1, spread)
        candidate = answer + random.choice([-1, 1]) * offset
        if candidate != answer and candidate not in wrong:
            wrong.add(candidate)
        attempts += 1
    # Fallback if not enough
    while len(wrong) < 3:
        wrong.add(answer + len(wrong) + 1)

    options = [answer] + list(wrong)
    random.shuffle(options)

    return visible, answer, options


# -- game class ------------------------------------------------------------

class MathSeqGame:
    def __init__(self, deck):
        self.deck = deck
        self.level = 1
        self.score = 0
        self.lives = MAX_LIVES
        self.best = scores.load_best("mathseq")
        self.running = False
        self.lock = threading.Lock()
        self.accepting_input = True

        # Current round state
        self.visible: list[int] = []
        self.answer: int = 0
        self.options: list[int] = []
        self.option_key_map: dict[int, int] = {}  # key -> option value

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    # -- renderers ---------------------------------------------------------

    def _render_hud_title(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 34), "MATH", font=_font(16), fill="#f59e0b", anchor="mm")
        d.text((48, 58), "SEQ", font=_font(14), fill="#fbbf24", anchor="mm")
        return img

    def _render_hud_level(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 20), "LEVEL", font=_font(14), fill="#9ca3af", anchor="mt")
        d.text((48, 52), str(self.level), font=_font(28), fill="#60a5fa", anchor="mt")
        return img

    def _render_hud_score(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
        d.text((48, 52), str(self.score), font=_font(28), fill="#34d399", anchor="mt")
        return img

    def _render_hud_lives(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 20), "LIVES", font=_font(14), fill="#9ca3af", anchor="mt")
        hearts = "\u2665" * self.lives + "\u2661" * (MAX_LIVES - self.lives)
        clr = "#ef4444" if self.lives == 1 else "#f87171" if self.lives == 2 else "#fb923c"
        d.text((48, 52), hearts, font=_font(22), fill=clr, anchor="mt")
        return img

    def _render_hud_best(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
        label = str(self.best) if self.best > 0 else "--"
        d.text((48, 52), label, font=_font(28), fill="#34d399", anchor="mt")
        return img

    def _render_hud_empty(self) -> Image.Image:
        return Image.new("RGB", SIZE, "#111827")

    def _render_seq_number(self, number: int) -> Image.Image:
        """Render a sequence number on dark slate background."""
        img = Image.new("RGB", SIZE, "#1e293b")
        d = ImageDraw.Draw(img)
        d.rectangle([3, 3, 92, 92], outline="#334155", width=1)
        text = str(number)
        fs = 30 if len(text) <= 3 else 22 if len(text) <= 5 else 16
        d.text((48, 48), text, font=_font(fs), fill="white", anchor="mm")
        return img

    def _render_question_mark(self) -> Image.Image:
        """Render the '?' placeholder for the missing number."""
        img = Image.new("RGB", SIZE, "#374151")
        d = ImageDraw.Draw(img)
        d.rectangle([3, 3, 92, 92], outline="#4b5563", width=2)
        d.text((48, 48), "?", font=_font(40), fill="#fbbf24", anchor="mm")
        return img

    def _render_option(self, number: int) -> Image.Image:
        """Render an answer option button."""
        img = Image.new("RGB", SIZE, "#065f46")
        d = ImageDraw.Draw(img)
        d.rectangle([3, 3, 92, 92], outline="#34d399", width=2)
        text = str(number)
        fs = 30 if len(text) <= 3 else 22 if len(text) <= 5 else 16
        d.text((48, 48), text, font=_font(fs), fill="white", anchor="mm")
        return img

    def _render_correct_flash(self, number: int) -> Image.Image:
        """Green flash for correct answer."""
        img = Image.new("RGB", SIZE, "#22c55e")
        d = ImageDraw.Draw(img)
        d.rectangle([3, 3, 92, 92], outline="white", width=3)
        text = str(number)
        fs = 30 if len(text) <= 3 else 22 if len(text) <= 5 else 16
        d.text((48, 48), text, font=_font(fs), fill="white", anchor="mm")
        return img

    def _render_wrong_flash(self, number: int) -> Image.Image:
        """Red flash for wrong answer."""
        img = Image.new("RGB", SIZE, "#dc2626")
        d = ImageDraw.Draw(img)
        d.rectangle([3, 3, 92, 92], outline="white", width=3)
        text = str(number)
        fs = 30 if len(text) <= 3 else 22 if len(text) <= 5 else 16
        d.text((48, 48), text, font=_font(fs), fill="white", anchor="mm")
        return img

    def _render_empty(self) -> Image.Image:
        return Image.new("RGB", SIZE, "#0f172a")

    def _render_start_btn(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#065f46")
        d = ImageDraw.Draw(img)
        d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
        d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
        return img

    def _render_game_over_tile(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#7c2d12")
        d = ImageDraw.Draw(img)
        d.text((48, 34), "GAME", font=_font(16), fill="white", anchor="mm")
        d.text((48, 58), "OVER", font=_font(16), fill="#fca5a5", anchor="mm")
        return img

    def _render_new_best(self) -> Image.Image:
        img = Image.new("RGB", SIZE, "#111827")
        d = ImageDraw.Draw(img)
        d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
        d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
        return img

    # -- HUD update --------------------------------------------------------

    def _update_hud(self):
        self.set_key(0, self._render_hud_title())
        self.set_key(1, self._render_hud_level())
        self.set_key(2, self._render_hud_score())
        self.set_key(3, self._render_hud_lives())
        for k in range(4, 8):
            self.set_key(k, self._render_hud_empty())

    # -- display round -----------------------------------------------------

    def _display_round(self):
        """Lay out the current sequence and options on the deck."""
        self._update_hud()

        # Sequence items: visible numbers + "?" at the end
        total_items = len(self.visible) + 1  # +1 for "?"

        # Determine which keys to use for sequence
        # Row 2 (8-15) = up to 8 items, overflow to row 3 (16-23)
        all_seq_keys = SEQ_ROW2 + SEQ_ROW3  # 16 keys max

        # Center the sequence across available keys
        # If total_items <= 8, use row 2 only (centered)
        # If total_items > 8, use both rows
        if total_items <= 8:
            pad = (8 - total_items) // 2
            used_keys = SEQ_ROW2[pad:pad + total_items]
            # Clear unused row 2 keys
            for k in SEQ_ROW2:
                if k not in used_keys:
                    self.set_key(k, self._render_empty())
            # Clear all row 3 keys
            for k in SEQ_ROW3:
                self.set_key(k, self._render_empty())
        else:
            # Fill row 2, overflow into row 3
            used_keys = SEQ_ROW2[:8] + SEQ_ROW3[:total_items - 8]
            for k in SEQ_ROW2 + SEQ_ROW3:
                if k not in used_keys:
                    self.set_key(k, self._render_empty())

        # Draw sequence numbers
        for i, k in enumerate(used_keys):
            if i < len(self.visible):
                self.set_key(k, self._render_seq_number(self.visible[i]))
            else:
                self.set_key(k, self._render_question_mark())

        # Row 4: answer options at keys 26-29, rest empty
        self.option_key_map = {}
        for i, k in enumerate(OPTION_KEYS):
            val = self.options[i]
            self.option_key_map[k] = val
            self.set_key(k, self._render_option(val))

        # Clear non-option keys in row 4
        for k in range(24, 32):
            if k not in OPTION_KEYS:
                self.set_key(k, self._render_empty())

    # -- idle / start ------------------------------------------------------

    def show_idle(self):
        """Show start screen."""
        self.running = False

        # HUD
        self.set_key(0, self._render_hud_title())
        self.set_key(1, self._render_hud_level())
        self.set_key(2, self._render_hud_score())
        self.set_key(3, self._render_hud_best())
        for k in range(4, 8):
            self.set_key(k, self._render_hud_empty())

        # Game area -- all empty, start button at START_KEY
        for k in GAME_KEYS:
            if k == START_KEY:
                self.set_key(k, self._render_start_btn())
            else:
                self.set_key(k, self._render_empty())

    def start_game(self):
        """Begin a new game from level 1."""
        with self.lock:
            self.level = 1
            self.score = 0
            self.lives = MAX_LIVES
            self.running = True
            self.accepting_input = True

        play_sfx("start")
        play_orc("start")
        self._next_round()

    def _next_round(self):
        """Generate and display the next sequence puzzle."""
        with self.lock:
            self.visible, self.answer, self.options = _generate_sequence(self.level)
            self.accepting_input = True
        self._display_round()

    # -- answer handling ---------------------------------------------------

    def _handle_correct(self, key: int):
        """Player chose the correct answer."""
        self.set_key(key, self._render_correct_flash(self.answer))
        play_sfx("correct")
        play_orc("correct")

        with self.lock:
            self.score += 1
            # Level up every 3 correct answers
            if self.score % 3 == 0:
                self.level += 1

        # Brief flash then next round
        def _advance():
            time.sleep(0.6)
            if self.score % 3 == 0:
                play_sfx("level_up")
                time.sleep(0.3)
            self._next_round()

        threading.Thread(target=_advance, daemon=True).start()

    def _handle_wrong(self, key: int, chosen_val: int):
        """Player chose a wrong answer."""
        self.set_key(key, self._render_wrong_flash(chosen_val))
        play_sfx("wrong")

        with self.lock:
            self.lives -= 1
            lives_left = self.lives

        self._update_hud()

        if lives_left <= 0:
            self._game_over()
        else:
            # Show the correct answer briefly, then next round
            def _recover():
                time.sleep(0.5)
                # Highlight correct answer in green
                for ok, ov in self.option_key_map.items():
                    if ov == self.answer:
                        self.set_key(ok, self._render_correct_flash(self.answer))
                        break
                time.sleep(0.8)
                self._next_round()

            threading.Thread(target=_recover, daemon=True).start()

    def _game_over(self):
        """Handle game over."""
        self.running = False
        is_new_best = self.best == 0 or self.score > self.best

        if is_new_best:
            self.best = self.score
            scores.save_best("mathseq", self.best)
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_orc("gameover")

        def _show_game_over():
            time.sleep(0.5)

            # Update HUD with best
            self.set_key(0, self._render_hud_title())
            self.set_key(1, self._render_hud_level())
            self.set_key(2, self._render_hud_score())
            self.set_key(3, self._render_hud_best())
            if is_new_best:
                self.set_key(4, self._render_new_best())
                for k in range(5, 8):
                    self.set_key(k, self._render_hud_empty())
            else:
                for k in range(4, 8):
                    self.set_key(k, self._render_hud_empty())

            # Game area: game over tiles + restart
            for k in GAME_KEYS:
                if k == START_KEY:
                    self.set_key(k, self._render_start_btn())
                elif k in (19, 20, 21):
                    self.set_key(k, self._render_game_over_tile())
                else:
                    self.set_key(k, self._render_empty())

        threading.Thread(target=_show_game_over, daemon=True).start()

    # -- key callback ------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        # Only respond to option keys
        if key not in OPTION_KEYS:
            return

        with self.lock:
            if not self.accepting_input:
                return
            self.accepting_input = False
            chosen_val = self.option_key_map.get(key)
            correct_answer = self.answer

        if chosen_val is None:
            return

        if chosen_val == correct_answer:
            self._handle_correct(key)
        else:
            self._handle_wrong(key, chosen_val)


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
    print("MATH SEQUENCE! Press button 20 to start.")

    game = MathSeqGame(deck)
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
