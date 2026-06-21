"""Generative (isobar) control surface — pulse pad for the GenEngine.

v1: one Euclidean voice. The top two rows are a live 16-step grid (hits +
moving playhead); the lower rows tweak steps / pulses / root / tempo and
start-stop the voice. The engine itself runs in the background
(``isobar_engine.engine``) and keeps playing when you leave this page.
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface

from isobar_engine import engine, note_name

GRID = range(0, 16)          # 16-step euclidean view + playhead
KEY_STEPS_DN, KEY_STEPS_UP = 16, 17
KEY_PULSES_DN, KEY_PULSES_UP = 18, 19
KEY_ROOT_DN, KEY_ROOT_UP = 20, 21
KEY_ALGO, KEY_SCALE = 22, 23   # cycle pitch algorithm / scale
KEY_PLAY = 24
KEY_TEMPO = 25
KEY_ROOT = 26
KEY_INFO = 27
KEY_HOME = ControlSurface.HOME_KEY  # 31

FPS = 14.0


class IsobarControl(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        self.running = True
        self.render()
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()

    def on_teardown(self) -> None:
        # Note: do NOT stop the engine — it persists across page flips.
        pass

    # -- animation: move the playhead while the voice plays --------------

    def _poll(self) -> None:
        frame = 1.0 / FPS
        while self.running:
            try:
                engine.current_tempo()           # refresh tempo/peers from Link
                snap = engine.snapshot()
                if snap["running"]:
                    self._paint_grid(snap)
                self.set_key(KEY_TEMPO, self._tempo_img(snap))
            except Exception:
                pass
            time.sleep(frame)

    def _tempo_img(self, snap: dict):
        synced = snap["peers"] > 0
        return deck_ui.btn("#0f172a", [
            ("BPM", 11, "#94a3b8"),
            (f"{snap['tempo']:.0f}", 24, "#fcd34d"),
            ("● LINK" if synced else "solo", 9, "#4ade80" if synced else "#6b7280"),
        ])

    # -- rendering ------------------------------------------------------

    def _step_img(self, in_range: bool, hit: bool, playhead: bool) -> Image.Image:
        if not in_range:
            return deck_ui.btn("#0b0f1a", [])
        if hit and playhead:
            bg, dot, r = "#052e16", "#4ade80", 22
        elif hit:
            bg, dot, r = "#0c1f3a", "#3b82f6", 20
        elif playhead:
            bg, dot, r = "#1f2937", "#cbd5e1", 20
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
        for i in GRID:
            in_range = i < steps
            hit = bool(pattern[i]) if (in_range and i < len(pattern)) else False
            self.set_key(i, self._step_img(in_range, hit, i == playhead))

    def render(self) -> None:
        if not self.running:
            return
        snap = engine.snapshot()
        self._paint_grid(snap)

        self.set_key(KEY_STEPS_DN, deck_ui.btn("#1e293b", [("STEP", 12, "#cbd5e1"), ("−", 26, "#fff")]))
        self.set_key(KEY_STEPS_UP, deck_ui.btn("#1e293b", [("STEP", 12, "#cbd5e1"), ("+", 26, "#fff")]))
        self.set_key(KEY_PULSES_DN, deck_ui.btn("#1e293b", [("PULSE", 11, "#cbd5e1"), ("−", 26, "#fff")]))
        self.set_key(KEY_PULSES_UP, deck_ui.btn("#1e293b", [("PULSE", 11, "#cbd5e1"), ("+", 26, "#fff")]))
        self.set_key(KEY_ROOT_DN, deck_ui.btn("#1e293b", [("ROOT", 12, "#cbd5e1"), ("−", 26, "#fff")]))
        self.set_key(KEY_ROOT_UP, deck_ui.btn("#1e293b", [("ROOT", 12, "#cbd5e1"), ("+", 26, "#fff")]))
        self.set_key(KEY_ALGO, deck_ui.btn("#3730a3", [("ALGO", 11, "#c7d2fe"),
                                                       (snap["mode"], 20, "#fff")]))
        self.set_key(KEY_SCALE, deck_ui.btn("#5b21b6", [("SCALE", 10, "#ddd6fe"),
                                                        (snap["scale"], 18, "#fff")]))

        playing = snap["running"]
        self.set_key(KEY_PLAY, deck_ui.btn("#16a34a" if playing else "#374151",
                                           [("▶" if not playing else "■", 30, "#fff"),
                                            ("PLAY" if not playing else "STOP", 12, "#d1fae5" if playing else "#d1d5db")]))
        self.set_key(KEY_TEMPO, self._tempo_img(snap))
        self.set_key(KEY_ROOT, deck_ui.btn("#0f172a", [("ROOT", 11, "#94a3b8"),
                                                       (note_name(snap["root"]), 22, "#a78bfa")]))
        self.set_key(KEY_INFO, deck_ui.btn("#0f172a", [("EUCLID", 10, "#94a3b8"),
                                                       (f"{snap['pulses']}/{snap['steps']}", 22, "#38bdf8")]))
        for k in (28, 29, 30):
            self.set_key(k, deck_ui.btn("#0b0f1a", []))
        self.render_home_key()

    # -- input ----------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
            return
        snap = engine.snapshot()
        if key == KEY_PLAY:
            engine.toggle()
        elif key == KEY_STEPS_DN:
            engine.set_steps(snap["steps"] - 1)
        elif key == KEY_STEPS_UP:
            engine.set_steps(snap["steps"] + 1)
        elif key == KEY_PULSES_DN:
            engine.set_pulses(snap["pulses"] - 1)
        elif key == KEY_PULSES_UP:
            engine.set_pulses(snap["pulses"] + 1)
        elif key == KEY_ROOT_DN:
            engine.nudge_root(-1)
        elif key == KEY_ROOT_UP:
            engine.nudge_root(+1)
        elif key == KEY_ALGO:
            engine.cycle_mode()
        elif key == KEY_SCALE:
            engine.cycle_scale()
        else:
            return
        self.render()
