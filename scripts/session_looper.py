"""Session Looper — record live loops straight into Ableton Session clips.

Grid: **7 tracks (columns 0-6) × 4 scene slots (rows)** for plenty of record
space, with the **control column on the right** (col 7): QUANT, AUTO, STOP, HOME.

- Tap an EMPTY slot → arms that track and fires it → Live records the input into
  the clip (a new loop). Tap again / it auto-stops → the clip loops.
- Tap an occupied clip → launch / relaunch it.
- Long-press a clip → DELETE it (clear the slot).
- QUANT ↻ — record/launch quantization. AUTO — auto-fit each finished recording
  to the grid (trim → beat loop → warp + quantize) and play it. STOP — stop all.

Recorded loops are native Session clips — Live shows the waveform + layer stack;
with AUTO on, recordings lock to the beat grid automatically.
"""

from __future__ import annotations

import threading
import time

import deck_ui
from control_surface import ControlSurface
from src.ableton import AbletonClient

TRACKS = 7      # columns 0-6 (col 7 = control column)
SCENES = 4      # rows 0-3
FPS = 5.0
LONG_PRESS = 0.45
CTRL_COL = 7
KEY_QUANT = 7        # col 7, row 0
KEY_AUTO = 15        # col 7, row 1
KEY_STOP_ALL = 23    # col 7, row 2
KEY_HOME = ControlSurface.HOME_KEY  # 31 (col 7, row 3)

QUANT_CYCLE = [(4, "1 Bar"), (3, "2 Bar"), (7, "1/4"), (0, "Off")]
# Live Quantization enum for warping clip transients on auto-fit (11 = 1/16).
QUANTIZE_GRID = 11


class SessionLooper(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.client: AbletonClient | None = None
        self._poll_thread: threading.Thread | None = None
        self._press_t: dict[int, float] = {}
        self._auto = True
        self._was_rec: dict[tuple[int, int], bool] = {}

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
                        self.client._subscribe_slots()
                    self._auto_trim_finished()
                    self.render()
            except Exception:
                pass
            tick += 1
            time.sleep(frame)

    def _auto_trim_finished(self) -> None:
        st = self.client.state
        with st.lock:
            rec = dict(st.slot_recording)
            has = dict(st.slot_has_clip)
        if self._auto:
            for key, was in self._was_rec.items():
                if was and not rec.get(key, False) and has.get(key):
                    threading.Thread(target=self._trim, args=(key[0], key[1], True),
                                     daemon=True).start()
        self._was_rec = rec

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
        if t >= ntracks:
            return deck_ui.btn("#0b0f1a", [])
        if rec:
            return deck_ui.btn("#dc2626", [("●", 22, "#fff"), ("REC", 9, "#fecaca")])  # solid
        if not has:
            return deck_ui.btn(self._dim(self._hex(tcolor), 0.22), [("+", 26, "#64748b")])
        base = self._hex(color or tcolor)
        if trig and not playing:
            blink = int(time.monotonic() * 2) % 2
            return deck_ui.btn(base if blink else "#1f2937", [("▷", 20, "#fff")])
        if playing:
            return deck_ui.btn(base, [("▶", 20, "#0b0f1a")], border="#ffffff")
        return deck_ui.btn(self._dim(base), [("■", 16, "#cbd5e1")])

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
        with self.client.state.lock:
            ntracks = self.client.state.num_tracks
            q = self.client.state.global_quant
        if ntracks == 0:
            self.set_key(0, deck_ui.btn("#1f2937", [("waiting", 11, "#cbd5e1"),
                                                    ("for Live", 12, "#fff")]))
        else:
            for s in range(SCENES):
                for t in range(TRACKS):
                    self.set_key(s * 8 + t, self._cell_img(t, s))
        # control column (right)
        qlabel = next((lbl for v, lbl in QUANT_CYCLE if v == q), str(q))
        qon = q != 0
        self.set_key(KEY_QUANT, deck_ui.btn("#0e7490" if qon else "#1f2937",
                                            [("QUANT", 10, "#a5f3fc"),
                                             (qlabel, 13, "#fff" if qon else "#9ca3af")]))
        self.set_key(KEY_AUTO, deck_ui.btn("#15803d" if self._auto else "#1f2937",
                                           [("AUTO", 12, "#fff" if self._auto else "#cbd5e1"),
                                            ("fit" if self._auto else "off", 9,
                                             "#bbf7d0" if self._auto else "#9ca3af")]))
        self.set_key(KEY_STOP_ALL, deck_ui.btn("#7f1d1d", [("STOP", 12, "#fecaca"), ("all", 9, "#f87171")]))
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if key % 8 < CTRL_COL:
            self._grid_gesture(key, pressed)
            return
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
        elif self.client is None:
            return
        elif key == KEY_STOP_ALL:
            self.client.stop_all_clips()
        elif key == KEY_AUTO:
            self._auto = not self._auto
            self.render()
        elif key == KEY_QUANT:
            with self.client.state.lock:
                cur = self.client.state.global_quant
            vals = [v for v, _ in QUANT_CYCLE]
            i = vals.index(cur) if cur in vals else 0
            self.client.set_global_quantize(vals[(i + 1) % len(vals)])
            self.render()

    def _grid_gesture(self, key: int, pressed: bool) -> None:
        if self.client is None:
            return
        s, t = key // 8, key % 8
        st = self.client.state
        with st.lock:
            has = st.slot_has_clip.get((t, s), False)
            ntracks = st.num_tracks
        if t >= ntracks:
            return
        if pressed:
            self._press_t[key] = time.monotonic()
            return
        if key not in self._press_t:
            return   # release without a press we saw (e.g. the page-launch key) — ignore
        dt = time.monotonic() - self._press_t.pop(key)
        if has and dt >= LONG_PRESS:
            self.set_key(key, deck_ui.btn("#7f1d1d", [("DEL", 14, "#fecaca")]))
            self.client.delete_clip_slot(t, s)   # long press = delete the clip
            return
        if not has:
            self.client.set_arm(t, True)
        self.client.fire_clip_slot(t, s)

    def _trim(self, t: int, s: int, play: bool = False) -> None:
        """Auto-fit: find the clip's content, set a beat-aligned loop, warp +
        quantize to the grid, and (optionally) re-launch it to play."""
        import math
        c = self.client
        try:
            import audio_trim
        except Exception as e:
            print(f"[session] trim: audio_trim import failed: {e}")
            return
        path = c.state.slot_file_path.get((t, s))
        if not path:
            c._send("/live/clip/get/file_path", t, s)
            for _ in range(40):
                time.sleep(0.05)
                path = c.state.slot_file_path.get((t, s))
                if path:
                    break
        if not path:
            return
        r = audio_trim.content_bounds(path)
        if not r:
            return
        start_sec, end_sec, dur, sr = r
        bpm = max(c.state.tempo, 1.0)
        spb = 60.0 / bpm
        ls = max(0.0, float(math.floor(start_sec / spb)))
        le = float(math.ceil(end_sec / spb))
        if le <= ls:
            le = ls + 1.0
        print(f"[session] fit t={t} s={s}: content {start_sec:.2f}-{end_sec:.2f}s "
              f"@ {bpm:.1f}bpm -> loop {ls:.0f}-{le:.0f} beats")
        c.set_clip_loop(t, s, ls, le)
        c.set_clip_warp(t, s, True)
        time.sleep(0.1)
        c.clip_quantize(t, s, QUANTIZE_GRID, 1.0)
        if play:
            time.sleep(0.1)
            c.fire_clip_slot(t, s)
