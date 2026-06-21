"""808/909 drum machine — control surface for DrumMachine.

All 16 Drum-Rack lanes live in the engine; the 8-key top row groups them
by family. Tap a slot to select it; tap the SAME slot again to cycle to a
related sound (e.g. SNARE↔Rim, HAT closed↔open, TOM mid/lo/hi). Rows 1-2
are the selected lane's 16 steps (tap = toggle, green playhead). Row 3:
play/stop (bar-quantised), clear lane, clear all, home.
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface

from drum_engine import machine, DRUMS, N_STEPS, BEATS
from isobar_engine import VOICES

VOICE_ROW = range(0, 8)
STEP_ROW = range(8, 24)
KEY_PLAY = 24
KEY_CLEAR = 25
KEY_CLEAR_ALL = 26
KEY_BEAT = 27      # cycle a built-in groove
KEY_SRC = 28       # link the active lane to a GEN voice's rhythm
KEY_HOME = ControlSurface.HOME_KEY  # 31
FPS = 14.0

# (group label, [lane indices, in cycle order], colour). Covers all 16 lanes.
GROUPS = [
    ("KICK",  [0],         "#ef4444"),
    ("SNARE", [2, 1],      "#f97316"),   # Snare, Rim
    ("HAT",   [6, 10],     "#eab308"),   # Closed, Open
    ("CLAP",  [3],         "#06b6d4"),
    ("TOM",   [9, 8, 11],  "#a855f7"),   # Mid, Lo, Hi
    ("CONGA", [5, 4, 7],   "#14b8a6"),   # Mid, Lo, Hi
    ("PERC",  [12, 15],    "#ec4899"),   # Maracas, Claves
    ("CYM",   [13, 14],    "#84cc16"),   # Cymbal, Cow Bell
]


class DrumControl(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.group = 0
        self.lane_sel = [0] * len(GROUPS)   # selected lane within each group
        self.beat_idx = 0
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        self.running = True
        self.render()
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()

    def on_teardown(self) -> None:
        pass

    def active_lane(self) -> int:
        return GROUPS[self.group][1][self.lane_sel[self.group]]

    def _poll(self) -> None:
        frame = 1.0 / FPS
        while self.running:
            try:
                snap = machine.snapshot()
                self._paint_steps(snap)
                self.set_key(KEY_PLAY, self._play_img(snap))
            except Exception:
                pass
            time.sleep(frame)

    # -- rendering -----------------------------------------------------

    def _play_img(self, snap: dict):
        if snap.get("pending"):
            blink = int(time.monotonic() * 2) % 2
            return deck_ui.btn("#a16207" if blink else "#3f2d06",
                               [("▶", 28, "#fde68a"), ("queued", 10, "#fde68a")])
        if snap.get("armed"):
            return deck_ui.btn("#16a34a", [("■", 28, "#fff"), ("PLAY", 12, "#d1fae5")])
        return deck_ui.btn("#374151", [("▶", 28, "#fff"), ("PLAY", 12, "#d1d5db")])

    def _step_img(self, on: bool, playhead: bool, color: str, beat: bool) -> Image.Image:
        if playhead:
            bg, dot, r = "#052e16", ("#4ade80" if on else "#16341f"), (22 if on else 14)
        elif on:
            bg, dot, r = "#0b0f1a", color, 22
        else:
            bg, dot, r = ("#11161f" if beat else "#0b0f1a"), "#1e293b", 9
        img = Image.new("RGB", deck_ui.SIZE, bg)
        d = ImageDraw.Draw(img)
        d.ellipse([48 - r, 48 - r, 48 + r, 48 + r], fill=dot)
        return img

    def _paint_steps(self, snap: dict) -> None:
        step = snap["step"] if (snap["running"] and snap["armed"]) else -1
        lane = self.active_lane()
        src = snap["source"][lane]
        if src is not None and src in VOICES:
            # Lane follows a GEN voice — show that rhythm (read-only, lilac).
            gp = VOICES[src].snapshot()["pattern"]
            n = len(gp) or 1
            for i in range(N_STEPS):
                on = gp[i % n] > 0
                self.set_key(STEP_ROW.start + i, self._step_img(on, i == step, "#a78bfa", i % 4 == 0))
        else:
            color = GROUPS[self.group][2]
            row = snap["patterns"][lane]
            for i in range(N_STEPS):
                self.set_key(STEP_ROW.start + i, self._step_img(bool(row[i]), i == step, color, i % 4 == 0))

    def _paint_voices(self, snap: dict) -> None:
        for g, (_label, lanes, color) in enumerate(GROUPS):
            lane = lanes[self.lane_sel[g]]
            name = DRUMS[lane][0]
            cur = (g == self.group)
            has = any(any(snap["patterns"][ln]) for ln in lanes)
            bg = color if cur else ("#1f2937" if has else "#111827")
            lines = [(name, 13, "#fff" if cur else "#cbd5e1")]
            if len(lanes) > 1:
                lines.append((f"{self.lane_sel[g] + 1}/{len(lanes)} ↻", 9,
                              "#fde68a" if cur else "#64748b"))
            self.set_key(g, deck_ui.btn(bg, lines, border="#f8fafc" if cur else None))

    def render(self) -> None:
        if not self.running:
            return
        snap = machine.snapshot()
        self._paint_voices(snap)
        self._paint_steps(snap)
        self.set_key(KEY_PLAY, self._play_img(snap))
        lane = self.active_lane()
        self.set_key(KEY_CLEAR, deck_ui.btn("#7f1d1d", [("CLEAR", 13, "#fecaca"),
                                                        (DRUMS[lane][0], 9, "#f87171")]))
        self.set_key(KEY_CLEAR_ALL, deck_ui.btn("#450a0a", [("CLEAR", 13, "#fecaca"), ("all", 9, "#f87171")]))
        self.set_key(KEY_BEAT, deck_ui.btn("#0f766e", [("BEAT ↻", 10, "#99f6e4"),
                                                       (BEATS[self.beat_idx][0], 14, "#fff")]))
        src = snap["source"][lane]
        self.set_key(KEY_SRC, deck_ui.btn("#5b21b6" if src else "#1f2937",
                                          [("SRC ↻", 10, "#ddd6fe"),
                                           (f"GEN {src}" if src else "off", 14, "#fff" if src else "#9ca3af")]))
        for k in (29, 30):
            self.set_key(k, deck_ui.btn("#0b0f1a", []))
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
        elif key in VOICE_ROW and key < len(GROUPS):
            if key == self.group:
                self.lane_sel[key] = (self.lane_sel[key] + 1) % len(GROUPS[key][1])  # cycle sound
            else:
                self.group = key
            self.render()
        elif key in STEP_ROW:
            lane = self.active_lane()
            if machine.snapshot()["source"][lane] is None:   # GEN-driven lanes are read-only
                machine.toggle_step(lane, key - STEP_ROW.start)
                self.render()
        elif key == KEY_PLAY:
            machine.toggle()
            self.render()
        elif key == KEY_CLEAR:
            machine.clear_voice(self.active_lane())
            self.render()
        elif key == KEY_CLEAR_ALL:
            machine.clear_all()
            self.render()
        elif key == KEY_BEAT:
            self.beat_idx = (self.beat_idx + 1) % len(BEATS)
            machine.load_beat(BEATS[self.beat_idx][1])
            self.render()
        elif key == KEY_SRC:
            machine.cycle_source(self.active_lane())
            self.render()
