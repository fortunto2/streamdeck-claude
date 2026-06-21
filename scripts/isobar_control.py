"""Generative (isobar) control surface — one voice of the GenEngine.

Top two rows are a live probability grid (tap a cell: off → hit →
ghost 50%, brightness = chance, green playhead = fired / grey = skipped).
Lower rows: steps / rotate / gate / fill / algo / scale / root± / euclid,
dice / mutate / clear, start-stop. Parametrised by `voice` (A/B/C) — each
is an independent generator on its own MIDI channel, phase-locked via Link.
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface

from isobar_engine import VOICES, note_name

GRID = range(0, 16)
KEY_STEPS_DN, KEY_STEPS_UP = 16, 17
KEY_ROTATE_L, KEY_ROTATE_R = 18, 19
KEY_GATE = 20
KEY_FILL = 21
KEY_ALGO, KEY_SCALE = 22, 23
KEY_PLAY = 24
KEY_ROOT_DN = 25   # ROOT −  (replaced the BPM display — tempo lives in the hub)
KEY_ROOT_UP = 26   # ROOT +
KEY_INFO = 27      # tap = cycle euclid pulses
KEY_DICE = 28
KEY_MUTATE = 29
KEY_CLEAR = 30
KEY_HOME = ControlSurface.HOME_KEY  # 31

FPS = 14.0
GATE_LABEL = {0.25: "short", 0.5: "med", 0.9: "long", 1.0: "HOLD"}


class IsobarControl(ControlSurface):

    def __init__(self, deck, on_home, voice: str = "A"):
        super().__init__(deck, on_home)
        self.eng = VOICES.get(voice, VOICES["A"])
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        self.running = True
        self.render()
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()

    def on_teardown(self) -> None:
        pass  # the voice keeps playing in the background

    # -- animation -----------------------------------------------------

    def _poll(self) -> None:
        frame = 1.0 / FPS
        while self.running:
            try:
                self.eng.current_tempo()      # keep Link warm/synced
                snap = self.eng.snapshot()
                if snap["running"]:
                    self._paint_grid(snap)
                self.set_key(KEY_PLAY, self._play_img(snap))
            except Exception:
                pass
            time.sleep(frame)

    def _play_img(self, snap: dict):
        if snap.get("pending"):
            blink = int(time.monotonic() * 2) % 2
            return deck_ui.btn("#a16207" if blink else "#3f2d06",
                               [("▶", 28, "#fde68a"), (f"GEN {self.eng.name} ·q", 11, "#fde68a")])
        if snap.get("armed"):
            return deck_ui.btn("#16a34a", [("■", 28, "#fff"), (f"GEN {self.eng.name}", 12, "#d1fae5")])
        return deck_ui.btn("#374151", [("▶", 28, "#fff"), (f"GEN {self.eng.name}", 12, "#d1d5db")])

    # -- rendering -----------------------------------------------------

    def _step_img(self, in_range: bool, prob: float, playhead: bool, fired: bool) -> Image.Image:
        if not in_range:
            return deck_ui.btn("#0b0f1a", [])
        full = prob >= 0.99
        ghost = 0.0 < prob < 0.99
        if playhead:
            if fired:
                bg, dot, r = "#052e16", "#4ade80", 22
            else:
                bg, dot, r = "#1f2937", "#475569", 15
        elif full:
            bg, dot, r = "#0c1f3a", "#3b82f6", 20
        elif ghost:
            bg, dot, r = "#101a33", "#1e40af", 13
        else:
            bg, dot, r = "#0f172a", "#1e293b", 11
        img = Image.new("RGB", deck_ui.SIZE, bg)
        d = ImageDraw.Draw(img)
        d.ellipse([48 - r, 48 - r, 48 + r, 48 + r], fill=dot)
        return img

    def _paint_grid(self, snap: dict) -> None:
        steps = snap["steps"]
        pattern = snap["pattern"]
        playhead = snap["step"] if snap["running"] else -1
        fired = snap.get("fired", False)
        for i in GRID:
            in_range = i < steps
            prob = pattern[i] if (in_range and i < len(pattern)) else 0.0
            ph = (i == playhead)
            self.set_key(i, self._step_img(in_range, prob, ph, ph and fired))

    def render(self) -> None:
        if not self.running:
            return
        snap = self.eng.snapshot()
        self._paint_grid(snap)

        self.set_key(KEY_STEPS_DN, deck_ui.btn("#1e293b", [("STEP", 12, "#cbd5e1"), ("−", 26, "#fff")]))
        self.set_key(KEY_STEPS_UP, deck_ui.btn("#1e293b", [("STEP", 12, "#cbd5e1"), ("+", 26, "#fff")]))
        self.set_key(KEY_ROTATE_L, deck_ui.btn("#1e293b", [("ROT", 12, "#cbd5e1"), ("◀", 24, "#fff")]))
        self.set_key(KEY_ROTATE_R, deck_ui.btn("#1e293b", [("ROT", 12, "#cbd5e1"), ("▶", 24, "#fff")]))
        glabel = GATE_LABEL.get(round(snap.get("gate", 0.5), 2), "med")
        gbg = "#0e7490" if glabel == "HOLD" else "#0f766e"
        self.set_key(KEY_GATE, deck_ui.btn(gbg, [("GATE", 11, "#99f6e4"), (glabel, 16, "#fff")]))
        fill_on = snap.get("fill", False)
        self.set_key(KEY_FILL, deck_ui.btn("#dc2626" if fill_on else "#7c2d12",
                                           [("FILL", 13, "#fff"), ("hold", 9, "#fecaca")]))
        self.set_key(KEY_ALGO, deck_ui.btn("#3730a3", [("ALGO", 11, "#c7d2fe"), (snap["mode"], 18, "#fff")]))
        self.set_key(KEY_SCALE, deck_ui.btn("#5b21b6", [("SCALE", 10, "#ddd6fe"), (snap["scale"], 18, "#fff")]))

        self.set_key(KEY_PLAY, self._play_img(snap))
        nn = note_name(snap["root"])
        self.set_key(KEY_ROOT_DN, deck_ui.btn("#1e293b", [("ROOT −", 11, "#94a3b8"), (nn, 22, "#a78bfa")]))
        self.set_key(KEY_ROOT_UP, deck_ui.btn("#1e293b", [("ROOT +", 11, "#94a3b8"), (nn, 22, "#a78bfa")]))
        self.set_key(KEY_INFO, deck_ui.btn("#0f172a", [("EUCLID ↻", 10, "#94a3b8"),
                                                       (f"{snap['pulses']}/{snap['steps']}", 22, "#38bdf8")]))
        self.set_key(KEY_DICE, deck_ui.btn("#b45309", [("DICE", 15, "#fff"), ("random", 9, "#fde68a")]))
        self.set_key(KEY_MUTATE, deck_ui.btn("#7c3aed", [("MUT", 15, "#fff"), ("evolve", 9, "#ddd6fe")]))
        self.set_key(KEY_CLEAR, deck_ui.btn("#7f1d1d", [("CLEAR", 14, "#fecaca"), ("grid", 9, "#f87171")]))
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if key == KEY_FILL:           # momentary — fires on press, off on release
            self.eng.set_fill(pressed)
            self.render()
            return
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
            return
        e = self.eng
        snap = e.snapshot()
        if key in GRID:
            if key < snap["steps"]:
                e.toggle_step(key)
        elif key == KEY_PLAY:
            e.toggle()
        elif key == KEY_STEPS_DN:
            e.set_steps(snap["steps"] - 1)
        elif key == KEY_STEPS_UP:
            e.set_steps(snap["steps"] + 1)
        elif key == KEY_ROTATE_L:
            e.rotate(-1)
        elif key == KEY_ROTATE_R:
            e.rotate(+1)
        elif key == KEY_GATE:
            e.cycle_gate()
        elif key == KEY_ALGO:
            e.cycle_mode()
        elif key == KEY_SCALE:
            e.cycle_scale()
        elif key == KEY_ROOT_DN:
            e.nudge_root(-1)
        elif key == KEY_ROOT_UP:
            e.nudge_root(+1)
        elif key == KEY_INFO:
            e.cycle_pulses()
        elif key == KEY_DICE:
            e.randomize()
        elif key == KEY_MUTATE:
            e.mutate()
        elif key == KEY_CLEAR:
            e.clear_pattern()
        else:
            return
        self.render()
