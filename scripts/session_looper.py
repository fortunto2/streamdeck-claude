"""Session Looper — record live loops straight into Ableton Session clips.

Grid: **6 tracks (columns 0-5) × 4 scene slots (rows)**, with two control
columns on the right: **col 6 = loop size** (×2 / ÷2 / →bar / selected-clip),
**col 7 = transport** (QUANT, AUTO, STOP, HOME).

- Tap an EMPTY slot → arms that track and fires it → Live records the input into
  the clip (a new loop). Tap again / it auto-stops → the clip loops.
- Tap a stopped clip → launch it (and select it). Tap a PLAYING clip → mute /
  unmute it (stays in sync — drop a layer out/in on the grid).
- Long-press a clip → DELETE it (clear the slot).
- The last clip you tap / record is **selected** (white dot). The size column
  then resizes its loop: **×2** doubles, **÷2** halves, **→bar** snaps to whole
  bars. One loop just circles; size it on the fly like a classic looper.
- QUANT ↻ — record/launch quantization. AUTO — auto-fit each finished recording
  to the grid (trim → beat loop → warp + quantize) and play it. STOP — stop all.

Recorded loops are native Session clips — Live shows the waveform + layer stack;
with AUTO on, recordings lock to the beat grid automatically.
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface
from src.ableton import AbletonClient

TRACKS = 6      # clip columns 0-5 (col 6 = loop-size column, col 7 = transport)
SCENES = 4      # rows 0-3
FPS = 5.0
LONG_PRESS = 0.45
MIN_LOOP_BEATS = 0.5
# col 6 — loop size of the SELECTED clip (the last one you tapped / recorded)
KEY_X2 = 6           # col 6, row 0 — double the loop
KEY_DIV2 = 14        # col 6, row 1 — halve the loop
KEY_LEN = 22         # col 6, row 2 — length readout; tap = snap to nearest bar
KEY_SEL = 30         # col 6, row 3 — which clip is selected (info)
# col 7 — transport
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
        self._sel: tuple[int, int] | None = None   # selected clip for ÷2 / ×2
        self._was_rec: dict[tuple[int, int], bool] = {}
        # Tiny waveform per clip: (t,s) -> (file_path, [0..1 envelope]).
        self._wave: dict[tuple[int, int], tuple] = {}
        self._wave_lock = threading.Lock()
        self._wave_pending: set[tuple[int, int]] = set()

    def start(self) -> None:
        self.running = True
        try:
            # No on_change repaint: the poll thread is the SINGLE renderer, so
            # two threads never blit concurrently (that raced the set_key cache
            # and flickered during recording). State updates show within 1/FPS.
            self.client = AbletonClient(on_change=lambda: None)
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
        for key, was in self._was_rec.items():
            if was and not rec.get(key, False) and has.get(key):
                self._sel = key          # just-recorded clip is the ÷2/×2 target
                if self._auto:
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

    def _wave_get(self, t: int, s: int):
        """Cached envelope for a clip; kicks off an async read if stale."""
        path = self.client.state.slot_file_path.get((t, s))
        if not path:
            self.client._send("/live/clip/get/file_path", t, s)
            return None
        with self._wave_lock:
            cached = self._wave.get((t, s))
        if cached and cached[0] == path:
            return cached[1]
        if (t, s) not in self._wave_pending:
            self._wave_pending.add((t, s))
            threading.Thread(target=self._compute_wave, args=(t, s, path), daemon=True).start()
        return cached[1] if cached else None

    def _compute_wave(self, t: int, s: int, path: str) -> None:
        try:
            import audio_trim
            env = audio_trim.waveform(path)
        except Exception:
            env = None
        with self._wave_lock:
            if env:
                self._wave[(t, s)] = (path, env)
            self._wave_pending.discard((t, s))

    def _wave_img(self, env, color: str, playing: bool, queued: bool,
                  muted: bool = False, selected: bool = False):
        # Sounding (playing & not muted) = bright wave, green-tinted bg, thick
        # green border (Ableton plays clips green). Playing-but-MUTED = dim wave
        # + slate border (in sync, silenced). Stopped = strongly dimmed, no
        # border. Queued = amber blink.
        live = playing and not muted
        bg = "#0a1f14" if live else "#0b0f1a"
        img = Image.new("RGB", deck_ui.SIZE, bg)
        d = ImageDraw.Draw(img)
        if env:
            wc = color if live else self._dim(color, 0.30)
            n = len(env)
            bw = 96.0 / n
            for i, v in enumerate(env):
                x = i * bw
                h = int(v * 38) + 1
                d.rectangle([x, 48 - h, x + bw + 0.6, 48 + h], fill=wc)
        if live:
            d.rectangle([0, 0, 95, 95], outline="#22c55e", width=6)        # green = sounding
        elif playing and muted:
            d.rectangle([0, 0, 95, 95], outline="#64748b", width=4)        # slate = playing, muted
            d.text((38, 40), "M", fill="#94a3b8")
        elif queued and int(time.monotonic() * 2) % 2:
            d.rectangle([0, 0, 95, 95], outline="#fbbf24", width=5)        # amber blink = queued
        if selected:
            d.ellipse([4, 4, 16, 16], fill="#fff")     # selected = ÷2/×2 target
        return img

    def _cell_img(self, t: int, s: int):
        st = self.client.state
        with st.lock:
            has = st.slot_has_clip.get((t, s), False)
            playing = st.slot_playing.get((t, s), False)
            trig = st.slot_triggered.get((t, s), False)
            rec = st.slot_recording.get((t, s), False)
            muted = st.slot_muted.get((t, s), False)
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
        env = self._wave_get(t, s)
        return self._wave_img(env, base, playing, trig and not playing, muted,
                              selected=(self._sel == (t, s)))

    def render(self) -> None:
        if not self.running:
            return
        # Set every key directly to its final image (NO pre-blank to black —
        # that re-blitted all 32 keys every frame and defeated the cache =
        # flicker). The grid covers cols 0-6; the control column covers col 7.
        if self.client is None:
            for s in range(SCENES):
                for t in range(TRACKS):
                    self.set_key(s * 8 + t,
                                 deck_ui.btn("#7f1d1d", [("no OSC", 12, "#fecaca")]) if s * 8 + t == 0
                                 else deck_ui.btn("#0b0f1a", []))
            for k in (KEY_X2, KEY_DIV2, KEY_LEN, KEY_SEL, KEY_QUANT, KEY_AUTO, KEY_STOP_ALL):
                self.set_key(k, deck_ui.btn("#0b0f1a", []))
            self.render_home_key()
            return
        with self.client.state.lock:
            ntracks = self.client.state.num_tracks
            q = self.client.state.global_quant
        for s in range(SCENES):
            for t in range(TRACKS):
                k = s * 8 + t
                if ntracks == 0:
                    self.set_key(k, deck_ui.btn("#1f2937", [("waiting", 11, "#cbd5e1"),
                                                            ("for Live", 12, "#fff")]) if k == 0
                                 else deck_ui.btn("#0b0f1a", []))
                else:
                    self.set_key(k, self._cell_img(t, s))
        # loop-size column (col 6) — operates on the SELECTED clip
        self._render_size_column()
        # transport column (col 7)
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

    def _sel_len_beats(self):
        """Loop length (beats) of the selected clip, or None."""
        if not self._sel:
            return None
        st = self.client.state
        with st.lock:
            ls = st.slot_loop_start.get(self._sel)
            le = st.slot_loop_end.get(self._sel)
        if ls is None or le is None:
            return None
        return max(0.0, le - ls)

    def _render_size_column(self) -> None:
        length = self._sel_len_beats()
        on = length is not None
        sub = "set" if on else "—"
        self.set_key(KEY_X2, deck_ui.btn("#1e3a8a" if on else "#1f2937",
                                         [("×2", 22, "#fff" if on else "#64748b"),
                                          ("longer", 8, "#bfdbfe" if on else "#475569")]))
        self.set_key(KEY_DIV2, deck_ui.btn("#1e3a8a" if on else "#1f2937",
                                           [("÷2", 22, "#fff" if on else "#64748b"),
                                            ("shorter", 8, "#bfdbfe" if on else "#475569")]))
        if on:
            with self.client.state.lock:
                spb = self.client.state.sig_num or 4
            bars = length / spb
            txt = f"{bars:g} bar" if abs(bars - round(bars)) < 0.02 else f"{length:g} b"
            self.set_key(KEY_LEN, deck_ui.btn("#374151", [("LEN", 9, "#9ca3af"),
                                                          (txt, 14, "#fff"), ("→bar", 8, "#9ca3af")]))
        else:
            self.set_key(KEY_LEN, deck_ui.btn("#1f2937", [("LEN", 11, "#64748b")]))
        if self._sel:
            t, s = self._sel
            with self.client.state.lock:
                tc = self.client.state.track_color.get(t)
            self.set_key(KEY_SEL, deck_ui.btn(self._dim(self._hex(tc), 0.5),
                                              [("SEL", 9, "#cbd5e1"), (f"{t+1}·{s+1}", 16, "#fff")]))
        else:
            self.set_key(KEY_SEL, deck_ui.btn("#1f2937", [("tap a", 9, "#64748b"),
                                                          ("clip", 11, "#94a3b8")]))

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if key % 8 < TRACKS:                  # cols 0-5 = clip grid
            self._grid_gesture(key, pressed)
            return
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
        elif self.client is None:
            return
        elif key == KEY_X2:
            self._resize_sel(2.0)
        elif key == KEY_DIV2:
            self._resize_sel(0.5)
        elif key == KEY_LEN:
            self._snap_sel_to_bar()
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
            playing = st.slot_playing.get((t, s), False)
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
            if self._sel == (t, s):
                self._sel = None
            return
        if has:
            self._sel = (t, s)                   # selected → ÷2 / ×2 target
        if has and playing:
            self.client.toggle_clip_muted(t, s)   # tap a playing clip = mute/unmute (stays in sync)
            return
        if not has:
            self.client.set_arm(t, True)
        self.client.fire_clip_slot(t, s)

    def _resize_sel(self, factor: float) -> None:
        """Halve (÷2) or double (×2) the selected clip's loop length, anchored
        at its loop start — the loop keeps circling, just shorter/longer."""
        if not self._sel:
            return
        t, s = self._sel
        st = self.client.state
        with st.lock:
            ls = st.slot_loop_start.get(self._sel)
            le = st.slot_loop_end.get(self._sel)
        if ls is None or le is None:
            self.client._send("/live/clip/get/loop_start", t, s)   # fetch, try again next tap
            self.client._send("/live/clip/get/loop_end", t, s)
            return
        new_len = max(MIN_LOOP_BEATS, (le - ls) * factor)
        self.client.set_clip_loop(t, s, ls, ls + new_len)
        self.render()

    def _snap_sel_to_bar(self) -> None:
        """Round the selected clip's loop to a whole number of bars."""
        if not self._sel:
            return
        t, s = self._sel
        st = self.client.state
        with st.lock:
            ls = st.slot_loop_start.get(self._sel)
            le = st.slot_loop_end.get(self._sel)
            spb = st.sig_num or 4
        if ls is None or le is None:
            return
        bars = max(1, round((le - ls) / spb))
        self.client.set_clip_loop(t, s, ls, ls + bars * spb)
        self.render()

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
