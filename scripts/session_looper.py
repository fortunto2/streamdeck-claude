"""Session Looper — record live loops straight into Ableton Session clips.

The deck mirrors Live's Session grid: 8 tracks (columns) × 3 scene slots (rows).

- Tap an EMPTY slot → arms that track and fires it → Live records the input
  into the clip (a new loop). Tap again / it auto-stops → the clip loops.
- Tap an occupied clip → launch / relaunch it.
- Bottom row: SCENE 1-3 launch the whole row (all tracks' clips together — the
  "master layer"), STOP-ALL, HOME.

The recorded loops ARE native Session clips: Live shows their waveforms, you
see the layers in the slots, and you can drag them onto a Drum Rack pad / merge
/ export by hand. No custom plugin — Ableton owns the audio, the deck drives it.
"""

from __future__ import annotations

import threading
import time

import deck_ui
from control_surface import ControlSurface
from src.ableton import AbletonClient

COLS = 8        # tracks shown
ROWS = 3        # scene slots shown
FPS = 6.0       # blink animation rate
KEY_STOP_ALL = 27
KEY_HOME = ControlSurface.HOME_KEY  # 31


class SessionLooper(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.client: AbletonClient | None = None
        self._poll_thread: threading.Thread | None = None

    def start(self) -> None:
        self.running = True
        try:
            self.client = AbletonClient(on_change=lambda: self.request_repaint())
            self.client.start()
            self.client.refresh()
        except Exception:
            self.client = None
        self.render()
        self._poll_thread = threading.Thread(target=self._poll, daemon=True)
        self._poll_thread.start()

    def on_teardown(self) -> None:
        if self.client is not None:
            try:
                self.client.stop_listening()
            except Exception:
                pass

    def _poll(self) -> None:
        frame = 1.0 / FPS
        tick = 0
        while self.running:
            try:
                if self.client is not None:
                    if tick % 4 == 0:
                        self.client.keepalive()
                    if tick == 6 and not self.client._slots_subscribed:
                        self.client._subscribe_slots()   # deferred, once
                    self.render()                         # blink animation
            except Exception:
                pass
            tick += 1
            time.sleep(frame)

    # -- rendering -----------------------------------------------------

    @staticmethod
    def _hex(color) -> str:
        if not color:
            return "#3b4252"
        return "#%06x" % (int(color) & 0xFFFFFF)

    @staticmethod
    def _dim(hexc: str, f: float = 0.42) -> str:
        h = hexc.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))

    def _cell_img(self, t: int, s: int):
        st = self.client.state
        with st.lock:
            has = st.slot_has_clip.get((t, s), False)
            playing = st.slot_playing.get((t, s), False)
            trig = st.slot_triggered.get((t, s), False)
            rec = st.slot_recording.get((t, s), False)
            color = st.slot_color.get((t, s))
            tcolor = st.track_color.get(t)
            ntracks = st.num_tracks
        blink = int(time.monotonic() * 2) % 2
        if t >= ntracks:
            return deck_ui.btn("#0b0f1a", [])
        if rec:
            return deck_ui.btn("#dc2626" if blink else "#5c1212", [("●", 22, "#fff"), ("REC", 9, "#fecaca")])
        if not has:
            # empty, tap to record — faint track tint + a clearly visible +
            return deck_ui.btn(self._dim(self._hex(tcolor), 0.22), [("+", 26, "#64748b")])
        base = self._hex(color or tcolor)
        if trig and not playing:
            return deck_ui.btn(base if blink else "#1f2937", [("▷", 20, "#fff")])  # queued
        if playing:
            return deck_ui.btn(base, [("▶", 20, "#0b0f1a")], border="#ffffff")
        return deck_ui.btn(self._dim(base), [("■", 16, "#cbd5e1")])   # stored, stopped

    def render(self) -> None:
        if not self.running:
            return
        if self.client is None:
            for k in range(32):
                if k != KEY_HOME:
                    self.set_key(k, deck_ui.btn("#0b0f1a", []))
            self.set_key(0, deck_ui.btn("#7f1d1d", [("no OSC", 12, "#fecaca")]))
            self.render_home_key()
            return
        with self.client.state.lock:
            ntracks = self.client.state.num_tracks
        if ntracks == 0:
            for k in range(24):
                self.set_key(k, deck_ui.btn("#0b0f1a", []))
            self.set_key(0, deck_ui.btn("#1f2937", [("waiting", 11, "#cbd5e1"),
                                                    ("for Live", 12, "#fff"),
                                                    ("OSC…", 9, "#9ca3af")]))
        else:
            for s in range(ROWS):
                for t in range(COLS):
                    self.set_key(s * 8 + t, self._cell_img(t, s))
        # bottom row — scene launch + stop-all
        for i in range(3):
            self.set_key(24 + i, deck_ui.btn("#1e293b", [("SCENE", 9, "#94a3b8"),
                                                         (str(i + 1), 18, "#e5e7eb")]))
        self.set_key(KEY_STOP_ALL, deck_ui.btn("#7f1d1d", [("STOP", 13, "#fecaca"), ("all", 9, "#f87171")]))
        for k in (28, 29, 30):
            self.set_key(k, deck_ui.btn("#0b0f1a", []))
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
        if key < 24:
            s, t = divmod(key, 8)   # row = scene, col = track
            st = self.client.state
            with st.lock:
                has = st.slot_has_clip.get((t, s), False)
                ntracks = st.num_tracks
            if t >= ntracks:
                return
            if not has:
                self.client.set_arm(t, True)   # auto-arm so the empty slot records
            self.client.fire_clip_slot(t, s)
            return
        if key in (24, 25, 26):
            self.client.fire_scene(key - 24)
        elif key == KEY_STOP_ALL:
            self.client.stop_all_clips()
