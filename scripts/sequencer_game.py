"""DJ Beats — Stream Deck step sequencer.

3-track drum machine (kick, snare, hihat) with 8 steps per track.
Synthesized drum sounds, multiple kits and preset patterns.

Layout (8x4 grid):
  Row 0: [BACK] [PLAY] [BPM] [BPM-] [BPM+] [KIT] [PATRN] [CLR]
  Row 1: Kick steps 1-8
  Row 2: Snare steps 1-8
  Row 3: HiHat steps 1-8

Usage:
    Launched from dashboard.py game menu.
"""

import math
import os
import random
import struct
import tempfile
import threading
import time
import wave

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.ImageHelpers import PILHelper

import sound_engine

# ── config ────────────────────────────────────────────────────────────

SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SAMPLE_RATE = 44100
STEPS = 8
TRACKS = 3  # kick, snare, hihat

TRACK_NAMES = ["KICK", "SNARE", "HIHAT"]
TRACK_COLORS_ON = ["#ef4444", "#3b82f6", "#fbbf24"]
TRACK_COLORS_OFF = ["#450a0a", "#172554", "#451a03"]
TRACK_COLORS_DIM = ["#7f1d1d", "#1e3a5f", "#78350f"]

# Grid keys: row 1 = keys 8-15, row 2 = 16-23, row 3 = 24-31
GRID_START = [8, 16, 24]  # first key of each track row

# Control keys (row 0)
KEY_PLAY = 1
KEY_BPM = 2
KEY_BPM_DOWN = 3
KEY_BPM_UP = 4
KEY_KIT = 5
KEY_PATTERN = 6
KEY_CLEAR = 7

BPM_MIN = 80
BPM_MAX = 200
BPM_STEP = 5
BPM_DEFAULT = 120

# ── kits (synthesis parameters) ──────────────────────────────────────

KITS = [
    {
        "name": "808",
        "kick": {"f_start": 160, "f_end": 35, "dur": 0.45, "decay": 6},
        "snare": {"freq": 180, "dur": 0.2, "noise_mix": 0.7, "decay": 12},
        "hihat": {"dur": 0.06, "decay": 45},
    },
    {
        "name": "HOUSE",
        "kick": {"f_start": 200, "f_end": 50, "dur": 0.3, "decay": 10},
        "snare": {"freq": 250, "dur": 0.15, "noise_mix": 0.5, "decay": 18},
        "hihat": {"dur": 0.1, "decay": 30},
    },
    {
        "name": "TECHNO",
        "kick": {"f_start": 180, "f_end": 40, "dur": 0.35, "decay": 8},
        "snare": {"freq": 300, "dur": 0.12, "noise_mix": 0.8, "decay": 22},
        "hihat": {"dur": 0.05, "decay": 50},
    },
    {
        "name": "TRAP",
        "kick": {"f_start": 120, "f_end": 25, "dur": 0.5, "decay": 5},
        "snare": {"freq": 200, "dur": 0.25, "noise_mix": 0.6, "decay": 10},
        "hihat": {"dur": 0.04, "decay": 55},
    },
]

# ── preset patterns ──────────────────────────────────────────────────

PATTERNS = [
    {"name": "EMPTY", "grid": [[0]*8, [0]*8, [0]*8]},
    {"name": "BASIC", "grid": [
        [1, 0, 0, 0, 1, 0, 0, 0],
        [0, 0, 1, 0, 0, 0, 1, 0],
        [1, 1, 1, 1, 1, 1, 1, 1],
    ]},
    {"name": "HOUSE", "grid": [
        [1, 0, 0, 0, 1, 0, 0, 0],
        [0, 0, 0, 0, 1, 0, 0, 0],
        [0, 0, 1, 0, 0, 0, 1, 0],
    ]},
    {"name": "TRAP", "grid": [
        [1, 0, 0, 1, 0, 0, 1, 0],
        [0, 0, 0, 0, 1, 0, 0, 1],
        [1, 1, 1, 1, 1, 1, 1, 1],
    ]},
    {"name": "DnB", "grid": [
        [1, 0, 0, 0, 0, 0, 1, 0],
        [0, 0, 1, 0, 0, 1, 0, 0],
        [1, 0, 1, 0, 1, 0, 1, 0],
    ]},
    {"name": "FUNK", "grid": [
        [1, 0, 0, 1, 0, 0, 0, 0],
        [0, 0, 1, 0, 0, 1, 0, 1],
        [1, 1, 0, 1, 1, 0, 1, 0],
    ]},
]


# ── font helper ──────────────────────────────────────────────────────

def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


# ── sound synthesis ──────────────────────────────────────────────────

def _write_wav(path: str, frames: list[int]):
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(struct.pack(f"<{len(frames)}h", *frames))


def _gen_kick(path: str, f_start=160, f_end=35, dur=0.45, decay=6):
    n = int(SAMPLE_RATE * dur)
    phase = 0.0
    frames = []
    for i in range(n):
        t = i / SAMPLE_RATE
        freq = f_end + (f_start - f_end) * math.exp(-t * decay * 2)
        phase += 2 * math.pi * freq / SAMPLE_RATE
        amp = math.exp(-t * decay) * 0.85
        sample = amp * math.sin(phase)
        frames.append(int(max(-1, min(1, sample)) * 32767))
    _write_wav(path, frames)


def _gen_snare(path: str, freq=180, dur=0.2, noise_mix=0.7, decay=12):
    n = int(SAMPLE_RATE * dur)
    frames = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = math.exp(-t * decay)
        noise = random.uniform(-1, 1) * noise_mix
        tone = math.sin(2 * math.pi * freq * t) * (1 - noise_mix)
        sample = env * (noise + tone) * 0.7
        frames.append(int(max(-1, min(1, sample)) * 32767))
    _write_wav(path, frames)


def _gen_hihat(path: str, dur=0.06, decay=45):
    n = int(SAMPLE_RATE * dur)
    frames = []
    for i in range(n):
        t = i / SAMPLE_RATE
        env = math.exp(-t * decay)
        noise = random.uniform(-1, 1)
        sample = env * noise * 0.5
        frames.append(int(max(-1, min(1, sample)) * 32767))
    _write_wav(path, frames)


# ── renderers ────────────────────────────────────────────────────────

def _render_step(track: int, step: int, active: bool, is_current: bool) -> Image.Image:
    if active:
        bg = TRACK_COLORS_ON[track]
    elif is_current:
        bg = TRACK_COLORS_DIM[track]
    else:
        bg = TRACK_COLORS_OFF[track]

    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)

    num_color = "white" if active else "#6b7280"
    d.text((48, 48), str(step + 1), font=_font(22), fill=num_color, anchor="mm")

    if is_current:
        d.rectangle([2, 2, 93, 93], outline="#ffffff", width=3)

    return img


def _render_play_btn(playing: bool) -> Image.Image:
    bg = "#16a34a" if playing else "#374151"
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    if playing:
        # Stop square
        d.rectangle([34, 28, 62, 56], fill="white")
        d.text((48, 74), "STOP", font=_font(12), fill="#bbf7d0", anchor="mm")
    else:
        # Play triangle
        d.polygon([(36, 26), (36, 58), (64, 42)], fill="white")
        d.text((48, 74), "PLAY", font=_font(12), fill="#9ca3af", anchor="mm")
    return img


def _render_bpm_btn(bpm: int) -> Image.Image:
    img = Image.new("RGB", SIZE, "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((48, 30), str(bpm), font=_font(28), fill="white", anchor="mm")
    d.text((48, 62), "BPM", font=_font(12), fill="#64748b", anchor="mm")
    return img


def _render_arrow_btn(label: str, direction: str) -> Image.Image:
    img = Image.new("RGB", SIZE, "#1e293b")
    d = ImageDraw.Draw(img)
    arrow = "-" if direction == "down" else "+"
    d.text((48, 34), arrow, font=_font(32), fill="#fbbf24", anchor="mm")
    d.text((48, 70), label, font=_font(10), fill="#64748b", anchor="mm")
    return img


def _render_kit_btn(kit_name: str) -> Image.Image:
    img = Image.new("RGB", SIZE, "#4c1d95")
    d = ImageDraw.Draw(img)
    d.text((48, 26), "KIT", font=_font(11), fill="#c4b5fd", anchor="mm")
    d.text((48, 52), kit_name, font=_font(16), fill="white", anchor="mm")
    return img


def _render_pattern_btn(pattern_name: str) -> Image.Image:
    img = Image.new("RGB", SIZE, "#1e3a5f")
    d = ImageDraw.Draw(img)
    d.text((48, 26), "PATRN", font=_font(10), fill="#93c5fd", anchor="mm")
    d.text((48, 52), pattern_name, font=_font(14), fill="white", anchor="mm")
    return img


def _render_clear_btn() -> Image.Image:
    img = Image.new("RGB", SIZE, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "CLR", font=_font(18), fill="#fca5a5", anchor="mm")
    d.text((48, 58), "ALL", font=_font(12), fill="#fca5a5", anchor="mm")
    return img


# ── game class ───────────────────────────────────────────────────────

class SequencerGame:
    def __init__(self, deck):
        self.deck = deck
        self.running = True
        self.playing = False
        self.current_step = 0
        self.bpm = BPM_DEFAULT
        self.kit_idx = 0
        self.pattern_idx = 0
        self.lock = threading.Lock()
        self._seq_thread = None

        # 3 tracks × 8 steps
        self.grid = [[0] * STEPS for _ in range(TRACKS)]

        # Sound file paths
        self._sound_dir = tempfile.mkdtemp(prefix="djbeats_")
        self._sounds = ["", "", ""]
        self._generate_sounds()

    def _generate_sounds(self):
        kit = KITS[self.kit_idx]
        kick_path = os.path.join(self._sound_dir, "kick.wav")
        snare_path = os.path.join(self._sound_dir, "snare.wav")
        hihat_path = os.path.join(self._sound_dir, "hihat.wav")

        _gen_kick(kick_path, **kit["kick"])
        _gen_snare(snare_path, **kit["snare"])
        _gen_hihat(hihat_path, **kit["hihat"])

        self._sounds = [kick_path, snare_path, hihat_path]

    def _set_key(self, pos: int, img: Image.Image):
        try:
            native = PILHelper.to_native_key_format(self.deck, img)
            with self.deck:
                self.deck.set_key_image(pos, native)
        except Exception:
            pass

    # ── display ───────────────────────────────────────────────────────

    def show_idle(self):
        self.deck.reset()
        self._render_controls()
        self._render_grid()

    def _render_controls(self):
        self._set_key(KEY_PLAY, _render_play_btn(self.playing))
        self._set_key(KEY_BPM, _render_bpm_btn(self.bpm))
        self._set_key(KEY_BPM_DOWN, _render_arrow_btn("SLOWER", "down"))
        self._set_key(KEY_BPM_UP, _render_arrow_btn("FASTER", "up"))
        self._set_key(KEY_KIT, _render_kit_btn(KITS[self.kit_idx]["name"]))
        self._set_key(KEY_PATTERN, _render_pattern_btn(PATTERNS[self.pattern_idx]["name"]))
        self._set_key(KEY_CLEAR, _render_clear_btn())

    def _render_grid(self):
        with self.lock:
            step = self.current_step
            playing = self.playing
        for track in range(TRACKS):
            base_key = GRID_START[track]
            for s in range(STEPS):
                active = self.grid[track][s]
                is_current = playing and (s == step)
                img = _render_step(track, s, active, is_current)
                self._set_key(base_key + s, img)

    # ── sequencer loop ────────────────────────────────────────────────

    def _start_playing(self):
        self.playing = True
        self.current_step = 0
        self._set_key(KEY_PLAY, _render_play_btn(True))
        self._seq_thread = threading.Thread(target=self._seq_loop, daemon=True)
        self._seq_thread.start()

    def _stop_playing(self):
        self.playing = False
        self._set_key(KEY_PLAY, _render_play_btn(False))
        self._render_grid()

    def _seq_loop(self):
        next_time = time.time()
        while self.playing and self.running:
            step_dur = 60.0 / self.bpm / 2  # 8th notes

            with self.lock:
                step = self.current_step

            # Play active tracks for this step
            for track in range(TRACKS):
                if self.grid[track][step]:
                    sound_engine._play(self._sounds[track])

            # Update display
            self._render_grid()

            # Advance
            with self.lock:
                self.current_step = (self.current_step + 1) % STEPS

            next_time += step_dur
            sleep_time = next_time - time.time()
            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                # Fell behind, reset timing
                next_time = time.time()

    # ── input ─────────────────────────────────────────────────────────

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Control row
        if key == KEY_PLAY:
            if self.playing:
                self._stop_playing()
            else:
                self._start_playing()
            return

        if key == KEY_BPM_DOWN:
            self.bpm = max(BPM_MIN, self.bpm - BPM_STEP)
            self._set_key(KEY_BPM, _render_bpm_btn(self.bpm))
            return

        if key == KEY_BPM_UP:
            self.bpm = min(BPM_MAX, self.bpm + BPM_STEP)
            self._set_key(KEY_BPM, _render_bpm_btn(self.bpm))
            return

        if key == KEY_KIT:
            self.kit_idx = (self.kit_idx + 1) % len(KITS)
            self._generate_sounds()
            self._set_key(KEY_KIT, _render_kit_btn(KITS[self.kit_idx]["name"]))
            return

        if key == KEY_PATTERN:
            self.pattern_idx = (self.pattern_idx + 1) % len(PATTERNS)
            pat = PATTERNS[self.pattern_idx]
            for t in range(TRACKS):
                self.grid[t] = list(pat["grid"][t])
            self._set_key(KEY_PATTERN, _render_pattern_btn(pat["name"]))
            self._render_grid()
            return

        if key == KEY_CLEAR:
            self.grid = [[0] * STEPS for _ in range(TRACKS)]
            self.pattern_idx = 0
            self._set_key(KEY_PATTERN, _render_pattern_btn("EMPTY"))
            self._render_grid()
            return

        # Grid toggle
        for track in range(TRACKS):
            base = GRID_START[track]
            if base <= key < base + STEPS:
                step = key - base
                self.grid[track][step] ^= 1
                with self.lock:
                    cur = self.current_step
                is_current = self.playing and (step == cur)
                img = _render_step(track, step, self.grid[track][step], is_current)
                self._set_key(key, img)
                # Play sound on toggle-on for feedback
                if self.grid[track][step] and not self.playing:
                    sound_engine._play(self._sounds[track])
                return

    def _cancel_all_timers(self):
        self.running = False
        self.playing = False
