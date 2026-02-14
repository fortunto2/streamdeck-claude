"""Simon Says — Stream Deck memory game.

Watch the sequence, repeat it! Each round adds one more step.
4 colored zones on the game area, sequence gets longer and faster.

Usage:
    uv run python scripts/simon_game.py
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

# ── config ───────────────────────────────────────────────────────────
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

# 4 color zones — each zone is a group of buttons
# Row 2 (8-15): left 4 = RED, right 4 = BLUE
# Row 3 (16-23): left 4 = GREEN, right 4 = YELLOW
ZONES = {
    "red":    {"keys": [8, 9, 16, 17],   "color": "#ef4444", "dim": "#7f1d1d", "note": 264},
    "blue":   {"keys": [10, 11, 18, 19], "color": "#3b82f6", "dim": "#1e3a5f", "note": 330},
    "green":  {"keys": [12, 13, 20, 21], "color": "#22c55e", "dim": "#14532d", "note": 392},
    "yellow": {"keys": [14, 15, 22, 23], "color": "#eab308", "dim": "#713f12", "note": 523},
}
ZONE_NAMES = list(ZONES.keys())
# Map button -> zone name
KEY_TO_ZONE: dict[int, str] = {}
for zname, zdata in ZONES.items():
    for k in zdata["keys"]:
        KEY_TO_ZONE[k] = zname

GAME_KEYS = list(range(8, 24))  # rows 2-3
HUD_KEYS = list(range(0, 8))
BOTTOM_KEYS = list(range(24, 32))  # row 4 — decorative

# Orc voices
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    # Simon Says = GLaDOS (Portal — she literally tells you what to do)
    "start": [
        "glados/sounds/Hello.mp3",
        "glados/sounds/CanYouHearMe.mp3",
        "glados/sounds/GoodNews.mp3",
        "glados/sounds/IKnowYoureThere.mp3",
    ],
    "fail": [
        "glados/sounds/WompWomp.mp3",
        "glados/sounds/Unbelievable.mp3",
        "glados/sounds/WhereDidYourLifeGoWrong.mp3",
        "glados/sounds/ItAintWorkin.mp3",
    ],
    "milestone": [
        "glados/sounds/Excellent.mp3",
        "glados/sounds/Fantastic.mp3",
        "glados/sounds/KeepDoing.mp3",
        "glados/sounds/Yes.mp3",
    ],
    "newbest": [
        "glados/sounds/Congratulations.mp3",
        "glados/sounds/Excellent.mp3",
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
            try:
                subprocess.Popen(["afplay", full], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            return


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
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="simon-sfx-")
    v = SFX_VOLUME

    # One tone per zone color
    for zname, zdata in ZONES.items():
        freq = zdata["note"]
        s = _triangle(freq, 0.25, v * 0.6)
        path = os.path.join(_sfx_dir, f"{zname}.wav")
        _write_wav(path, s)
        _sfx_cache[zname] = path

    # Error buzz
    s = _square(150, 0.4, v * 0.5, 0.5)
    _write_wav(os.path.join(_sfx_dir, "error.wav"), s)
    _sfx_cache["error"] = os.path.join(_sfx_dir, "error.wav")

    # Success ding
    s = _triangle(523, 0.06, v * 0.5) + _triangle(659, 0.06, v * 0.55) + _triangle(784, 0.1, v * 0.6)
    _write_wav(os.path.join(_sfx_dir, "success.wav"), s)
    _sfx_cache["success"] = os.path.join(_sfx_dir, "success.wav")


def play_sfx(name: str):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        try:
            subprocess.Popen(["afplay", wav], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# ── font ─────────────────────────────────────────────────────────────
def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


# ── renderers ────────────────────────────────────────────────────────

def render_zone_tile(color: str, size=SIZE) -> Image.Image:
    """Solid color zone tile."""
    return Image.new("RGB", size, color)


def render_hud_text(line1: str, line2: str, bg: str = "#111827",
                    c1: str = "#9ca3af", c2: str = "#ffffff", s2: int = 32, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 20), line1, font=_font(14), fill=c1, anchor="mt")
    d.text((48, 52), line2, font=_font(s2), fill=c2, anchor="mt")
    return img


def render_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "SIMON", font=_font(16), fill="#a78bfa", anchor="mt")
    d.text((48, 52), "SAYS", font=_font(16), fill="#a78bfa", anchor="mt")
    return img


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
    return img


def render_watch(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#4c1d95")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "WATCH", font=_font(18), fill="#c4b5fd", anchor="mm")
    d.text((48, 60), "...", font=_font(18), fill="#a78bfa", anchor="mm")
    return img


def render_your_turn(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "YOUR", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "TURN!", font=_font(16), fill="#34d399", anchor="mm")
    return img


def render_game_over(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white", anchor="mm", align="center")
    return img


def render_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


# ── game logic ───────────────────────────────────────────────────────

class SimonGame:
    def __init__(self, deck):
        self.deck = deck
        self.sequence: list[str] = []
        self.player_pos = 0
        self.round = 0
        self.best = 0
        self.state = "idle"  # idle | showing | playing | gameover
        self.lock = threading.Lock()
        self.accepting_input = False
        # Speed: starts slow, gets faster
        self.base_delay = 0.6  # seconds per flash
        self.min_delay = 0.2

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _delay(self) -> float:
        """Current flash delay — faster at higher rounds."""
        return max(self.min_delay, self.base_delay - self.round * 0.03)

    def _draw_zones_dim(self):
        """Draw all zones in dim state."""
        for zname, zdata in ZONES.items():
            for k in zdata["keys"]:
                self.set_key(k, render_zone_tile(zdata["dim"]))

    def _flash_zone(self, zname: str, duration: float = 0.3):
        """Light up a zone bright, play its tone, then dim."""
        zdata = ZONES[zname]
        for k in zdata["keys"]:
            self.set_key(k, render_zone_tile(zdata["color"]))
        play_sfx(zname)
        time.sleep(duration)
        for k in zdata["keys"]:
            self.set_key(k, render_zone_tile(zdata["dim"]))

    def _update_hud(self):
        self.set_key(0, render_title())
        self.set_key(1, render_hud_text("ROUND", str(self.round)))
        self.set_key(2, render_hud_text("BEST", str(self.best), c2="#34d399"))
        self.set_key(3, render_hud_text("SEQ", str(len(self.sequence)), c2="#a78bfa", s2=28))
        for k in range(4, 8):
            self.set_key(k, render_empty())

    def show_idle(self):
        self.state = "idle"
        self.set_key(0, render_title())
        self.set_key(1, render_hud_text("ROUND", "0"))
        self.set_key(2, render_hud_text("BEST", str(self.best), c2="#34d399"))
        for k in range(3, 8):
            self.set_key(k, render_empty())
        self._draw_zones_dim()
        # Start button in bottom row center
        for k in BOTTOM_KEYS:
            if k == 28:
                self.set_key(k, render_start())
            else:
                self.set_key(k, render_empty())

    def start_game(self):
        self.sequence = []
        self.round = 0
        self.state = "showing"
        self.accepting_input = False
        play_orc("start")
        self._draw_zones_dim()
        for k in BOTTOM_KEYS:
            self.set_key(k, render_empty())
        self._update_hud()
        # Start first round in background
        threading.Thread(target=self._next_round, daemon=True).start()

    def _next_round(self):
        """Add one to sequence and show it."""
        self.round += 1
        self.sequence.append(random.choice(ZONE_NAMES))
        self.state = "showing"
        self._update_hud()

        # Show "WATCH" indicator
        self.set_key(7, render_watch())
        time.sleep(0.5)

        # Play the full sequence
        delay = self._delay()
        for zname in self.sequence:
            if self.state != "showing":
                return
            self._flash_zone(zname, delay)
            time.sleep(0.15)  # gap between flashes

        # Now it's player's turn
        self.set_key(7, render_your_turn())
        self.player_pos = 0
        self.state = "playing"
        self.accepting_input = True

        # Orc milestone every 5 rounds
        if self.round % 5 == 0 and self.round > 0:
            play_orc("milestone")

    def _player_hit(self, zname: str):
        """Player pressed a zone."""
        expected = self.sequence[self.player_pos]
        if zname == expected:
            # Correct!
            self._flash_zone(zname, 0.15)
            self.player_pos += 1
            if self.player_pos >= len(self.sequence):
                # Round complete!
                self.accepting_input = False
                play_sfx("success")
                self._update_hud()
                time.sleep(0.5)
                threading.Thread(target=self._next_round, daemon=True).start()
        else:
            # Wrong!
            self.accepting_input = False
            self.state = "gameover"
            play_sfx("error")

            # Flash the correct zone to show what it was
            zdata = ZONES[expected]
            for k in zdata["keys"]:
                self.set_key(k, render_zone_tile(zdata["color"]))

            new_best = self.round - 1 > self.best
            if self.round - 1 > self.best:
                self.best = self.round - 1

            if new_best and self.best > 0:
                play_orc("newbest")
            else:
                play_orc("fail")

            self._update_hud()
            self.set_key(7, render_game_over())

            time.sleep(1.5)
            self._draw_zones_dim()
            # Show restart
            self.set_key(28, render_start())

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == 28 and self.state in ("idle", "gameover"):
            self.start_game()
            return

        if not self.accepting_input or self.state != "playing":
            return

        zname = KEY_TO_ZONE.get(key)
        if zname:
            self._player_hit(zname)


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

    try:
        _generate_sfx()
        print("Sound effects: ON")
    except Exception:
        print("Sound effects: OFF")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("SIMON SAYS! Press START to begin.")

    game = SimonGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(f"\nBye! Best: {game.best} rounds")
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
