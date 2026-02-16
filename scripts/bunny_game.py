"""Bunny Blitz — Stream Deck mini-game.

Full-field bunny hunt! Multiple bunnies, fox decoys, faster pace.
Only 2 HUD keys (score + timer), rest = game area (30 keys).

Usage:
    uv run python scripts/bunny_game.py
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

# ── config ───────────────────────────────────────────────────────────
HUD_SCORE_KEY = 0
HUD_TIMER_KEY = 7
GAME_KEYS = [k for k in range(32) if k not in (HUD_SCORE_KEY, HUD_TIMER_KEY)]
# 30 game keys: 1-6, 8-31

BUNNY_TIMEOUT_START = 3.5        # initial seconds before bunny hops away
BUNNY_TIMEOUT_MIN = 0.30        # fastest possible
BUNNY_SPEEDUP = 0.12            # seconds faster per level
LEVEL_EVERY = 2                 # level up every N catches (faster than beaver's 3)
GAME_DURATION = 40              # seconds (shorter = more intense)

MAX_BUNNIES = 3                 # max simultaneous bunnies
FOX_START_LEVEL = 3             # foxes appear from this level
CARROT_CHANCE = 0.12            # chance of bonus carrot spawn

SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

# ── voice lines (Helldivers 2 — war vibes for hunting) ───────────────
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": [
        "hd2_helldiver/sounds/ReadyToLiberate1.mp3",
        "hd2_helldiver/sounds/ReadyToLiberate2.mp3",
        "hd2_helldiver/sounds/ReportingForDuty1.mp3",
        "hd2_helldiver/sounds/PointMeToTheEnemy.mp3",
    ],
    "levelup": [
        "hd2_helldiver/sounds/GetSome.mp3",
        "hd2_helldiver/sounds/SayHelloToDemocracy.mp3",
        "hd2_helldiver/sounds/DemocracyForAll.mp3",
        "hd2_helldiver/sounds/FullAutoLaugh.mp3",
    ],
    "gameover": [
        "hd2_helldiver/sounds/CancelThat.mp3",
        "hd2_helldiver/sounds/NeverMind.mp3",
        "hd2_helldiver/sounds/ImSorry.mp3",
        "hd2_helldiver/sounds/CanistersEmpty.mp3",
    ],
    "newbest": [
        "hd2_helldiver/sounds/ObjectiveCompleted.mp3",
        "hd2_helldiver/sounds/FreedomNeverSleeps.mp3",
        "hd2_helldiver/sounds/LibertyProsperityDemocracy.mp3",
        "hd2_helldiver/sounds/DemocracyHasLanded.mp3",
    ],
    "fox": [
        "hd2_helldiver/sounds/Negative.mp3",
        "hd2_helldiver/sounds/LastReload.mp3",
    ],
}

_last_voice_time: float = 0
VOICE_COOLDOWN = 4.0


def play_voice(event: str):
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


# ── 8-bit sound engine ───────────────────────────────────────────────
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
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="bunny-sfx-")
    v = SFX_VOLUME

    # HIT — bouncy chirp (higher pitch than beaver, more playful)
    s = _square(660, 0.03, v * 0.5, 0.25) + _triangle(880, 0.03, v * 0.6) + _triangle(1047, 0.05, v * 0.7)
    _write_wav(os.path.join(_sfx_dir, "hit.wav"), s)
    _sfx_cache["hit"] = os.path.join(_sfx_dir, "hit.wav")

    # MISS — quick low buzz
    s = _square(200, 0.06, v * 0.4, 0.5) + _square(150, 0.08, v * 0.3, 0.5)
    _write_wav(os.path.join(_sfx_dir, "miss.wav"), s)
    _sfx_cache["miss"] = os.path.join(_sfx_dir, "miss.wav")

    # FOX — nasty buzz + descending
    s = _merge(
        _noise(0.06, v * 0.4),
        _square(300, 0.05, v * 0.3, 0.3) + _square(200, 0.08, v * 0.35, 0.5) + _square(120, 0.1, v * 0.3, 0.5),
    )
    _write_wav(os.path.join(_sfx_dir, "fox.wav"), s)
    _sfx_cache["fox"] = os.path.join(_sfx_dir, "fox.wav")

    # CARROT — magical sparkle
    s = (_triangle(784, 0.04, v * 0.4) + _triangle(1047, 0.04, v * 0.5) +
         _triangle(1319, 0.04, v * 0.55) + _triangle(1568, 0.08, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "carrot.wav"), s)
    _sfx_cache["carrot"] = os.path.join(_sfx_dir, "carrot.wav")

    # LEVEL UP — rapid arpeggio
    s = (_square(330, 0.04, v * 0.4, 0.25) +
         _square(440, 0.04, v * 0.45, 0.25) +
         _square(554, 0.04, v * 0.5, 0.25) +
         _square(660, 0.04, v * 0.5, 0.25) +
         _triangle(880, 0.12, v * 0.65))
    _write_wav(os.path.join(_sfx_dir, "levelup.wav"), s)
    _sfx_cache["levelup"] = os.path.join(_sfx_dir, "levelup.wav")

    # START — energetic power-up
    s = (_triangle(440, 0.05, v * 0.4) +
         _triangle(554, 0.05, v * 0.45) +
         _triangle(660, 0.05, v * 0.5) +
         _triangle(880, 0.1, v * 0.6))
    _write_wav(os.path.join(_sfx_dir, "start.wav"), s)
    _sfx_cache["start"] = os.path.join(_sfx_dir, "start.wav")

    # GAME OVER
    s = (_square(660, 0.1, v * 0.5, 0.5) +
         _square(440, 0.1, v * 0.45, 0.5) +
         _square(330, 0.12, v * 0.4, 0.5) +
         _square(220, 0.25, v * 0.35, 0.5))
    _write_wav(os.path.join(_sfx_dir, "gameover.wav"), s)
    _sfx_cache["gameover"] = os.path.join(_sfx_dir, "gameover.wav")

    # TICK — warning beep
    s = _square(990, 0.03, v * 0.3, 0.25)
    _write_wav(os.path.join(_sfx_dir, "tick.wav"), s)
    _sfx_cache["tick"] = os.path.join(_sfx_dir, "tick.wav")

    # SPAWN — soft pop
    s = _merge(_noise(0.015, v * 0.15), _triangle(1200, 0.025, v * 0.2))
    _write_wav(os.path.join(_sfx_dir, "spawn.wav"), s)
    _sfx_cache["spawn"] = os.path.join(_sfx_dir, "spawn.wav")

    # NEW BEST — victory fanfare
    s = (_triangle(660, 0.06, v * 0.5) +
         _triangle(880, 0.06, v * 0.55) +
         _triangle(1047, 0.06, v * 0.6) +
         _triangle(1319, 0.2, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "newbest.wav"), s)
    _sfx_cache["newbest"] = os.path.join(_sfx_dir, "newbest.wav")

    # COMBO — quick ascending pips
    s = _triangle(880, 0.02, v * 0.3) + _triangle(1100, 0.02, v * 0.4) + _triangle(1320, 0.04, v * 0.5)
    _write_wav(os.path.join(_sfx_dir, "combo.wav"), s)
    _sfx_cache["combo"] = os.path.join(_sfx_dir, "combo.wav")


def play_sfx(name: str):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# ── renderers ────────────────────────────────────────────────────────

def render_bunny(size=SIZE) -> Image.Image:
    """Draw a chunky pixel-art bunny face — white with pink ears."""
    img = Image.new("RGB", size, "#1a3a1a")
    d = ImageDraw.Draw(img)

    # Long ears
    d.ellipse([24, 2, 38, 38], fill="#e8e8e8")   # left ear outer
    d.ellipse([58, 2, 72, 38], fill="#e8e8e8")   # right ear outer
    d.ellipse([28, 6, 34, 34], fill="#f9a8d4")    # left ear inner (pink)
    d.ellipse([62, 6, 68, 34], fill="#f9a8d4")    # right ear inner (pink)

    # Head — white round
    d.ellipse([18, 28, 78, 82], fill="#f0f0f0")

    # Eyes — big black with white highlight
    d.ellipse([30, 40, 42, 54], fill="black")
    d.ellipse([54, 40, 66, 54], fill="black")
    d.ellipse([33, 42, 37, 46], fill="white")     # left highlight
    d.ellipse([57, 42, 61, 46], fill="white")     # right highlight

    # Nose — pink triangle
    d.polygon([(48, 56), (42, 62), (54, 62)], fill="#f472b6")

    # Whiskers
    d.line([(22, 58), (38, 56)], fill="#9ca3af", width=1)
    d.line([(22, 64), (38, 62)], fill="#9ca3af", width=1)
    d.line([(58, 56), (74, 58)], fill="#9ca3af", width=1)
    d.line([(58, 62), (74, 64)], fill="#9ca3af", width=1)

    # Teeth — two small
    d.rectangle([43, 64, 48, 72], fill="white")
    d.rectangle([50, 64, 55, 72], fill="white")

    return img


def render_fox(size=SIZE) -> Image.Image:
    """Draw a sneaky fox face — orange with pointy ears. DECOY!"""
    img = Image.new("RGB", size, "#1a3a1a")
    d = ImageDraw.Draw(img)

    # Pointy ears
    d.polygon([(20, 36), (30, 4), (42, 32)], fill="#ea580c")
    d.polygon([(54, 32), (66, 4), (76, 36)], fill="#ea580c")
    d.polygon([(25, 30), (30, 10), (37, 28)], fill="#1a1a1a")  # inner left
    d.polygon([(59, 28), (66, 10), (71, 30)], fill="#1a1a1a")  # inner right

    # Head
    d.ellipse([16, 26, 80, 82], fill="#ea580c")
    # Muzzle — lighter
    d.ellipse([30, 50, 66, 82], fill="#fdba74")

    # Eyes — sly, narrow
    d.ellipse([28, 38, 42, 50], fill="#fef08a")
    d.ellipse([54, 38, 68, 50], fill="#fef08a")
    d.ellipse([32, 42, 38, 48], fill="black")     # slit pupil
    d.ellipse([58, 42, 64, 48], fill="black")

    # Nose
    d.ellipse([42, 54, 54, 62], fill="#1a1a1a")

    # Smirk
    d.arc([36, 58, 60, 74], 0, 180, fill="#92400e", width=2)

    return img


def render_carrot(size=SIZE) -> Image.Image:
    """Bonus carrot tile — orange carrot on green field."""
    img = Image.new("RGB", size, "#1a3a1a")
    d = ImageDraw.Draw(img)

    # Carrot body (orange triangle)
    d.polygon([(48, 82), (32, 30), (64, 30)], fill="#f97316")
    d.polygon([(48, 82), (36, 40), (60, 40)], fill="#ea580c")

    # Carrot top (green leaves)
    d.polygon([(48, 30), (38, 8), (48, 18)], fill="#22c55e")
    d.polygon([(48, 30), (58, 8), (48, 18)], fill="#16a34a")
    d.polygon([(48, 30), (48, 4)], fill="#15803d")

    # Sparkle stars
    d.text((16, 20), "*", font=_font(16), fill="#fde047", anchor="mm")
    d.text((76, 50), "*", font=_font(12), fill="#fde047", anchor="mm")

    # "+3" label
    d.text((48, 88), "+3", font=_font(12), fill="#fde047", anchor="mb")

    return img


def render_field(size=SIZE) -> Image.Image:
    """Empty meadow tile — lighter green field with flowers."""
    img = Image.new("RGB", size, "#1a3a1a")
    d = ImageDraw.Draw(img)
    # Grass tufts
    for _ in range(4):
        x = random.randint(10, 80)
        y = random.randint(50, 85)
        d.line([(x, y), (x - 3, y - 10)], fill="#2d6b3f", width=2)
        d.line([(x, y), (x + 4, y - 8)], fill="#2d6b3f", width=2)
    # Occasional tiny flower
    if random.random() < 0.3:
        fx = random.randint(15, 75)
        fy = random.randint(15, 45)
        clr = random.choice(["#fde047", "#f9a8d4", "#93c5fd"])
        d.ellipse([fx - 3, fy - 3, fx + 3, fy + 3], fill=clr)
    return img


def render_hit_splash(points: int = 1, size=SIZE) -> Image.Image:
    """Splash when bunny is caught."""
    img = Image.new("RGB", size, "#fbbf24")
    d = ImageDraw.Draw(img)
    cx, cy = 48, 48
    for angle in range(0, 360, 30):
        rad = math.radians(angle)
        x2 = cx + int(38 * math.cos(rad))
        y2 = cy + int(38 * math.sin(rad))
        d.line([(cx, cy), (x2, y2)], fill="#f59e0b", width=3)
    label = f"+{points}" if points > 0 else str(points)
    d.text((cx, cy), label, font=_font(28), fill="#7c2d12", anchor="mm")
    return img


def render_fox_hit(size=SIZE) -> Image.Image:
    """Penalty splash — clicked a fox!"""
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.line([(20, 20), (76, 76)], fill="#ef4444", width=6)
    d.line([(76, 20), (20, 76)], fill="#ef4444", width=6)
    d.text((48, 48), "-3", font=_font(26), fill="#fca5a5", anchor="mm")
    return img


def render_miss(size=SIZE) -> Image.Image:
    """Miss — clicked empty."""
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.line([(24, 24), (72, 72)], fill="#ef4444", width=4)
    d.line([(72, 24), (24, 72)], fill="#ef4444", width=4)
    d.text((48, 48), "-1", font=_font(20), fill="#fca5a5", anchor="mm")
    return img


# ── HUD renderers (compact — only 2 keys) ───────────────────────────

def render_hud_score(score: int, level: int, combo: int, size=SIZE) -> Image.Image:
    """Compact score HUD: score + level + combo."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 6), "BUNNY", font=_font(10), fill="#f9a8d4", anchor="mt")
    d.text((48, 18), "BLITZ", font=_font(10), fill="#f9a8d4", anchor="mt")
    # Score big
    d.text((48, 42), str(score), font=_font(26), fill="#fbbf24", anchor="mt")
    # Level + combo bar
    lvl_txt = f"LV{level}"
    combo_txt = f"x{combo}" if combo > 1 else ""
    d.text((10, 78), lvl_txt, font=_font(11), fill="#60a5fa", anchor="lm")
    if combo_txt:
        d.text((86, 78), combo_txt, font=_font(11), fill="#f87171", anchor="rm")
    return img


def render_hud_timer(seconds_left: int, best: int, size=SIZE) -> Image.Image:
    """Compact timer HUD: time + best."""
    bg = "#991b1b" if seconds_left <= 5 else "#111827"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 6), "TIME", font=_font(10), fill="#9ca3af", anchor="mt")
    clr = "#f87171" if seconds_left <= 5 else "#60a5fa"
    d.text((48, 26), str(seconds_left), font=_font(28), fill=clr, anchor="mt")
    d.text((48, 64), "BEST", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 78), str(best), font=_font(18), fill="#34d399", anchor="mt")
    return img


def render_hud_idle_score(best: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 14), "BUNNY", font=_font(13), fill="#f9a8d4", anchor="mt")
    d.text((48, 32), "BLITZ", font=_font(13), fill="#f9a8d4", anchor="mt")
    d.text((48, 56), "BEST", font=_font(10), fill="#9ca3af", anchor="mt")
    d.text((48, 72), str(best), font=_font(20), fill="#34d399", anchor="mt")
    return img


def render_hud_idle_timer(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "30 KEYS", font=_font(11), fill="#9ca3af", anchor="mt")
    d.text((48, 48), "FULL", font=_font(12), fill="#60a5fa", anchor="mt")
    d.text((48, 64), "FIELD", font=_font(12), fill="#60a5fa", anchor="mt")
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
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white", anchor="mm", align="center")
    return img


# ── game logic ───────────────────────────────────────────────────────

# Entity types on the field
EMPTY = 0
BUNNY = 1
FOX = 2
CARROT = 3


class BunnyGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.best = scores.load_best("bunny")
        self.level = 1
        self.combo = 0                        # consecutive hits
        self.bunny_timeout = BUNNY_TIMEOUT_START
        self.catches_this_level = 0
        self.running = False
        self.game_over = False
        self.time_left = GAME_DURATION
        self.lock = threading.Lock()
        self.timers: list[threading.Timer] = []
        self.game_timer = None

        # Field state: key -> entity type
        self.field: dict[int, int] = {}

        # Pre-render reusable images
        self.img_bunny = render_bunny()
        self.img_fox = render_fox()
        self.img_carrot = render_carrot()
        self.img_start = render_start()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _cancel_all_timers(self):
        for t in self.timers:
            t.cancel()
        self.timers.clear()

    def _add_timer(self, delay: float, func, *args) -> threading.Timer:
        t = threading.Timer(delay, func, args=args)
        t.daemon = True
        self.timers.append(t)
        t.start()
        return t

    # ── idle screen ──────────────────────────────────────────────────

    def show_idle(self):
        self.game_over = False
        self.running = False

        self.set_key(HUD_SCORE_KEY, render_hud_idle_score(self.best))
        self.set_key(HUD_TIMER_KEY, render_hud_idle_timer())

        # Game area — start button in center (key 20), rest field
        for k in GAME_KEYS:
            if k == 20:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, render_field())

    # ── start game ───────────────────────────────────────────────────

    def start_game(self):
        with self.lock:
            self.score = 0
            self.level = 1
            self.combo = 0
            self.bunny_timeout = BUNNY_TIMEOUT_START
            self.catches_this_level = 0
            self.time_left = GAME_DURATION
            self.running = True
            self.game_over = False
            self.field.clear()

        play_sfx("start")
        play_voice("start")

        # Clear game area
        for k in GAME_KEYS:
            self.field[k] = EMPTY
            self.set_key(k, render_field())

        self._update_hud()

        # Spawn initial bunny
        self._spawn_wave()

        # Start game clock
        self.game_timer = threading.Thread(target=self._game_clock, daemon=True)
        self.game_timer.start()

    # ── game clock ───────────────────────────────────────────────────

    def _game_clock(self):
        while self.time_left > 0 and self.running:
            time.sleep(1)
            with self.lock:
                self.time_left -= 1
            self._update_hud()
            if self.time_left <= 5 and self.time_left > 0:
                play_sfx("tick")

        with self.lock:
            self.running = False
            self.game_over = True
            if self.score > self.best:
                self.best = self.score
                scores.save_best("bunny", self.best)

        self._cancel_all_timers()
        self._clear_entities()

        if self.score > 0 and self.score >= self.best:
            play_sfx("newbest")
            play_voice("newbest")
        else:
            play_sfx("gameover")
            play_voice("gameover")

        self._show_game_over()

    def _clear_entities(self):
        """Remove all entities from field."""
        for k in GAME_KEYS:
            if self.field.get(k, EMPTY) != EMPTY:
                self.field[k] = EMPTY
                self.set_key(k, render_field())

    def _show_game_over(self):
        self.set_key(HUD_SCORE_KEY, render_hud_score(self.score, self.level, 0))
        self.set_key(HUD_TIMER_KEY, render_hud_timer(0, self.best))

        go_img = render_game_over()
        center_keys = [19, 20, 21]
        for k in GAME_KEYS:
            if k == 20:
                self.set_key(k, self.img_start)
            elif k in center_keys:
                self.set_key(k, go_img)
            else:
                self.set_key(k, render_field())

    # ── HUD ──────────────────────────────────────────────────────────

    def _update_hud(self):
        self.set_key(HUD_SCORE_KEY, render_hud_score(self.score, self.level, self.combo))
        self.set_key(HUD_TIMER_KEY, render_hud_timer(self.time_left, self.best))

    # ── spawning ─────────────────────────────────────────────────────

    def _max_bunnies(self) -> int:
        """How many bunnies can be on field at once — ramps with level."""
        if self.level < 3:
            return 1
        elif self.level < 5:
            return 2
        else:
            return MAX_BUNNIES

    def _spawn_wave(self):
        """Spawn bunnies (+ maybe fox/carrot) up to max count."""
        if not self.running:
            return

        # Count current entities
        current_bunnies = sum(1 for v in self.field.values() if v == BUNNY)
        target = self._max_bunnies()

        available = [k for k in GAME_KEYS if self.field.get(k, EMPTY) == EMPTY]
        if not available:
            return

        while current_bunnies < target and available:
            pos = random.choice(available)
            available.remove(pos)

            # Decide what to spawn
            entity = BUNNY
            if self.level >= FOX_START_LEVEL and random.random() < 0.2:
                entity = FOX
            elif random.random() < CARROT_CHANCE:
                entity = CARROT

            self.field[pos] = entity
            if entity == BUNNY:
                self.set_key(pos, self.img_bunny)
                current_bunnies += 1
            elif entity == FOX:
                self.set_key(pos, self.img_fox)
            elif entity == CARROT:
                self.set_key(pos, self.img_carrot)

            # Auto-despawn timer for this entity
            self._add_timer(self.bunny_timeout, self._entity_escaped, pos)

    def _entity_escaped(self, pos: int):
        """Entity wasn't caught in time — it hops away."""
        if not self.running:
            return
        with self.lock:
            entity = self.field.get(pos, EMPTY)
            if entity == EMPTY:
                return
            self.field[pos] = EMPTY
            self.set_key(pos, render_field())
            # Reset combo on missed bunny
            if entity == BUNNY:
                self.combo = 0
                self._update_hud()

        # Spawn new wave
        self._spawn_wave()

    # ── key handling ─────────────────────────────────────────────────

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
            entity = self.field.get(key, EMPTY)

            if entity == BUNNY:
                # HIT!
                self.combo += 1
                points = 1
                if self.combo >= 5:
                    points = 3
                elif self.combo >= 3:
                    points = 2
                self.score += points
                self.catches_this_level += 1

                # Level up?
                leveled = False
                if self.catches_this_level >= LEVEL_EVERY:
                    self.catches_this_level = 0
                    self.level += 1
                    self.bunny_timeout = max(
                        BUNNY_TIMEOUT_MIN,
                        BUNNY_TIMEOUT_START - (self.level - 1) * BUNNY_SPEEDUP,
                    )
                    leveled = True

                self.field[key] = EMPTY
                self.set_key(key, render_hit_splash(points))
                self._update_hud()

                if leveled:
                    play_sfx("levelup")
                    if self.level % 2 == 0:
                        play_voice("levelup")
                elif self.combo >= 3:
                    play_sfx("combo")
                else:
                    play_sfx("hit")

                # Brief flash then restore + spawn
                self._add_timer(0.15, self._after_hit, key)

            elif entity == FOX:
                # FOX PENALTY!
                self.score = max(0, self.score - 3)
                self.combo = 0
                self.field[key] = EMPTY
                self.set_key(key, render_fox_hit())
                self._update_hud()
                play_sfx("fox")
                play_voice("fox")
                self._add_timer(0.3, self._restore_field, key)

            elif entity == CARROT:
                # BONUS!
                self.score += 3
                self.combo += 1
                self.field[key] = EMPTY
                self.set_key(key, render_hit_splash(3))
                self._update_hud()
                play_sfx("carrot")
                self._add_timer(0.15, self._after_hit, key)

            else:
                # MISS — empty field
                self.score = max(0, self.score - 1)
                self.combo = 0
                self.set_key(key, render_miss())
                self._update_hud()
                play_sfx("miss")
                self._add_timer(0.25, self._restore_field, key)

    def _after_hit(self, key: int):
        """Restore tile and spawn new wave."""
        if not self.running:
            return
        self.set_key(key, render_field())
        self._spawn_wave()

    def _restore_field(self, key: int):
        """Restore a field tile after miss/fox flash."""
        if not self.running:
            return
        self.field[key] = EMPTY
        self.set_key(key, render_field())


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
        print("Sound effects: OFF (generation failed)")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("BUNNY BLITZ! Press the center button to start.")

    game = BunnyGame(deck)
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
