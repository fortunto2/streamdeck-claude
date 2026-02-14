"""Beaver Hunt — Stream Deck mini-game.

Find and smash the beaver before it escapes!
Top row shows score and timer. Game area: buttons 8-31.

Usage:
    uv run python scripts/beaver_game.py
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
GAME_KEYS = list(range(8, 32))  # rows 2-4 = game area
SCORE_KEYS = list(range(0, 8))  # row 1 = HUD
BEAVER_TIMEOUT_START = 2.5  # initial seconds before beaver escapes
BEAVER_TIMEOUT_MIN = 0.45  # fastest possible (insane mode)
BEAVER_SPEEDUP = 0.15  # seconds faster per level
LEVEL_EVERY = 3  # level up every N catches
GAME_DURATION = 45  # seconds (longer to enjoy the ramp)
SIZE = (96, 96)

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

# ── orc voice lines (peon-ping packs) ───────────────────────────────
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    "start": [  # game start — ready/warcry lines
        "peon/sounds/PeonReady1.wav",
        "peon/sounds/PeonWarcry1.wav",
        "peon/sounds/PeonYesAttack1.wav",
        "peon/sounds/PeonYesAttack2.wav",
        "dota2_axe/sounds/AxeIsReady.mp3",
        "dota2_axe/sounds/GoodDayToFight.mp3",
        "dota2_axe/sounds/LetTheCarnageBegin.mp3",
        "peasant/sounds/PeasantReady1.wav",
    ],
    "levelup": [  # level up — aggressive/excited
        "peon/sounds/PeonYesAttack3.wav",
        "peon/sounds/PeonWarcry1.wav",
        "dota2_axe/sounds/ComeAndGetIt.mp3",
        "dota2_axe/sounds/Forward.mp3",
        "dota2_axe/sounds/ToBattle.mp3",
        "dota2_axe/sounds/AxeManComes.mp3",
    ],
    "gameover": [  # game over — death/angry
        "peon/sounds/PeonDeath.wav",
        "peon/sounds/PeonAngry1.wav",
        "peon/sounds/PeonAngry2.wav",
        "dota2_axe/sounds/FoughtBadly.mp3",
        "dota2_axe/sounds/YouGetNothing.mp3",
        "dota2_axe/sounds/RestForTheDead.mp3",
    ],
    "newbest": [  # new record — victory
        "dota2_axe/sounds/Culled.mp3",
        "dota2_axe/sounds/CutAbove.mp3",
        "dota2_axe/sounds/ISaidGoodDaySir.mp3",
        "peon/sounds/PeonYes1.wav",
        "peon/sounds/PeonYes2.wav",
    ],
}


_last_orc_time: float = 0
ORC_COOLDOWN = 4.0  # minimum seconds between voice lines


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
            try:
                subprocess.Popen(
                    ["afplay", full],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
            return


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


# ── 8-bit sound engine (from solo-factory) ───────────────────────────
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
    _sfx_dir = tempfile.mkdtemp(prefix="beaver-sfx-")
    v = SFX_VOLUME

    # HIT — cheerful rising blip (C5→E5→G5)
    s = _square(523, 0.04, v * 0.5, 0.25) + _square(659, 0.04, v * 0.6, 0.25) + _triangle(784, 0.06, v * 0.7)
    _write_wav(os.path.join(_sfx_dir, "hit.wav"), s)
    _sfx_cache["hit"] = os.path.join(_sfx_dir, "hit.wav")

    # MISS — sad descending (A4→E4)
    s = _square(440, 0.08, v * 0.4, 0.5) + _square(330, 0.12, v * 0.35, 0.5)
    _write_wav(os.path.join(_sfx_dir, "miss.wav"), s)
    _sfx_cache["miss"] = os.path.join(_sfx_dir, "miss.wav")

    # LEVEL UP — fanfare arpeggio (C4→E4→G4→C5, bright)
    s = (_square(262, 0.06, v * 0.4, 0.25) +
         _square(330, 0.06, v * 0.45, 0.25) +
         _square(392, 0.06, v * 0.5, 0.25) +
         _triangle(523, 0.15, v * 0.65))
    _write_wav(os.path.join(_sfx_dir, "levelup.wav"), s)
    _sfx_cache["levelup"] = os.path.join(_sfx_dir, "levelup.wav")

    # START — exciting power-up (E4→G4→B4→E5)
    s = (_triangle(330, 0.06, v * 0.4) +
         _triangle(392, 0.06, v * 0.45) +
         _triangle(494, 0.06, v * 0.5) +
         _triangle(659, 0.12, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # GAME OVER — dramatic descend (C5→G4→E4→C4, slow)
    s = (_square(523, 0.12, v * 0.5, 0.5) +
         _square(392, 0.12, v * 0.45, 0.5) +
         _square(330, 0.12, v * 0.4, 0.5) +
         _square(262, 0.25, v * 0.35, 0.5))
    _write_wav(os.path.join(_sfx_dir, "gameover.wav"), s)
    _sfx_cache["gameover"] = os.path.join(_sfx_dir, "gameover.wav")

    # SPAWN — tiny pop (noise + high blip)
    s = _merge(_noise(0.02, v * 0.2), _square(1047, 0.03, v * 0.2, 0.15))
    _write_wav(os.path.join(_sfx_dir, "spawn.wav"), s)
    _sfx_cache["spawn"] = os.path.join(_sfx_dir, "spawn.wav")

    # TICK — last 5 seconds warning beep
    s = _square(880, 0.03, v * 0.3, 0.25)
    _write_wav(os.path.join(_sfx_dir, "tick.wav"), s)
    _sfx_cache["tick"] = os.path.join(_sfx_dir, "tick.wav")

    # NEW BEST — victory jingle (C5→E5→G5→C6, triumphant)
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
        try:
            subprocess.Popen(
                ["afplay", wav],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# ── renderers ────────────────────────────────────────────────────────

def render_beaver(size=SIZE) -> Image.Image:
    """Draw a chunky pixel-art beaver face."""
    img = Image.new("RGB", size, "#2d1b0e")
    d = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2

    # Head — brown circle
    d.ellipse([14, 14, 82, 78], fill="#8B5E3C")
    # Ears
    d.ellipse([14, 10, 30, 28], fill="#6B3F1F")
    d.ellipse([66, 10, 82, 28], fill="#6B3F1F")
    # Eyes — white + black pupil
    d.ellipse([28, 30, 42, 44], fill="white")
    d.ellipse([54, 30, 68, 44], fill="white")
    d.ellipse([32, 33, 40, 41], fill="black")
    d.ellipse([58, 33, 66, 41], fill="black")
    # Nose
    d.ellipse([40, 44, 56, 54], fill="#3d2010")
    # Teeth — two big white rectangles
    d.rectangle([38, 56, 47, 70], fill="white")
    d.rectangle([49, 56, 58, 70], fill="white")
    # Mouth line
    d.line([(38, 56), (58, 56)], fill="#3d2010", width=2)

    return img


def render_empty(size=SIZE) -> Image.Image:
    """Empty grass tile."""
    img = Image.new("RGB", size, "#1a472a")
    d = ImageDraw.Draw(img)
    # Little grass tufts
    for _ in range(5):
        x = random.randint(10, 80)
        y = random.randint(50, 85)
        d.line([(x, y), (x - 3, y - 12)], fill="#2d6b3f", width=2)
        d.line([(x, y), (x + 4, y - 10)], fill="#2d6b3f", width=2)
    return img


def render_splash(size=SIZE) -> Image.Image:
    """Splash effect when beaver is caught."""
    img = Image.new("RGB", size, "#fbbf24")
    d = ImageDraw.Draw(img)
    cx, cy = 48, 48
    # Star burst
    for angle in range(0, 360, 30):
        rad = math.radians(angle)
        x2 = cx + int(40 * math.cos(rad))
        y2 = cy + int(40 * math.sin(rad))
        d.line([(cx, cy), (x2, y2)], fill="#f59e0b", width=3)
    d.text((cx, cy), "+1", font=_font(28), fill="#7c2d12", anchor="mm")
    return img


def render_miss(size=SIZE) -> Image.Image:
    """Miss — red X."""
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.line([(20, 20), (76, 76)], fill="#ef4444", width=6)
    d.line([(76, 20), (20, 76)], fill="#ef4444", width=6)
    d.text((48, 48), "-1", font=_font(22), fill="#fca5a5", anchor="mm")
    return img


def render_hud_score(score: int, size=SIZE) -> Image.Image:
    """Score display."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "SCORE", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(score), font=_font(32), fill="#fbbf24", anchor="mt")
    return img


def render_hud_timer(seconds_left: int, size=SIZE) -> Image.Image:
    """Timer display."""
    bg = "#991b1b" if seconds_left <= 5 else "#111827"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "TIME", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(seconds_left), font=_font(32), fill="#f87171" if seconds_left <= 5 else "#60a5fa", anchor="mt")
    return img


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "BEAVER", font=_font(16), fill="#fbbf24", anchor="mt")
    d.text((48, 52), "HUNT", font=_font(16), fill="#fbbf24", anchor="mt")
    return img


def render_hud_best(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(best), font=_font(28), fill="#34d399", anchor="mt")
    return img


def render_hud_level(level: int, size=SIZE) -> Image.Image:
    """Level / difficulty display."""
    # Color ramps from green to red as level increases
    if level <= 3:
        bg, clr = "#111827", "#34d399"
    elif level <= 6:
        bg, clr = "#111827", "#fbbf24"
    else:
        bg, clr = "#450a0a", "#f87171"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 20), "LEVEL", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(level), font=_font(32), fill=clr, anchor="mt")
    return img


def render_hud_speed(timeout: float, size=SIZE) -> Image.Image:
    """Speed indicator — how fast the beaver hides."""
    # Bar visualization
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 16), "SPEED", font=_font(12), fill="#9ca3af", anchor="mt")
    # Bar: full = fast (low timeout), empty = slow (high timeout)
    pct = 1.0 - (timeout - BEAVER_TIMEOUT_MIN) / (BEAVER_TIMEOUT_START - BEAVER_TIMEOUT_MIN)
    pct = max(0.0, min(1.0, pct))
    bar_w = 60
    bar_h = 12
    x0 = (96 - bar_w) // 2
    y0 = 40
    d.rectangle([x0, y0, x0 + bar_w, y0 + bar_h], outline="#4b5563")
    fill_w = int(bar_w * pct)
    if fill_w > 0:
        clr = "#ef4444" if pct > 0.7 else "#fbbf24" if pct > 0.4 else "#22c55e"
        d.rectangle([x0, y0, x0 + fill_w, y0 + bar_h], fill=clr)
    # Numeric
    d.text((48, 62), f"{timeout:.1f}s", font=_font(18), fill="#e5e7eb", anchor="mt")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_game_over(score: int, best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white", anchor="mm", align="center")
    return img


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
    return img


# ── game logic ───────────────────────────────────────────────────────

class BeaverGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.best = 0
        self.level = 1
        self.beaver_timeout = BEAVER_TIMEOUT_START
        self.catches_this_level = 0
        self.beaver_pos = -1
        self.running = False
        self.game_over = False
        self.time_left = GAME_DURATION
        self.lock = threading.Lock()
        self.beaver_timer = None
        self.game_timer = None
        # Pre-render reusable images
        self.img_beaver = render_beaver()
        self.img_hud_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_start = render_start()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def show_idle(self):
        """Show start screen."""
        self.game_over = False
        self.running = False
        # HUD row
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(0))
        self.set_key(2, render_hud_best(self.best))
        for k in range(3, 8):
            self.set_key(k, self.img_hud_empty)

        # Game area — show start button in center, rest grass
        for k in GAME_KEYS:
            if k == 20:  # center-ish
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, render_empty())

    def start_game(self):
        """Start a new round."""
        with self.lock:
            self.score = 0
            self.level = 1
            self.beaver_timeout = BEAVER_TIMEOUT_START
            self.catches_this_level = 0
            self.time_left = GAME_DURATION
            self.running = True
            self.game_over = False

        play_sfx("start")
        play_orc("start")

        # Clear game area
        for k in GAME_KEYS:
            self.set_key(k, render_empty())

        self._update_hud()
        self._spawn_beaver()

        # Start game clock
        self.game_timer = threading.Thread(target=self._game_clock, daemon=True)
        self.game_timer.start()

    def _game_clock(self):
        """Count down game timer."""
        while self.time_left > 0 and self.running:
            time.sleep(1)
            with self.lock:
                self.time_left -= 1
            self._update_hud()
            # Tick warning for last 5 seconds
            if self.time_left <= 5 and self.time_left > 0:
                play_sfx("tick")

        # Game over
        with self.lock:
            self.running = False
            self.game_over = True
            if self.score > self.best:
                self.best = self.score

        self._cancel_beaver_timer()
        # Clear beaver
        if self.beaver_pos >= 0:
            self.set_key(self.beaver_pos, render_empty())
            self.beaver_pos = -1

        # New best?
        if self.score > 0 and self.score >= self.best:
            play_sfx("newbest")
            play_orc("newbest")
        else:
            play_sfx("gameover")
            play_orc("gameover")

        self._show_game_over()

    def _show_game_over(self):
        """Flash game over screen."""
        # HUD
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_timer(0))
        for k in range(4, 8):
            self.set_key(k, self.img_hud_empty)

        # Show GAME OVER on all game keys
        go_img = render_game_over(self.score, self.best)
        for k in GAME_KEYS:
            if k == 20:
                self.set_key(k, self.img_start)  # restart button
            else:
                self.set_key(k, go_img if k in (18, 19, 20, 21) else render_empty())

    def _update_hud(self):
        self.set_key(0, self.img_hud_title)
        self.set_key(1, render_hud_score(self.score))
        self.set_key(2, render_hud_best(self.best))
        self.set_key(3, render_hud_timer(self.time_left))
        self.set_key(4, render_hud_level(self.level))
        self.set_key(5, render_hud_speed(self.beaver_timeout))

    def _spawn_beaver(self):
        """Place beaver on a random game key."""
        if not self.running:
            return
        available = [k for k in GAME_KEYS if k != self.beaver_pos]
        new_pos = random.choice(available)

        with self.lock:
            # Clear old position
            if self.beaver_pos >= 0:
                self.set_key(self.beaver_pos, render_empty())
            self.beaver_pos = new_pos
            self.set_key(new_pos, self.img_beaver)

        # Auto-move timer — beaver escapes (faster at higher levels)
        self._cancel_beaver_timer()
        self.beaver_timer = threading.Timer(self.beaver_timeout, self._beaver_escaped)
        self.beaver_timer.daemon = True
        self.beaver_timer.start()

    def _beaver_escaped(self):
        """Beaver wasn't caught in time."""
        if not self.running:
            return
        with self.lock:
            if self.beaver_pos >= 0:
                self.set_key(self.beaver_pos, render_empty())
        self._spawn_beaver()

    def _cancel_beaver_timer(self):
        if self.beaver_timer:
            self.beaver_timer.cancel()
            self.beaver_timer = None

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == 20 and not self.running:
            self.start_game()
            return

        if not self.running:
            return

        if key in GAME_KEYS:
            with self.lock:
                if key == self.beaver_pos:
                    # HIT!
                    self.score += 1
                    self.catches_this_level += 1
                    # Level up?
                    leveled = False
                    if self.catches_this_level >= LEVEL_EVERY:
                        self.catches_this_level = 0
                        self.level += 1
                        self.beaver_timeout = max(
                            BEAVER_TIMEOUT_MIN,
                            BEAVER_TIMEOUT_START - (self.level - 1) * BEAVER_SPEEDUP,
                        )
                        leveled = True
                    self._cancel_beaver_timer()
                    self.set_key(key, render_splash())
                    self._update_hud()
                    play_sfx("levelup" if leveled else "hit")
                    if leveled and self.level % 2 == 1:
                        play_orc("levelup")
                    # Brief flash then spawn new
                    threading.Timer(0.15, self._spawn_beaver).start()
                else:
                    # MISS — penalty
                    self.score = max(0, self.score - 1)
                    self.set_key(key, render_miss())
                    self._update_hud()
                    play_sfx("miss")
                    # Restore grass after flash
                    threading.Timer(0.3, lambda: self.set_key(key, render_empty())).start()


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
    print("BEAVER HUNT! Press the center button to start.")

    game = BeaverGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nBye! Final score:", game.score, "Best:", game.best)
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
