"""Memory Match — Stream Deck mini-game.

Find all 12 matching color pairs! Flip two cards at a time.
Top row shows HUD, game area: buttons 8-31 = 24 tiles = 12 pairs.

Usage:
    uv run python scripts/memory_game.py
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

# ── config ───────────────────────────────────────────────────────────
GAME_KEYS = list(range(8, 32))  # rows 2-4 = game area (24 buttons)
HUD_KEYS = list(range(0, 8))    # row 1 = HUD
TOTAL_PAIRS = 12
SIZE = (96, 96)

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
MAX_MOVES = 25

# 12 distinct bright colors for pairs
PAIR_COLORS = [
    ("#ef4444", "red"),       # red
    ("#3b82f6", "blue"),      # blue
    ("#22c55e", "green"),     # green
    ("#eab308", "yellow"),    # yellow
    ("#f97316", "orange"),    # orange
    ("#a855f7", "purple"),    # purple
    ("#ec4899", "pink"),      # pink
    ("#06b6d4", "cyan"),      # cyan
    ("#84cc16", "lime"),      # lime
    ("#e879f9", "magenta"),   # magenta
    ("#14b8a6", "teal"),      # teal
    ("#fb923c", "coral"),     # coral
]

# ── orc voice lines (peon-ping packs) ───────────────────────────────
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    # Memory = Battlecruiser (StarCraft — calm, methodical, scanning)
    "start": [
        "sc_battlecruiser/sounds/BattlecruiserOperational.mp3",
        "sc_battlecruiser/sounds/AllCrewsReporting.mp3",
        "sc_battlecruiser/sounds/GoodDayCommander.mp3",
    ],
    "win": [
        "sc_battlecruiser/sounds/Engage.mp3",
        "sc_battlecruiser/sounds/MakeItHappen.mp3",
        "sc_battlecruiser/sounds/SetACourse.mp3",
    ],
    "newbest": [
        "sc_battlecruiser/sounds/HailingFrequenciesOpen.mp3",
        "sc_battlecruiser/sounds/ReceivingTransmission.mp3",
    ],
    "lose": [
        "sc_battlecruiser/sounds/AllCrewsReporting.mp3",
        "sc_battlecruiser/sounds/GoodDayCommander.mp3",
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
    _sfx_dir = tempfile.mkdtemp(prefix="memory-sfx-")
    v = SFX_VOLUME

    # FLIP — short blip when card revealed (C5 quick)
    s = _square(523, 0.04, v * 0.4, 0.25)
    _write_wav(os.path.join(_sfx_dir, "flip.wav"), s)
    _sfx_cache["flip"] = os.path.join(_sfx_dir, "flip.wav")

    # MATCH — happy rising two-note (E5 -> G5)
    s = _triangle(659, 0.08, v * 0.5) + _triangle(784, 0.12, v * 0.6)
    _write_wav(os.path.join(_sfx_dir, "match.wav"), s)
    _sfx_cache["match"] = os.path.join(_sfx_dir, "match.wav")

    # NOMATCH — sad descending note (A4 -> E4)
    s = _square(440, 0.1, v * 0.35, 0.5) + _square(330, 0.15, v * 0.3, 0.5)
    _write_wav(os.path.join(_sfx_dir, "nomatch.wav"), s)
    _sfx_cache["nomatch"] = os.path.join(_sfx_dir, "nomatch.wav")

    # WIN — victory jingle (C5 -> E5 -> G5 -> C6)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) +
         _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "win.wav"), s)
    _sfx_cache["win"] = os.path.join(_sfx_dir, "win.wav")

    # LOSE — sad descending (A4->F4->D4->A3)
    s = (_square(440, 0.1, v * 0.5, 0.5) +
         _square(349, 0.1, v * 0.45, 0.5) +
         _square(294, 0.12, v * 0.4, 0.5) +
         _square(220, 0.25, v * 0.35, 0.5))
    _write_wav(os.path.join(_sfx_dir, "lose.wav"), s)
    _sfx_cache["lose"] = os.path.join(_sfx_dir, "lose.wav")


def play_sfx(name: str):
    """Play sound non-blocking via afplay."""
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# ── renderers ────────────────────────────────────────────────────────

def render_face_down(size=SIZE) -> Image.Image:
    """Dark gray card face-down with '?' text."""
    img = Image.new("RGB", size, "#374151")
    d = ImageDraw.Draw(img)
    # Subtle border
    d.rectangle([4, 4, 91, 91], outline="#4b5563", width=2)
    d.text((48, 48), "?", font=_font(40), fill="#9ca3af", anchor="mm")
    return img


def render_face_up(color: str, size=SIZE) -> Image.Image:
    """Bright color card face-up."""
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    # White border to make it pop
    d.rectangle([4, 4, 91, 91], outline="white", width=3)
    return img


def render_matched(color: str, size=SIZE) -> Image.Image:
    """Matched card — bright color with checkmark."""
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([4, 4, 91, 91], outline="white", width=2)
    # Checkmark
    d.text((48, 48), "\u2713", font=_font(36), fill="white", anchor="mm")
    return img


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "MEMORY", font=_font(15), fill="#f59e0b", anchor="mm")
    d.text((48, 60), "MATCH", font=_font(12), fill="#fbbf24", anchor="mm")
    return img


def render_hud_moves(moves: int, max_moves: int = MAX_MOVES, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "MOVES", font=_font(14), fill="#9ca3af", anchor="mt")
    remaining = max_moves - moves
    clr = "#ef4444" if remaining <= 5 else "#fbbf24" if remaining <= 10 else "#60a5fa"
    d.text((48, 48), f"{moves}/{max_moves}", font=_font(22), fill=clr, anchor="mt")
    return img


def render_hud_pairs(found: int, total: int = TOTAL_PAIRS, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "PAIRS", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), f"{found}/{total}", font=_font(26), fill="#34d399", anchor="mt")
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


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
    return img


def render_win(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "YOU", font=_font(18), fill="white", anchor="mm")
    d.text((48, 60), "WIN!", font=_font(18), fill="#34d399", anchor="mm")
    return img


def render_game_over(size=SIZE) -> Image.Image:
    """Win screen tile for non-special positions."""
    img = Image.new("RGB", size, "#fbbf24")
    d = ImageDraw.Draw(img)
    d.text((48, 42), "\u2605", font=_font(40), fill="white", anchor="mm")
    return img


def render_lose(size=SIZE) -> Image.Image:
    """Lose screen — out of moves."""
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "OUT OF", font=_font(14), fill="white", anchor="mm")
    d.text((48, 58), "MOVES!", font=_font(16), fill="#fca5a5", anchor="mm")
    return img


def render_new_best(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
    return img


# ── game logic ───────────────────────────────────────────────────────

class MemoryGame:
    def __init__(self, deck):
        self.deck = deck
        self.moves = 0
        self.pairs_found = 0
        self.best = scores.load_best("memory")
        self.running = False
        self.lock = threading.Lock()
        self.accepting_input = True

        # Board state: index 0-23 maps to GAME_KEYS[0-23]
        # Each slot holds a color index (0-11), two slots share the same index
        self.board: list[int] = []
        # Track which cards are revealed (matched)
        self.matched: list[bool] = [False] * 24
        # Currently flipped cards (up to 2) — stores board indices (0-23)
        self.flipped: list[int] = []

        # Pre-render reusable images
        self.img_face_down = render_face_down()
        self.img_hud_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_start = render_start()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _board_index(self, key: int) -> int:
        """Convert a key (8-31) to board index (0-23)."""
        return key - 8

    def _key_from_index(self, idx: int) -> int:
        """Convert board index (0-23) to key (8-31)."""
        return idx + 8

    def _color_hex(self, board_idx: int) -> str:
        """Get the hex color for a board position."""
        return PAIR_COLORS[self.board[board_idx]][0]

    def _update_hud(self):
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_moves(self.moves))
        self.set_key(2, render_hud_pairs(self.pairs_found))
        self.set_key(3, render_hud_best(self.best))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

    def show_idle(self):
        """Show start screen."""
        self.running = False
        # HUD row
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_moves(0))
        self.set_key(2, render_hud_pairs(0))
        self.set_key(3, render_hud_best(self.best))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Game area — all face-down, start button at key 20
        for k in GAME_KEYS:
            if k == 20:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, self.img_face_down)

    def start_game(self):
        """Start a new round — shuffle and deal cards."""
        with self.lock:
            self.moves = 0
            self.pairs_found = 0
            self.running = True
            self.accepting_input = True
            self.flipped = []
            self.matched = [False] * 24

            # Create pairs: 12 colors x 2 = 24 cards
            self.board = list(range(TOTAL_PAIRS)) * 2
            random.shuffle(self.board)

        play_sfx("flip")
        play_orc("start")

        # Show all cards face-down
        for k in GAME_KEYS:
            self.set_key(k, self.img_face_down)

        self._update_hud()

    def _reveal_card(self, board_idx: int):
        """Show a card face-up on the deck."""
        key = self._key_from_index(board_idx)
        color = self._color_hex(board_idx)
        self.set_key(key, render_face_up(color))

    def _hide_card(self, board_idx: int):
        """Flip a card back to face-down."""
        key = self._key_from_index(board_idx)
        self.set_key(key, self.img_face_down)

    def _mark_matched(self, board_idx: int):
        """Show a card as permanently matched."""
        key = self._key_from_index(board_idx)
        color = self._color_hex(board_idx)
        self.set_key(key, render_matched(color))

    def _check_win(self):
        """Check if all pairs are found."""
        if self.pairs_found >= TOTAL_PAIRS:
            self.running = False
            is_new_best = self.best == 0 or self.moves < self.best

            if is_new_best:
                self.best = self.moves
                scores.save_best("memory", self.best)
                play_sfx("win")
                play_orc("newbest")
            else:
                play_sfx("win")
                play_orc("win")

            self._show_win(is_new_best)

    def _lose(self):
        """Handle loss — out of moves."""
        self.running = False
        play_sfx("lose")
        play_orc("lose")

        # Show all hidden cards briefly, then lose screen
        for i in range(24):
            if not self.matched[i]:
                key = self._key_from_index(i)
                color = self._color_hex(i)
                self.set_key(key, render_face_up(color))

        self._update_hud()

        def _show_lose_screen():
            time.sleep(1.2)
            for k in GAME_KEYS:
                if k == 20:
                    self.set_key(k, self.img_start)
                elif k in (18, 19, 21):
                    self.set_key(k, render_lose())
                else:
                    self.set_key(k, render_hud_empty())

        threading.Thread(target=_show_lose_screen, daemon=True).start()

    def _show_win(self, is_new_best: bool):
        """Show win screen."""
        # Update HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_moves(self.moves))
        self.set_key(2, render_hud_pairs(self.pairs_found))
        self.set_key(3, render_hud_best(self.best))
        if is_new_best:
            self.set_key(4, render_new_best())
        for k in range(5 if is_new_best else 4, 8):
            self.set_key(k, self.img_hud_empty)

        # Game area — victory display
        for k in GAME_KEYS:
            if k == 20:
                self.set_key(k, self.img_start)  # restart button
            else:
                # Show matched colors with stars on some
                bidx = self._board_index(k)
                color = self._color_hex(bidx)
                self.set_key(k, render_matched(color))

        # Flash win tile at center positions
        win_keys = [18, 19, 20, 21]
        for k in win_keys:
            if k == 20:
                continue
            self.set_key(k, render_win())

    def _handle_no_match(self, idx_a: int, idx_b: int):
        """Flip two non-matching cards back after a delay."""
        time.sleep(0.8)
        with self.lock:
            # Only hide if they haven't been matched in the meantime
            if not self.matched[idx_a]:
                self._hide_card(idx_a)
            if not self.matched[idx_b]:
                self._hide_card(idx_b)
            self.accepting_input = True

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == 20 and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        if key not in GAME_KEYS:
            return

        with self.lock:
            if not self.accepting_input:
                return

            board_idx = self._board_index(key)

            # Ignore already-matched cards
            if self.matched[board_idx]:
                return

            # Ignore already-flipped card
            if board_idx in self.flipped:
                return

            # Flip the card
            self.flipped.append(board_idx)
            self._reveal_card(board_idx)
            play_sfx("flip")

            if len(self.flipped) == 2:
                idx_a, idx_b = self.flipped
                self.moves += 1
                self._update_hud()

                # Check move limit
                if self.moves >= MAX_MOVES and self.board[idx_a] != self.board[idx_b]:
                    self.flipped = []
                    self._lose()
                    return

                if self.board[idx_a] == self.board[idx_b]:
                    # Match found!
                    self.matched[idx_a] = True
                    self.matched[idx_b] = True
                    self.pairs_found += 1
                    self.flipped = []
                    self._update_hud()

                    # Show matched state
                    self._mark_matched(idx_a)
                    self._mark_matched(idx_b)
                    play_sfx("match")

                    # Check win (outside lock)
                    threading.Thread(target=self._check_win, daemon=True).start()
                else:
                    # No match — block input, flip back after delay
                    self.accepting_input = False
                    saved_a, saved_b = idx_a, idx_b
                    self.flipped = []
                    play_sfx("nomatch")
                    threading.Thread(
                        target=self._handle_no_match,
                        args=(saved_a, saved_b),
                        daemon=True,
                    ).start()


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
    print("MEMORY MATCH! Press the center button to start.")

    game = MemoryGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(f"\nBye! Best: {game.best} moves")
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
