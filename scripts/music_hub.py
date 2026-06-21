"""Music Hub — a section that groups every music instrument on the deck.

Shows the shared tempo (Ableton Link) and which generator voices are
live, and launches / switches between instruments: REAPER, Ableton, and
the three generative voices (GEN A/B/C). Each instrument keeps running in
the background; the hub is just the switchboard. Back from an instrument
returns here; HOME returns to the main dashboard.

Adding more instruments (the old MIDI sequencer, a drum machine, …) is a
matter of dropping another entry in INSTRUMENTS.
"""

from __future__ import annotations

import threading
import time

import deck_ui
from control_surface import ControlSurface

try:
    import reaper_control
except Exception:  # pragma: no cover
    reaper_control = None
try:
    import ableton_control
except Exception:  # pragma: no cover
    ableton_control = None
try:
    import isobar_control
    from isobar_engine import VOICES
except Exception:  # pragma: no cover
    isobar_control = None
    VOICES = {}


def _gen_factory(voice):
    def make(deck, on_home):
        return isobar_control.IsobarControl(deck, on_home, voice=voice)
    return make


# (key, label, sublabel, colour, factory-or-None, voice-key-or-None)
def _instruments():
    items = []
    if reaper_control is not None:
        items.append((8, "REAPER", "mix", "#0f766e", reaper_control.ReaperControl, None))
    if ableton_control is not None:
        items.append((9, "ABLETON", "live", "#f59e0b", ableton_control.AbletonControl, None))
    if isobar_control is not None:
        items.append((10, "GEN A", "melody", "#3b82f6", _gen_factory("A"), "A"))
        items.append((11, "GEN B", "bass", "#8b5cf6", _gen_factory("B"), "B"))
        items.append((12, "GEN C", "high", "#ec4899", _gen_factory("C"), "C"))
    return items


KEY_TEMPO = 0
KEY_HOME = ControlSurface.HOME_KEY  # 31
FPS = 4.0


class MusicHub(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.items = _instruments()
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        self.running = True
        self.render()
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()

    def _poll(self) -> None:
        frame = 1.0 / FPS
        while self.running:
            try:
                self._paint_tempo()
                self._paint_instruments()
            except Exception:
                pass
            time.sleep(frame)

    # -- rendering -----------------------------------------------------

    def _paint_tempo(self) -> None:
        tempo, peers = 120.0, 0
        if VOICES:
            try:
                tempo, peers = next(iter(VOICES.values())).current_tempo()
            except Exception:
                pass
        synced = peers > 0
        self.set_key(KEY_TEMPO, deck_ui.btn("#0f172a", [
            ("TEMPO", 11, "#94a3b8"),
            (f"{tempo:.0f}", 26, "#fcd34d"),
            ("● LINK" if synced else "solo", 9, "#4ade80" if synced else "#6b7280"),
        ]))

    def _paint_instruments(self) -> None:
        for key, label, sub, color, _factory, voice in self.items:
            playing = bool(voice) and voice in VOICES and VOICES[voice].running
            bg = color if playing else "#1f2937"
            sub2 = ("▶ live" if playing else sub)
            self.set_key(key, deck_ui.btn(bg, [
                (label, 14, "#ffffff"),
                (sub2, 11, "#d1fae5" if playing else "#9ca3af"),
            ]))

    def render(self) -> None:
        if not self.running:
            return
        # Blank the whole deck, then paint header + instruments.
        for k in range(32):
            self.set_key(k, deck_ui.btn("#0b0f1a", []))
        self.set_key(1, deck_ui.btn("#111827", [("MUSIC", 13, "#a78bfa"), ("hub", 10, "#6b7280")]))
        self._paint_tempo()
        self._paint_instruments()
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
            return
        for k, _label, _sub, _color, factory, _voice in self.items:
            if key == k and factory is not None and self.goto is not None:
                self.goto(factory, back=lambda: self.goto(MusicHub))
                return
