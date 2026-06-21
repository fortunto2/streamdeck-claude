"""808/909 drum machine — control surface for DrumMachine.

Row 0: pick a drum voice (kick / snare / hat / …). Rows 1-2: that voice's
16 steps — tap to toggle, green playhead sweeps. Row 3: play/stop (bar-
quantised), clear voice, clear all, home.
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface

from drum_engine import machine, DRUMS, N_STEPS

VOICE_ROW = range(0, 8)
STEP_ROW = range(8, 24)     # 16 steps of the selected voice
KEY_PLAY = 24
KEY_CLEAR = 25
KEY_CLEAR_ALL = 26
KEY_HOME = ControlSurface.HOME_KEY  # 31
FPS = 14.0


class DrumControl(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.voice = 0
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        self.running = True
        self.render()
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()

    def on_teardown(self) -> None:
        pass  # machine keeps playing in the background

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
        color = DRUMS[self.voice][2]
        row = snap["patterns"][self.voice]
        for i in range(N_STEPS):
            self.set_key(STEP_ROW.start + i, self._step_img(bool(row[i]), i == step, color, i % 4 == 0))

    def _paint_voices(self, snap: dict) -> None:
        for i, (name, _note, color) in enumerate(DRUMS):
            sel = (i == self.voice)
            active = any(snap["patterns"][i])
            bg = color if sel else ("#1f2937" if active else "#111827")
            self.set_key(i, deck_ui.btn(bg, [(name, 13, "#fff" if sel else "#cbd5e1")],
                                        border="#f8fafc" if sel else None))

    def render(self) -> None:
        if not self.running:
            return
        snap = machine.snapshot()
        self._paint_voices(snap)
        self._paint_steps(snap)
        self.set_key(KEY_PLAY, self._play_img(snap))
        self.set_key(KEY_CLEAR, deck_ui.btn("#7f1d1d", [("CLEAR", 13, "#fecaca"), (DRUMS[self.voice][0], 9, "#f87171")]))
        self.set_key(KEY_CLEAR_ALL, deck_ui.btn("#450a0a", [("CLEAR", 13, "#fecaca"), ("all", 9, "#f87171")]))
        for k in (27, 28, 29, 30):
            self.set_key(k, deck_ui.btn("#0b0f1a", []))
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
        elif key in VOICE_ROW:
            self.voice = key
            self.render()
        elif key in STEP_ROW:
            machine.toggle_step(self.voice, key - STEP_ROW.start)
            self.render()
        elif key == KEY_PLAY:
            machine.toggle()
            self.render()
        elif key == KEY_CLEAR:
            machine.clear_voice(self.voice)
            self.render()
        elif key == KEY_CLEAR_ALL:
            machine.clear_all()
            self.render()
