"""Minesweeper — Stream Deck puzzle game.

Classic minesweeper on a 3x8 grid (24 game tiles, Stream Deck XL).
Top row (keys 0-7) is HUD; game area is rows 1-3 (keys 8-31).
Reveal all safe tiles without hitting a mine. First click is always safe.
Tracks solve time; lower is better.

Usage:
    uv run python scripts/mines_game.py
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

import sound_engine
import scores

# -- config ----------------------------------------------------------------
ROWS = 3          # game grid: 3 rows (deck rows 1-3, keys 8-31)
COLS = 8
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
NUM_MINES = 5     # 5 mines out of 24 tiles
ROW_OFFSET = 1    # game row 0 = deck row 1

# -- grid helpers ----------------------------------------------------------

def pos_to_rc(pos):
    """Convert deck key position to game grid (row, col).
    Deck row 1 = game row 0, deck row 2 = game row 1, etc."""
    return pos // COLS - ROW_OFFSET, pos % COLS


def rc_to_pos(row, col):
    """Convert game grid (row, col) to deck key position."""
    return (row + ROW_OFFSET) * COLS + col


def neighbors(r, c):
    """Return list of valid (row, col) neighbors."""
    return [
        (r + dr, c + dc)
        for dr in (-1, 0, 1)
        for dc in (-1, 0, 1)
        if (dr, dc) != (0, 0) and 0 <= r + dr < ROWS and 0 <= c + dc < COLS
    ]


# -- voice pack (Starcraft Battlecruiser) ----------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "sc_battlecruiser/sounds/BattlecruiserOperational.mp3",
        "sc_battlecruiser/sounds/AllCrewsReporting.mp3",
        "sc_battlecruiser/sounds/GoodDayCommander.mp3",
    ],
    "gameover": [
        "sc_battlecruiser/sounds/IdentifyYourself.mp3",
        "sc_battlecruiser/sounds/ShieldsUp.mp3",
        "sc_battlecruiser/sounds/WayBehindSchedule.mp3",
    ],
    "win": [
        "sc_battlecruiser/sounds/Engage.mp3",
        "sc_battlecruiser/sounds/MakeItHappen.mp3",
        "sc_battlecruiser/sounds/SetACourse.mp3",
        "sc_battlecruiser/sounds/HailingFrequenciesOpen.mp3",
    ],
}

_last_voice_time: float = 0
VOICE_COOLDOWN = 4.0


def play_voice(event: str):
    """Play a random voice line -- with cooldown to avoid spam."""
    global _last_voice_time
    now = time.monotonic()
    if now - _last_voice_time < VOICE_COOLDOWN:
        return
    paths = VOICES.get(event, [])
    if not paths:
        return
    random.shuffle(paths)
    for rel in paths:
        full = os.path.join(PEON_DIR, rel)
        if os.path.exists(full):
            _last_voice_time = now
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
            tail = max(0.0, 1.0 - (i / n) * 0.6)
            samples.append(val * env * tail)
    return samples


def _noise(dur: float, vol: float = 0.5) -> list[float]:
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        env = max(0, 1.0 - (i / n) * 6)
        samples.append(random.uniform(-vol, vol) * env)
    return samples


def _merge(*lists: list[float]) -> list[float]:
    length = max(len(a) for a in lists)
    result = []
    for i in range(length):
        s = sum(a[i] if i < len(a) else 0 for a in lists)
        result.append(max(-0.95, min(0.95, s)))
    return result


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
    _sfx_dir = tempfile.mkdtemp(prefix="mines-sfx-")
    v = SFX_VOLUME

    # REVEAL -- soft click (short high blip)
    s = _square(880, 0.025, v * 0.3, 0.25)
    _write_wav(os.path.join(_sfx_dir, "reveal.wav"), s)
    _sfx_cache["reveal"] = os.path.join(_sfx_dir, "reveal.wav")

    # EMPTY_FLOOD -- cascading sweep (rising pitch sequence)
    s = []
    for i, f in enumerate([440, 523, 587, 659, 698, 784]):
        s += _triangle(f, 0.03, v * (0.2 + i * 0.04))
    _write_wav(os.path.join(_sfx_dir, "empty_flood.wav"), s)
    _sfx_cache["empty_flood"] = os.path.join(_sfx_dir, "empty_flood.wav")

    # MINE -- explosion (noise burst + low rumble)
    boom = _noise(0.15, v * 0.8)
    rumble = _square(60, 0.3, v * 0.5, 0.5)
    crack = _square(200, 0.05, v * 0.6, 0.3)
    s = _merge(crack + [0] * (len(boom) - len(crack)), boom) + rumble
    _write_wav(os.path.join(_sfx_dir, "mine.wav"), s)
    _sfx_cache["mine"] = os.path.join(_sfx_dir, "mine.wav")

    # WIN -- victory fanfare (C5 -> E5 -> G5 -> C6, triumphant)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) +
         _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "win.wav"), s)
    _sfx_cache["win"] = os.path.join(_sfx_dir, "win.wav")

    # START -- click (quick power-up)
    s = (_triangle(330, 0.04, v * 0.4) +
         _triangle(440, 0.04, v * 0.45) +
         _triangle(523, 0.06, v * 0.5))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")


def play_sfx(name: str):
    """Play sound non-blocking via afplay."""
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# -- number colors (1-8) --------------------------------------------------
NUMBER_COLORS = {
    1: "#3b82f6",   # blue
    2: "#22c55e",   # green
    3: "#ef4444",   # red
    4: "#a855f7",   # purple
    5: "#7f1d1d",   # maroon
    6: "#06b6d4",   # cyan
    7: "#1e293b",   # black (dark)
    8: "#9ca3af",   # grey
}

# -- renderers -------------------------------------------------------------

def render_unrevealed(size=SIZE) -> Image.Image:
    """Dark grey raised tile with subtle 3D border effect."""
    img = Image.new("RGB", size, "#374151")
    d = ImageDraw.Draw(img)
    # Top-left highlight (lighter)
    d.line([(4, 4), (91, 4)], fill="#6b7280", width=3)
    d.line([(4, 4), (4, 91)], fill="#6b7280", width=3)
    # Bottom-right shadow (darker)
    d.line([(4, 91), (91, 91)], fill="#1f2937", width=3)
    d.line([(91, 4), (91, 91)], fill="#1f2937", width=3)
    return img


def render_revealed_empty(size=SIZE) -> Image.Image:
    """Flat dark tile for 0 adjacent mines."""
    img = Image.new("RGB", size, "#1e293b")
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 93, 93], outline="#334155", width=1)
    return img


def render_revealed_number(number: int, size=SIZE) -> Image.Image:
    """Colored number on flat tile."""
    img = Image.new("RGB", size, "#1e293b")
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, 93, 93], outline="#334155", width=1)
    color = NUMBER_COLORS.get(number, "#e5e7eb")
    d.text((48, 48), str(number), font=_font(42), fill=color, anchor="mm")
    return img


def render_mine(size=SIZE) -> Image.Image:
    """Red tile with black circle bomb."""
    img = Image.new("RGB", size, "#dc2626")
    d = ImageDraw.Draw(img)
    cx, cy = 48, 48
    # Bomb body
    d.ellipse([cx - 18, cy - 18, cx + 18, cy + 18], fill="#111827")
    # Spikes
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x1 = cx + int(16 * math.cos(rad))
        y1 = cy + int(16 * math.sin(rad))
        x2 = cx + int(24 * math.cos(rad))
        y2 = cy + int(24 * math.sin(rad))
        d.line([(x1, y1), (x2, y2)], fill="#111827", width=3)
    # Highlight
    d.ellipse([cx - 8, cy - 12, cx - 2, cy - 6], fill="#6b7280")
    return img


def render_mine_all(size=SIZE) -> Image.Image:
    """Smaller mine indicator for revealing all mines on game over."""
    img = Image.new("RGB", size, "#991b1b")
    d = ImageDraw.Draw(img)
    cx, cy = 48, 48
    d.ellipse([cx - 14, cy - 14, cx + 14, cy + 14], fill="#111827")
    for angle in range(0, 360, 45):
        rad = math.radians(angle)
        x1 = cx + int(12 * math.cos(rad))
        y1 = cy + int(12 * math.sin(rad))
        x2 = cx + int(19 * math.cos(rad))
        y2 = cy + int(19 * math.sin(rad))
        d.line([(x1, y1), (x2, y2)], fill="#111827", width=2)
    d.ellipse([cx - 6, cy - 9, cx - 1, cy - 4], fill="#6b7280")
    return img


def render_won_tile(size=SIZE) -> Image.Image:
    """Gold/green celebration tile."""
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    # Gold star
    cx, cy = 48, 42
    d.text((cx, cy), "\u2605", font=_font(36), fill="#fbbf24", anchor="mm")
    return img


def render_hud_title(text: str, size=SIZE) -> Image.Image:
    """Generic HUD title tile."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 48), text, font=_font(18), fill="#fbbf24", anchor="mm")
    return img


def render_hud_best(best_time, size=SIZE) -> Image.Image:
    """Best time display."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    label = f"{best_time}s" if best_time < 999 else "--"
    d.text((48, 52), label, font=_font(24), fill="#34d399", anchor="mt")
    return img


def render_hud_timer(elapsed: int, size=SIZE) -> Image.Image:
    """Elapsed time display."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "TIME", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), f"{elapsed}s", font=_font(26), fill="#60a5fa", anchor="mt")
    return img


def render_hud_mines(remaining: int, size=SIZE) -> Image.Image:
    """Mines remaining display."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "MINES", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(remaining), font=_font(28), fill="#ef4444", anchor="mt")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
    return img


def render_game_over_text(size=SIZE) -> Image.Image:
    """Game over overlay tile."""
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "GAME", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "OVER", font=_font(16), fill="#fca5a5", anchor="mm")
    return img


def render_win_text(size=SIZE) -> Image.Image:
    """Win overlay tile."""
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "YOU", font=_font(18), fill="white", anchor="mm")
    d.text((48, 58), "WIN!", font=_font(18), fill="#34d399", anchor="mm")
    return img


def render_new_best(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
    return img


# -- game logic ------------------------------------------------------------

class MinesGame:
    def __init__(self, deck):
        self.deck = deck
        self.best = scores.load_best("mines", default=999)
        self.running = False
        self.game_over = False
        self.lock = threading.Lock()

        # Board state
        self.mines: set = set()           # set of (row, col)
        self.revealed: set = set()        # set of (row, col)
        self.numbers: dict = {}           # (row, col) -> int
        self.first_click = True
        self.start_time: float = 0
        self.elapsed: int = 0

        # Timers
        self.clock_timer: threading.Timer | None = None
        self.timers: list[threading.Timer] = []

        # Pre-render reusable images
        self.img_unrevealed = render_unrevealed()
        self.img_start = render_start()
        self.img_hud_empty = render_hud_empty()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _cancel_all_timers(self):
        """Cancel all running timers for cleanup."""
        if self.clock_timer:
            self.clock_timer.cancel()
            self.clock_timer = None
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    # -- idle screen -------------------------------------------------------

    def show_idle(self):
        """Show title screen: HUD on row 0, game preview on rows 1-3."""
        self.running = False
        self.game_over = False
        self._cancel_all_timers()

        # Row 0 (keys 0-7): HUD
        self.set_key(0, render_hud_title("MINES"))
        self.set_key(1, render_hud_title("WEEP"))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_mines(NUM_MINES))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Rows 1-3 (keys 8-31): game area preview + start button
        for k in range(8, 32):
            if k == 20:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, self.img_unrevealed)

    # -- mine generation ---------------------------------------------------

    def _generate_board(self, safe_r: int, safe_c: int):
        """Place mines randomly, ensuring safe_r/safe_c and its neighbors
        are mine-free. Then calculate number counts."""
        # Collect safe zone (first click + neighbors)
        safe_zone = set()
        safe_zone.add((safe_r, safe_c))
        for nr, nc in neighbors(safe_r, safe_c):
            safe_zone.add((nr, nc))

        # All possible positions minus safe zone
        all_positions = [
            (r, c) for r in range(ROWS) for c in range(COLS)
            if (r, c) not in safe_zone
        ]
        random.shuffle(all_positions)
        self.mines = set(all_positions[:NUM_MINES])

        # Calculate numbers for every cell
        self.numbers = {}
        for r in range(ROWS):
            for c in range(COLS):
                if (r, c) in self.mines:
                    self.numbers[(r, c)] = -1  # mine marker
                else:
                    count = sum(1 for nr, nc in neighbors(r, c) if (nr, nc) in self.mines)
                    self.numbers[(r, c)] = count

    # -- flood fill --------------------------------------------------------

    def _flood_reveal(self, r: int, c: int):
        """Reveal tile at (r,c). If it has 0 adjacent mines, recursively
        reveal all connected empty tiles and their numbered borders."""
        stack = [(r, c)]
        while stack:
            cr, cc = stack.pop()
            if (cr, cc) in self.revealed:
                continue
            if (cr, cc) in self.mines:
                continue

            self.revealed.add((cr, cc))
            count = self.numbers.get((cr, cc), 0)
            pos = rc_to_pos(cr, cc)

            if count == 0:
                # Empty tile -- render and expand to neighbors
                self.set_key(pos, render_revealed_empty())
                for nr, nc in neighbors(cr, cc):
                    if (nr, nc) not in self.revealed:
                        stack.append((nr, nc))
            else:
                # Numbered tile -- render number, do not expand
                self.set_key(pos, render_revealed_number(count))

    # -- game clock --------------------------------------------------------

    def _tick_clock(self):
        """Update the elapsed time display every second."""
        if not self.running:
            return
        self.elapsed = int(time.monotonic() - self.start_time)
        self.set_key(2, render_hud_timer(self.elapsed))

        # Schedule next tick
        self.clock_timer = threading.Timer(1.0, self._tick_clock)
        self.clock_timer.daemon = True
        self.clock_timer.start()

    # -- win / lose --------------------------------------------------------

    def _check_win(self):
        """Check if all non-mine tiles are revealed."""
        total_safe = ROWS * COLS - NUM_MINES
        if len(self.revealed) >= total_safe:
            return True
        return False

    def _handle_win(self):
        """Player revealed all safe tiles."""
        self.running = False
        self._cancel_all_timers()
        final_time = int(time.monotonic() - self.start_time)

        is_new_best = final_time < self.best
        if is_new_best:
            self.best = final_time
            scores.save_best("mines", self.best)

        play_sfx("win")
        play_voice("win")

        # Show celebration: all tiles become won tiles
        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                if (r, c) in self.mines:
                    # Show mines as defused (green)
                    self.set_key(pos, render_won_tile())
                elif (r, c) in self.revealed:
                    # Already revealed -- leave as is or overlay star
                    pass

        # Win text on center tiles
        self.set_key(19, render_win_text())
        self.set_key(20, self.img_start)

        # HUD updates
        self.set_key(0, render_hud_title("MINES"))
        self.set_key(1, render_hud_title("WEEP"))
        self.set_key(2, render_hud_timer(final_time))
        self.set_key(3, render_hud_best(self.best))
        if is_new_best:
            self.set_key(4, render_new_best())
        for k in range(5 if is_new_best else 4, 8):
            self.set_key(k, self.img_hud_empty)

    def _handle_mine_hit(self, hit_r: int, hit_c: int):
        """Player hit a mine -- game over."""
        self.running = False
        self.game_over = True
        self._cancel_all_timers()

        play_sfx("mine")
        play_voice("gameover")

        # Show the hit mine
        hit_pos = rc_to_pos(hit_r, hit_c)
        self.set_key(hit_pos, render_mine())

        # Reveal all other mines after brief delay
        def _show_all_mines():
            time.sleep(0.4)
            for mr, mc in self.mines:
                if (mr, mc) != (hit_r, hit_c):
                    pos = rc_to_pos(mr, mc)
                    self.set_key(pos, render_mine_all())
                    time.sleep(0.08)  # cascade effect

            # Show game over text and restart
            time.sleep(0.3)
            self.set_key(19, render_game_over_text())
            self.set_key(20, self.img_start)

            # Update HUD
            final_time = int(time.monotonic() - self.start_time)
            self.set_key(0, render_hud_title("MINES"))
            self.set_key(1, render_hud_title("WEEP"))
            self.set_key(2, render_hud_timer(final_time))
            self.set_key(3, render_hud_best(self.best))
            for k in range(4, 8):
                self.set_key(k, self.img_hud_empty)

        t = threading.Thread(target=_show_all_mines, daemon=True)
        t.start()

    # -- start game --------------------------------------------------------

    def start_game(self):
        """Start a new round."""
        with self.lock:
            self._cancel_all_timers()
            self.mines = set()
            self.revealed = set()
            self.numbers = {}
            self.first_click = True
            self.running = True
            self.game_over = False
            self.elapsed = 0
            self.start_time = 0

        play_sfx("start")
        play_voice("start")

        # Render all tiles as unrevealed
        for r in range(ROWS):
            for c in range(COLS):
                pos = rc_to_pos(r, c)
                self.set_key(pos, self.img_unrevealed)

        # HUD
        self.set_key(0, render_hud_title("MINES"))
        self.set_key(1, render_hud_title("WEEP"))
        self.set_key(2, render_hud_timer(0))
        self.set_key(3, render_hud_best(self.best))
        self.set_key(4, render_hud_mines(NUM_MINES))
        for k in range(5, 8):
            self.set_key(k, self.img_hud_empty)

    # -- key handler -------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart from idle or game-over
        if key == 20 and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        # Only game area keys (rows 1-3, keys 8-31)
        if key < ROW_OFFSET * COLS or key >= (ROW_OFFSET + ROWS) * COLS:
            return

        r, c = pos_to_rc(key)

        with self.lock:
            # Ignore already-revealed tiles
            if (r, c) in self.revealed:
                return

            # First click -- generate board (guaranteed safe)
            if self.first_click:
                self.first_click = False
                self._generate_board(r, c)
                self.start_time = time.monotonic()
                # Start clock
                self.clock_timer = threading.Timer(1.0, self._tick_clock)
                self.clock_timer.daemon = True
                self.clock_timer.start()

            # Check if mine
            if (r, c) in self.mines:
                self._handle_mine_hit(r, c)
                return

            # Reveal tile
            count = self.numbers.get((r, c), 0)
            if count == 0:
                # Flood fill for empty tiles
                self._flood_reveal(r, c)
                play_sfx("empty_flood")
            else:
                # Single numbered tile
                self.revealed.add((r, c))
                pos = rc_to_pos(r, c)
                self.set_key(pos, render_revealed_number(count))
                play_sfx("reveal")

            # Check win condition
            if self._check_win():
                self._handle_win()


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
    print("MINESWEEPER! Press the center button to start.")

    game = MinesGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(f"\nBye! Best time: {game.best}s")
    finally:
        game._cancel_all_timers()
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
