"""Music Hub — section that groups every music instrument on the deck.

Shows the shared Link tempo, a master start/stop for the generator voices,
a preset/snapshot manager (save / cycle / load / delete), and launches /
switches between instruments (REAPER, Ableton, GEN A/B/C). Each instrument
keeps running in the background; the hub is the switchboard. Back from an
instrument returns here; HOME returns to the main dashboard.

State autosaves continuously (isobar_engine), so a crash restores the last
patterns on restart.
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
    from isobar_engine import (VOICES, start_all, stop_all, any_playing,
                               save_preset, list_presets, load_preset, delete_preset,
                               tap_tempo, mult_tempo, set_link_tempo)
except Exception:  # pragma: no cover
    isobar_control = None
    VOICES = {}
try:
    import drum_control
    from drum_engine import machine as drum_machine
except Exception:  # pragma: no cover
    drum_control = None
    drum_machine = None
try:
    import vdj_control
except Exception:  # pragma: no cover
    vdj_control = None
try:
    import vocal_looper
except Exception:  # pragma: no cover
    vocal_looper = None


def _gen_factory(voice):
    def make(deck, on_home):
        return isobar_control.IsobarControl(deck, on_home, voice=voice)
    return make


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
        items.append((13, "GEN D", "chord", "#14b8a6", _gen_factory("D"), "D"))
        items.append((14, "GEN E", "walk", "#f97316", _gen_factory("E"), "E"))
        items.append((15, "GEN F", "arp", "#eab308", _gen_factory("F"), "F"))
    if drum_control is not None:
        items.append((16, "DRUM", "808", "#dc2626", drum_control.DrumControl, "DRUM"))
    if vdj_control is not None:
        items.append((17, "VDJ", "decks", "#0ea5e9", vdj_control.VdjControl, None))
    if vocal_looper is not None:
        items.append((18, "LOOPER", "vocals", "#10b981", vocal_looper.VocalLooper, None))
    return items


def _engine_for(voice):
    if voice == "DRUM":
        return drum_machine
    if voice and voice in VOICES:
        return VOICES[voice]
    return None


KEY_TEMPO = 0
KEY_PLAY_ALL = 2
KEY_STOP_ALL = 3
KEY_TAP = 4
KEY_MUL2 = 5
KEY_DIV2 = 6
KEY_T100 = 7
KEY_SAVE = 24
KEY_PREV = 25
KEY_NAME = 26      # tap = load selected preset
KEY_NEXT = 27
KEY_DEL = 28
KEY_HOME = ControlSurface.HOME_KEY  # 31
FPS = 4.0


class MusicHub(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.items = _instruments()
        self.presets = list_presets() if isobar_control else []
        self.sel = max(0, len(self.presets) - 1)
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
                self._paint_transport()
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
            (f"● {peers} LINK" if synced else "solo", 9, "#4ade80" if synced else "#6b7280"),
        ]))

    def _all_engines(self):
        es = list(VOICES.values())
        if drum_machine is not None:
            es.append(drum_machine)
        return es

    def _paint_transport(self) -> None:
        engines = self._all_engines()
        armed = any(e.armed for e in engines)
        pending = any(e.pending for e in engines)
        blink = int(time.monotonic() * 2) % 2
        if pending and blink:
            pbg = "#a16207"   # queued — blink amber
        else:
            pbg = "#16a34a" if armed else "#14532d"
        self.set_key(KEY_PLAY_ALL, deck_ui.btn(pbg, [("▶", 26, "#fff"), ("ALL", 12, "#bbf7d0")]))
        self.set_key(KEY_STOP_ALL, deck_ui.btn("#374151", [("■", 24, "#fff"), ("ALL", 12, "#d1d5db")]))

    def _paint_instruments(self) -> None:
        blink = int(time.monotonic() * 2) % 2
        for key, label, sub, color, _factory, voice in self.items:
            v = _engine_for(voice)
            if v is not None and v.pending:
                bg = "#a16207" if blink else "#3f2d06"   # queued
                st, col = ("→ start" if v.pending == "start" else "→ stop"), "#fde68a"
            elif v is not None and v.armed:
                bg, st, col = color, "▶ live", "#d1fae5"
            else:
                bg, st, col = "#1f2937", sub, "#9ca3af"
            self.set_key(key, deck_ui.btn(bg, [(label, 14, "#ffffff"), (st, 11, col)]))

    def _paint_tempo_ctl(self) -> None:
        self.set_key(KEY_TAP, deck_ui.btn("#0e7490", [("TAP", 17, "#fff"), ("tempo", 9, "#a5f3fc")]))
        self.set_key(KEY_MUL2, deck_ui.btn("#1e293b", [("×2", 22, "#fff"), ("bpm", 9, "#94a3b8")]))
        self.set_key(KEY_DIV2, deck_ui.btn("#1e293b", [("÷2", 22, "#fff"), ("bpm", 9, "#94a3b8")]))
        self.set_key(KEY_T100, deck_ui.btn("#1e293b", [("100", 22, "#fff"), ("reset", 9, "#94a3b8")]))

    def _paint_presets(self) -> None:
        n = len(self.presets)
        name = self.presets[self.sel] if n else "—"
        self.set_key(KEY_SAVE, deck_ui.btn("#15803d", [("SAVE", 14, "#fff"), ("snapshot", 9, "#bbf7d0")]))
        self.set_key(KEY_PREV, deck_ui.btn("#1e293b" if n else "#0b0f1a", [("◀", 24, "#94a3b8" if n else "#334155")]))
        self.set_key(KEY_NAME, deck_ui.btn("#0f172a", [
            ("PRESET", 10, "#94a3b8"),
            (name, 14, "#a78bfa" if n else "#475569"),
            (f"{self.sel + 1}/{n} · load" if n else "save first", 9, "#64748b"),
        ]))
        self.set_key(KEY_NEXT, deck_ui.btn("#1e293b" if n else "#0b0f1a", [("▶", 24, "#94a3b8" if n else "#334155")]))
        self.set_key(KEY_DEL, deck_ui.btn("#7f1d1d" if n else "#0b0f1a",
                                          [("DEL", 14, "#fecaca" if n else "#475569"), ("preset", 9, "#f87171" if n else "#334155")]))

    def render(self) -> None:
        if not self.running:
            return
        for k in range(32):
            self.set_key(k, deck_ui.btn("#0b0f1a", []))
        self.set_key(1, deck_ui.btn("#111827", [("MUSIC", 13, "#a78bfa"), ("hub", 10, "#6b7280")]))
        self._paint_tempo()
        self._paint_tempo_ctl()
        self._paint_transport()
        self._paint_instruments()
        self._paint_presets()
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def _refresh(self) -> None:
        self.presets = list_presets()
        self.sel = min(self.sel, max(0, len(self.presets) - 1))

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
            return
        if key == KEY_PLAY_ALL:
            start_all()
            if drum_machine is not None:
                drum_machine.start()
            self.render()
            return
        if key == KEY_STOP_ALL:
            stop_all()
            if drum_machine is not None:
                drum_machine.stop()
            self.render()
            return
        if key == KEY_TAP:
            tap_tempo()
            self._paint_tempo()
            return
        if key == KEY_MUL2:
            mult_tempo(2.0)
            self._paint_tempo()
            return
        if key == KEY_DIV2:
            mult_tempo(0.5)
            self._paint_tempo()
            return
        if key == KEY_T100:
            set_link_tempo(100)
            self._paint_tempo()
            return
        if key == KEY_SAVE:
            name = save_preset()
            self._refresh()
            if name in self.presets:
                self.sel = self.presets.index(name)
            self.render()
            return
        if key == KEY_PREV and self.presets:
            self.sel = (self.sel - 1) % len(self.presets)
            self._paint_presets()
            return
        if key == KEY_NEXT and self.presets:
            self.sel = (self.sel + 1) % len(self.presets)
            self._paint_presets()
            return
        if key == KEY_NAME and self.presets:
            load_preset(self.presets[self.sel])
            self.render()
            return
        if key == KEY_DEL and self.presets:
            delete_preset(self.presets[self.sel])
            self._refresh()
            self.render()
            return
        for k, _label, _sub, _color, factory, _voice in self.items:
            if key == k and factory is not None and self.goto is not None:
                self.goto(factory, back=lambda: self.goto(MusicHub))
                return
