"""Vocal Looper — multi-track live looping station + per-track FX bypass.

Up to 3 vocal layers (rows 0-2), each a Live track that hosts a Looper:
voice, backing, khomus drone… Per row, left→right:

  LOOP  CLR   FX1 FX2 FX3   MUTE ARM  VU

The bottom row (3) is a utility strip: ÷2 / ×2 per looper, then STOP-ALL.

- LOOP goes out as MIDI (Bus 1) to that looper's mapped multi-purpose button
  (OSC State-set doesn't reliably start a fresh record — the button does).
- CLR: tap = clear (MIDI), hold = undo (MIDI).
- FX1-3 bypass the track's non-looper devices over OSC — no mapping, lit = on.
- MUTE / ARM toggle the track over OSC. VU mirrors the track level. The LOOP
  LED mirrors Live's record/play/overdub state. STOP-ALL stops every looper.

Row 0 (top) is the main looper — it reuses the Ableton page's MIDI notes, so
its existing mapping just works; only the extra loopers need mapping. See
docs/vocal-looper.md for the per-row note table.
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface
from src.ableton import AbletonClient

try:
    from src.midi_out import MidiOut, iac_bus_prefer
except Exception:  # pragma: no cover
    MidiOut = None

LAYERS = 3
VL_MIDI_CH = 15            # MIDI channel 16 → Bus 1 (looper buttons)


# Per-row looper MIDI notes. Row 0 is the "main" looper — SAME transport/clear/
# ÷2/×2 as the Ableton page (113/110/111/112), so its existing mapping carries
# over; only the extra loopers (rows 1-2) need mapping. `undo` is a fresh note.
def _build_row_notes(n: int) -> list[dict]:
    rows = [{"transport": 113, "clear": 110, "half": 111, "double": 112, "undo": 109}]
    for r in range(1, n):
        b = 114 + (r - 1) * 5   # row1:114-118  row2:119-123
        rows.append({"transport": b, "clear": b + 1, "half": b + 2,
                     "double": b + 3, "undo": b + 4})
    return rows


ROW_NOTES = _build_row_notes(LAYERS)
BOTTOM = range(24, 32)     # utility row
KEY_STOP_ALL = 30
FPS = 8.0
LONG_PRESS = 0.35
KEY_HOME = ControlSurface.HOME_KEY  # 31

# Looper State → (colour, glyph). 0 Stop, 1 Record, 2 Play, 3 Overdub.
LOOP_LED = {0: ("#374151", "▶ LOOP"), 1: ("#dc2626", "● REC"),
            2: ("#16a34a", "▶ PLAY"), 3: ("#d97706", "◉ DUB")}
FX_COLORS = ["#0e7490", "#7c3aed", "#b45309"]   # FX1/2/3 accent colours


class VocalLooper(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.client: AbletonClient | None = None
        self.midi = None
        self._poll_thread: threading.Thread | None = None
        self._press_t: dict[int, float] = {}

    def start(self) -> None:
        self.running = True
        try:
            self.client = AbletonClient(on_change=lambda: self.request_repaint())
            self.client.start()
            self.client.refresh()
        except Exception:
            self.client = None
        if MidiOut is not None:
            try:
                self.midi = MidiOut(iac_prefer=iac_bus_prefer(1))
            except Exception:
                self.midi = None
        self.render()
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()

    def on_teardown(self) -> None:
        if self.client is not None:
            try:
                self.client.stop_listening()
            except Exception:
                pass
        if self.midi is not None:
            try:
                self.midi.all_notes_off(VL_MIDI_CH)
                self.midi.close()
            except Exception:
                pass

    def _looper_midi(self, note: int) -> None:
        if self.midi is None:
            return
        try:
            self.midi.note_on(note, 127, VL_MIDI_CH)
            self.midi.note_off(note, VL_MIDI_CH)
        except Exception:
            pass

    # -- rendering -----------------------------------------------------

    def _poll(self) -> None:
        frame = 1.0 / FPS
        keepalive_every = max(1, int(FPS * 0.8))
        tick = 0
        while self.running:
            try:
                if self.client is not None:
                    if tick % keepalive_every == 0:
                        self.client.keepalive()
                    tracks = self.client.vocal_tracks(LAYERS)
                    if tracks:
                        self.client.request_meters(max(tracks) + 1)
                        self._paint_vu(tracks)
            except Exception:
                pass
            tick += 1
            time.sleep(frame)

    def _abbrev(self, name: str) -> str:
        return (name[:7]) if name and name != "—" else "—"

    def _vu_img(self, level: float) -> Image.Image:
        img = Image.new("RGB", deck_ui.SIZE, "#0b0f1a")
        deck_ui.vu_bar(ImageDraw.Draw(img), level, x0=20, x1=76)
        return img

    def _cell_img(self, row: int, col: int, track: int) -> Image.Image:
        c = self.client
        st = c.state
        if col == 0:                              # LOOP transport (LED = state)
            with st.lock:
                name = st.track_names.get(track, f"trk {track}")
            bg, glyph = LOOP_LED.get(c.looper_state_of(track), LOOP_LED[0])
            return deck_ui.btn(bg, [(glyph, 12, "#fff"), (name[:8], 10, "#e5e7eb")])
        if col == 1:                              # CLR (tap) / UNDO (hold)
            return deck_ui.btn("#450a0a", [("CLR", 13, "#fecaca"),
                                           ("hold=undo", 8, "#9f5050")])
        if col in (2, 3, 4):                      # FX bypass
            fx = c.fx_devices(track, 3)
            j = col - 2
            if j >= len(fx):
                return deck_ui.btn("#0b0f1a", [])
            dev = fx[j]
            on = c.device_is_on(track, dev)
            nm = self._abbrev(c.device_name(track, dev))
            return deck_ui.btn(FX_COLORS[j] if on else "#1f2937",
                               [(nm, 12, "#fff" if on else "#9ca3af"),
                                ("on" if on else "byp", 9, "#d1fae5" if on else "#6b7280")])
        if col == 5:                              # MUTE
            with st.lock:
                muted = st.track_mute.get(track, False)
            return deck_ui.btn("#7f1d1d" if muted else "#1f2937",
                               [("MUTE" if muted else "mute", 12,
                                 "#fecaca" if muted else "#cbd5e1")])
        if col == 6:                              # ARM
            with st.lock:
                armed = st.track_arm.get(track, False)
            return deck_ui.btn("#b91c1c" if armed else "#1f2937",
                               [("ARM", 12, "#fff" if armed else "#cbd5e1")])
        with st.lock:                             # col 7 — VU
            lvl = st.track_meter.get(track, 0.0)
        return self._vu_img(lvl)

    def _paint_vu(self, tracks: list[int]) -> None:
        for row, track in enumerate(tracks):
            self.set_key(row * 8 + 7, self._cell_img(row, 7, track))

    def _paint_bottom(self, n: int) -> None:
        for i in range(LAYERS):
            half_k, dbl_k = 24 + i * 2, 25 + i * 2
            if i < n:
                self.set_key(half_k, deck_ui.btn("#1e3a5f", [(f"L{i + 1}", 10, "#93c5fd"),
                                                             ("÷2", 20, "#dbeafe")]))
                self.set_key(dbl_k, deck_ui.btn("#1e3a5f", [(f"L{i + 1}", 10, "#93c5fd"),
                                                            ("×2", 20, "#dbeafe")]))
            else:
                self.set_key(half_k, deck_ui.btn("#0b0f1a", []))
                self.set_key(dbl_k, deck_ui.btn("#0b0f1a", []))
        self.set_key(KEY_STOP_ALL, deck_ui.btn("#7f1d1d", [("STOP", 13, "#fecaca"),
                                                           ("all loops", 9, "#f87171")]))

    def render(self) -> None:
        if not self.running:
            return
        for k in range(32):
            if k != KEY_HOME:
                self.set_key(k, deck_ui.btn("#0b0f1a", []))
        if self.client is None:
            self.set_key(0, deck_ui.btn("#7f1d1d", [("no OSC", 12, "#fecaca")]))
            self.render_home_key()
            return
        tracks = self.client.vocal_tracks(LAYERS)
        if not tracks:
            self.set_key(0, deck_ui.btn("#1f2937", [("add a", 11, "#cbd5e1"),
                                                    ("Looper", 13, "#fff"),
                                                    ("to a track", 9, "#9ca3af")]))
            self._paint_bottom(0)
            self.render_home_key()
            return
        for row, track in enumerate(tracks):
            for col in range(8):
                self.set_key(row * 8 + col, self._cell_img(row, col, track))
        self._paint_bottom(len(tracks))
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if key == KEY_HOME:
            if pressed:
                self.on_home()
            return
        if self.client is None:
            return
        tracks = self.client.vocal_tracks(LAYERS)
        if key in BOTTOM:
            self._bottom_key(key, pressed, len(tracks))
            return
        row, col = divmod(key, 8)
        if row >= len(tracks):
            return
        track = tracks[row]
        notes = ROW_NOTES[row]
        if col == 1:   # CLR — tap = clear, hold = undo (press/release timing)
            if pressed:
                self._press_t[key] = time.monotonic()
                return
            held = (time.monotonic() - self._press_t.pop(key, time.monotonic())) >= LONG_PRESS
            self._looper_midi(notes["undo"] if held else notes["clear"])
            return
        if not pressed:
            return
        if col == 0:
            self.client.solo_record(track)          # only one looper records at a time
            self._looper_midi(notes["transport"])   # transport tap (MIDI)
        elif col in (2, 3, 4):
            fx = self.client.fx_devices(track, 3)
            j = col - 2
            if j < len(fx):
                self.client.toggle_device(track, fx[j])
        elif col == 5:
            self.client.toggle_mute(track)
        elif col == 6:
            self.client.toggle_arm(track)
        else:
            return   # col 7 = VU, no action
        # instant single-key feedback (optimistic state) — no full-deck blink
        self.set_key(key, self._cell_img(row, col, track))

    def _bottom_key(self, key: int, pressed: bool, n: int) -> None:
        if not pressed:
            return
        if key == KEY_STOP_ALL:
            self.client.stop_all_loopers()
            return
        i = (key - 24) // 2                 # looper index
        if i >= n:
            return
        is_double = (key - 24) % 2 == 1
        self._looper_midi(ROW_NOTES[i]["double" if is_double else "half"])
