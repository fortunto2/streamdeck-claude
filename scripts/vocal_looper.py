"""Vocal Looper — multi-track live looping station + per-track FX bypass.

Each row is a vocal layer (a Live track that hosts a Looper): voice, backing,
khomus drone, live khomus… Per row, left→right:

  LOOP  CLR   FX1 FX2 FX3   MUTE ARM  VU

- LOOP / CLR go out as MIDI (Bus 1) to that looper's mapped multi-purpose /
  clear buttons (setting the Looper State over OSC doesn't reliably start a
  fresh record — the button always does; same trick as the Ableton page).
- FX1-3 bypass the track's non-looper devices (reverb / distortion / comp …)
  over OSC — no mapping, lit = on. Names read from Live.
- MUTE / ARM toggle the track over OSC. VU mirrors the track level. The LOOP
  LED mirrors Live's record/play/overdub state.

Map once in Live: each looper's transport button → note (90+row), its clear →
note (94+row), MIDI channel 16. See docs/vocal-looper.md.
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface
from src.ableton import AbletonClient

try:
    from src.midi_out import MidiOut
except Exception:  # pragma: no cover
    MidiOut = None

LAYERS = 4
VL_MIDI_CH = 15            # MIDI channel 16 → Bus 1 (looper buttons)
VL_TRANSPORT_BASE = 90    # transport note = base + row
VL_CLEAR_BASE = 94        # clear note     = base + row
FPS = 10.0
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
                self.midi = MidiOut(iac_prefer=["sdeck Bus 1", "IAC Driver Bus 1", "Bus 1"])
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
        while self.running:
            try:
                if self.client is not None:
                    self.client.keepalive()
                    self.client.request_meters(16)
                    self._paint_vu()
            except Exception:
                pass
            time.sleep(frame)

    def _vu_img(self, level: float) -> Image.Image:
        img = Image.new("RGB", deck_ui.SIZE, "#0b0f1a")
        d = ImageDraw.Draw(img)
        deck_ui.vu_bar(d, level, x0=20, x1=76)
        return img

    def _paint_vu(self) -> None:
        if self.client is None:
            return
        tracks = self.client.vocal_tracks(LAYERS)
        st = self.client.state
        for row, track in enumerate(tracks):
            k = row * 8 + 7
            if k == KEY_HOME:
                continue
            with st.lock:
                lvl = st.track_meter.get(track, 0.0)
            self.set_key(k, self._vu_img(lvl))

    def _abbrev(self, name: str) -> str:
        return (name[:7]) if name and name != "—" else "—"

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
        st = self.client.state
        if not tracks:
            self.set_key(0, deck_ui.btn("#1f2937", [("add a", 11, "#cbd5e1"),
                                                    ("Looper", 13, "#fff"),
                                                    ("to a track", 9, "#9ca3af")]))
            self.render_home_key()
            return
        for row, track in enumerate(tracks):
            base = row * 8
            with st.lock:
                name = st.track_names.get(track, f"trk {track}")
                muted = st.track_mute.get(track, False)
                armed = st.track_arm.get(track, False)
            lstate = self.client.looper_state_of(track)
            lbg, lglyph = LOOP_LED.get(lstate, LOOP_LED[0])
            self.set_key(base + 0, deck_ui.btn(lbg, [(lglyph, 12, "#fff"),
                                                     (name[:8], 10, "#e5e7eb")]))
            self.set_key(base + 1, deck_ui.btn("#450a0a", [("CLR", 14, "#fecaca")]))
            fx = self.client.fx_devices(track, 3)
            for j in range(3):
                k = base + 2 + j
                if j < len(fx):
                    dev = fx[j]
                    on = self.client.device_is_on(track, dev)
                    nm = self._abbrev(self.client.device_name(track, dev))
                    col = FX_COLORS[j]
                    self.set_key(k, deck_ui.btn(col if on else "#1f2937",
                                                [(nm, 12, "#fff" if on else "#9ca3af"),
                                                 ("on" if on else "byp", 9,
                                                  "#d1fae5" if on else "#6b7280")]))
                else:
                    self.set_key(k, deck_ui.btn("#0b0f1a", []))
            self.set_key(base + 5, deck_ui.btn("#7f1d1d" if muted else "#1f2937",
                                               [("MUTE" if muted else "mute", 12,
                                                 "#fecaca" if muted else "#cbd5e1")]))
            arm_k = base + 6
            self.set_key(arm_k, deck_ui.btn("#b91c1c" if armed else "#1f2937",
                                            [("ARM", 12, "#fff" if armed else "#cbd5e1")]))
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
            return
        if self.client is None:
            return
        row, col = divmod(key, 8)
        tracks = self.client.vocal_tracks(LAYERS)
        if row >= len(tracks):
            return
        track = tracks[row]
        if col == 0:
            self._looper_midi(VL_TRANSPORT_BASE + row)   # transport tap (MIDI)
        elif col == 1:
            self._looper_midi(VL_CLEAR_BASE + row)        # clear (MIDI)
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
            return
        self.render()
