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

    def _link_step_img(self, prob: float, playhead: bool, beat: bool) -> Image.Image:
        if playhead:
            bg, dot, r = "#052e16", ("#4ade80" if prob > 0 else "#16341f"), (22 if prob > 0 else 14)
        elif prob >= 0.99:
            bg, dot, r = "#0b0f1a", "#a78bfa", 22
        elif prob > 0:
            bg, dot, r = "#0b0f1a", "#5b21b6", 13      # ghost — dim lilac
        else:
            bg, dot, r = ("#11161f" if beat else "#0b0f1a"), "#1e293b", 9
        img = Image.new("RGB", deck_ui.SIZE, bg)
        ImageDraw.Draw(img).ellipse([48 - r, 48 - r, 48 + r, 48 + r], fill=dot)
        return img

    def _paint_steps(self, snap: dict) -> None:
        lane = self.active_lane()
        src = snap["source"][lane]
        if src is not None and src in VOICES:
            # Mirror the GEN voice's live pattern — exactly as on its page
            # (only its step count, ghosts dim), looping playhead within it.
            gp = VOICES[src].snapshot()["pattern"]
            gn = len(gp)
            ph = (snap["step"] % gn) if (snap["running"] and snap["armed"] and gn) else -1
            for i in range(N_STEPS):
                if i < gn:
                    self.set_key(STEP_ROW.start + i, self._link_step_img(gp[i], i == ph, i % 4 == 0))
                else:
                    self.set_key(STEP_ROW.start + i, deck_ui.btn("#0b0f1a", []))
        else:
            step = snap["step"] if (snap["running"] and snap["armed"]) else -1
            color = GROUPS[self.group][2]
            row = snap["patterns"][lane]
            for i in range(N_STEPS):
                self.set_key(STEP_ROW.start + i, self._step_img(bool(row[i]), i == step, color, i % 4 == 0))

    def _voice_img(self, g: int, snap: dict) -> Image.Image:
        _label, lanes, color = GROUPS[g]
        cur = (g == self.group)
        sel_lane = lanes[self.lane_sel[g]]
        name = DRUMS[sel_lane][0]
        active_any = any(any(snap["patterns"][ln]) or snap["source"][ln] for ln in lanes)
        bg = color if cur else ("#1f2937" if active_any else "#111827")
        img = Image.new("RGB", deck_ui.SIZE, bg)
        d = ImageDraw.Draw(img)
        f = deck_ui.font(13)
        d.text((48, 32), deck_ui.fit(d, name, f, 90), font=f, fill="#fff" if cur else "#cbd5e1", anchor="mm")
        # Activity dots — one per lane in the group (incl. hidden ones):
        # filled if it has steps, gold if GEN-linked, white = the shown one.
        n = len(lanes)
        gap = 16
        x0 = 48 - (n - 1) * gap / 2.0
        for k, ln in enumerate(lanes):
            sourced = snap["source"][ln] is not None
            has = any(snap["patterns"][ln]) or sourced
            is_sel = (ln == sel_lane)
            cx, cy, r = int(x0 + k * gap), 70, (6 if is_sel else 5)
            if has:
                col = "#fde68a" if sourced else ("#ffffff" if is_sel else "#cbd5e1")
                d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
            else:
                d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#475569", width=1)
        if cur:
            d.rectangle([0, 0, 95, 95], outline="#f8fafc", width=3)
        return img

    def _paint_voices(self, snap: dict) -> None:
        for g in range(len(GROUPS)):
            self.set_key(g, self._voice_img(g, snap))

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
