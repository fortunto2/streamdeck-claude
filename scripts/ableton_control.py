"""Ableton Live control surface — Session clip launcher (presentation).

Hosted by ``scripts/dashboard.py``; wraps the deck-agnostic OSC client in
``src/ableton.py``. Paradigm mirrors an Ableton Push / APC for live DJing:

  Row 0  — SCENES   : fire a whole scene ("master layer" of the set)
  Row 1  — TRACKS   : mute-toggle on/off, with track colour + live VU meter
  Row 2  — ARM      : record-arm each track
  Row 3  — transport: play / stop / stop-all-clips / session-rec
                      + scene paging + back-to-home
"""

from __future__ import annotations

import threading
import time

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface
from src.ableton import AbletonClient, METER_CAP

# Looper Clear / ×2 / ÷2 aren't OSC-controllable (not parameters), so the
# deck sends them as MIDI for the user to map once in Live (Cmd+M).
try:
    from src.midi_out import MidiOut, iac_bus_prefer
except Exception:  # pragma: no cover
    MidiOut = None

MAX_COLS = 8  # Stream Deck XL is 8 keys wide

# MIDI sent for the Looper extras (mapped once in Live → Looper buttons).
# The transport tap goes to MIDI too: setting the State param over OSC does
# not reliably start recording on a freshly-cleared loop, whereas the
# multi-purpose button (mouse / MIDI) always does.
LOOP_MIDI_CH = 15        # MIDI channel 16
LOOP_MIDI_CLEAR = 110
LOOP_MIDI_HALF = 111      # ÷2
LOOP_MIDI_DOUBLE = 112    # ×2
LOOP_MIDI_TRANSPORT = 113  # multi-purpose transport button (rec→play→dub)

# Key layout (content on the first three rows, as requested).
SCENE_ROW = range(0, 8)        # whole-scene launch ("master layer")
MUTE_ROW = range(8, 16)        # track on/off (with VU + colour)
ARM_ROW = range(16, 24)        # track record-arm
KEY_PLAY = 24
KEY_STOP = 25
KEY_REC = 26          # session record (REC left)
KEY_STOP_CLIPS = 27  # stop-all-clips, or Looper CLEAR when a Looper exists
KEY_SCENES_PREV = 28
KEY_SCENES_NEXT = 29
KEY_REFRESH = 30
KEY_HOME = ControlSurface.HOME_KEY  # 31

# VU motion: instant attack, exponential decay so the bar dances and falls
# instead of pinning to the top. Gamma > 1 expands the low end for headroom.
VU_FPS = 20.0
VU_DECAY = 0.80    # multiplier per frame when the signal drops
VU_GAMMA = 1.4     # display = level ** gamma
BEAT_PULSE = 0.30  # seconds a beat flash takes to fade


class AbletonControl(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.client = AbletonClient(on_change=self.request_repaint)
        self.scene_offset = 0
        self._poll_thread: threading.Thread | None = None
        self._last_conn = False
        self._vu_display: dict[int, float] = {}  # decayed level per track
        self._loop_press_ts = None  # set on LOOP press; None = no press seen here
        self._mute_press_ts: dict[int, float] = {}  # tap-vs-hold per track key
        self.midi = None  # MidiOut for the Looper Clear/×2/÷2 extras

    # Looper State → (bg, glyph, label, colour). Glyphs mirror the Looper
    # device: ■ stop / ● record / ▶ play / ✚ overdub.
    _LOOP_CFG = {
        0: ("#374151", "■", "STOP", "#9ca3af"),
        1: ("#dc2626", "●", "REC", "#ffffff"),
        2: ("#16a34a", "▶", "PLAY", "#ffffff"),
        3: ("#d97706", "✚", "DUB", "#ffffff"),
    }
    LOOP_HOLD = 0.5  # seconds held = stop
    MUTE_HOLD = 0.4  # track key: hold = solo, tap = on/off

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.running = True
        self.client.start()
        self.client.refresh()
        # Open a MIDI port (IAC if available, else a virtual port) so the
        # Looper Clear/×2/÷2 keys have something to map to in Live.
        if MidiOut is not None:
            try:
                # Looper control → IAC Bus 1 (Live: Remote on, Track off) so
                # these messages map to buttons and never play notes.
                self.midi = MidiOut(iac_prefer=iac_bus_prefer(1))
            except Exception:
                self.midi = None
        self.render()
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()

    def on_teardown(self) -> None:
        self.client.stop_listening()
        if self.midi is not None:
            try:
                self.midi.all_notes_off(LOOP_MIDI_CH)
                self.midi.close()
            except Exception:
                pass
            self.midi = None

    def _looper_midi(self, note: int) -> None:
        """Momentary MIDI tap for a Looper extra (mapped in Live)."""
        if self.midi is None:
            return
        try:
            self.midi.note_on(note, 127, LOOP_MIDI_CH)
            self.midi.note_off(note, LOOP_MIDI_CH)
        except Exception:
            pass

    # -- background poll: keepalive + meters + reconnect -----------------

    def _poll_loop(self) -> None:
        frame = 1.0 / VU_FPS
        keepalive_every = max(1, int(VU_FPS * 0.8))  # ~0.8 s
        tick = 0
        while self.running:
            try:
                st = self.client.state
                with st.lock:
                    visible = min(st.num_tracks, MAX_COLS)
                    meters = dict(st.track_meter)
                if tick % keepalive_every == 0:
                    self.client.keepalive()
                self.client.request_meters(visible)

                # Attack/decay peak-hold for lively motion.
                for t in range(visible):
                    target = meters.get(t, 0.0)
                    cur = self._vu_display.get(t, 0.0)
                    self._vu_display[t] = target if target >= cur else cur * VU_DECAY

                conn = self.client.connected
                if conn != self._last_conn:
                    self._last_conn = conn
                    if conn:
                        self.client.refresh()  # Live (re)appeared — repopulate
                    self.render()
                elif conn:
                    self._paint_vu_row(visible)
                    self._paint_beat()
                    self._paint_scenes_if_blinking()
                # Defer the heavy clip-slot subscription (~0.7s in) so opening
                # the page stays instant; runs once off the OSC handler thread.
                if conn and tick >= 14 and not self.client._slots_subscribed:
                    self.client._subscribe_slots()
            except Exception:
                pass
            tick += 1
            time.sleep(frame)

    # -- rendering -------------------------------------------------------

    def _mute_img(self, name: str, muted: bool, vu: float, color: int | None,
                  selected: bool = False, soloed: bool = False) -> Image.Image:
        """Track on/off key: colour header + name + ON/OFF + VU bar.

        `selected` draws a bright border (track highlighted in Live).
        `soloed` draws a gold "S" badge (hold the key to toggle solo).
        """
        bg = "#3f1d1d" if muted else "#134e2a"
        img = Image.new("RGB", deck_ui.SIZE, bg)
        d = ImageDraw.Draw(img)
        # Prominent track-colour header (the track's actual colour in Live).
        if color is not None:
            d.rectangle([0, 0, 95, 13], fill=deck_ui.rgb(color))
        f = deck_ui.font(14)
        d.text((48, 42), deck_ui.fit(d, name, f, 74), font=f, fill="#e5e7eb", anchor="mm")
        state = "OFF" if muted else "ON"
        d.text((48, 70), state, font=deck_ui.font(16),
               fill="#f87171" if muted else "#4ade80", anchor="mm")
        deck_ui.vu_bar(d, 0.0 if muted else vu ** VU_GAMMA, top=18)
        if soloed:
            d.ellipse([3, 15, 21, 33], fill="#facc15")
            d.text((12, 24), "S", font=deck_ui.font(13), fill="#1a1a1a", anchor="mm")
        if selected:
            d.rectangle([0, 0, 95, 95], outline="#f8fafc", width=3)
        return img

    def _paint_beat(self) -> None:
        """Beat clock on the PLAY key — "BAR N" + a row of beat dots that
        fill one per beat (●○○○ → ●●○○ → …), green pulse each beat, gold
        ring on the downbeat. Still launches transport when pressed; shows
        the plain PLAY button when stopped.
        """
        if not self.running:
            return
        st = self.client.state
        with st.lock:
            playing = st.playing
            beat = st.beat
            signum = max(1, st.sig_num)
            ts = st.beat_ts
            tempo = st.tempo
        if not playing:
            self.set_key(KEY_PLAY, deck_ui.btn("#14532d",
                                               [("▶", 30, "#ffffff"), ("PLAY", 12, "#bbf7d0")]))
            return
        now = time.monotonic()
        bar = beat // signum + 1
        beat_in_bar = beat % signum + 1
        downbeat = (beat_in_bar == 1)
        # Ableton-style position: bar.beat.sixteenth. The 16th within the
        # beat is interpolated from elapsed time since the last beat + tempo.
        beat_dur = 60.0 / max(tempo, 1.0)
        sixteenth = min(4, max(1, int((now - ts) / beat_dur * 4) + 1))
        intensity = max(0.0, 1.0 - (now - ts) / BEAT_PULSE)
        bg = deck_ui.hexstr(deck_ui.mix((12, 74, 45), (34, 197, 94), intensity))
        img = Image.new("RGB", deck_ui.SIZE, bg)
        d = ImageDraw.Draw(img)
        d.text((48, 26), f"{bar}.{beat_in_bar}.{sixteenth}", font=deck_ui.font(17), fill="#ffffff", anchor="mm")
        # Beat dots — one per beat in the bar, filled up to the current beat.
        n = min(signum, 8)
        r = 8 if n <= 4 else 5
        gap = 8 if n <= 4 else 5
        x = (96 - (n * 2 * r + (n - 1) * gap)) // 2 + r
        cy = 62
        for i in range(n):
            cx = x + i * (2 * r + gap)
            current = (i == beat_in_bar - 1)
            if i < beat_in_bar:
                rr = r + 2 if current else r
                d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                          fill="#fbbf24" if current else "#ffffff")
            else:
                d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#6b7280", width=2)
        if downbeat:
            d.rectangle([0, 0, 95, 95], outline="#fbbf24", width=3)
        self.set_key(KEY_PLAY, img)

    def _loop_img(self, state: int, track: int | None = None, name: str = "") -> Image.Image:
        bg, glyph, label, col = self._LOOP_CFG.get(state, self._LOOP_CFG[0])
        where = f"T{track + 1}" if track is not None else "?"
        if name:
            where += " " + name[:6]
        return deck_ui.btn(bg, [
            (glyph, 30, col),
            (label, 12, col),
            (where, 10, "#fcd34d"),
        ])

    @staticmethod
    def _scene_status(sidx, ntr, slot_playing, slot_triggered) -> str:
        if any(slot_triggered.get((t, sidx), False) for t in range(ntr)):
            return "triggered"  # queued — blinks until it actually starts
        if any(slot_playing.get((t, sidx), False) for t in range(ntr)):
            return "playing"
        return "idle"

    def _scene_img(self, sidx: int, name: str, base: int, status: str, blink_on: bool) -> Image.Image:
        if status == "playing":
            factor, border, sub = 1.0, "#ffffff", "▶ PLAY"
        elif status == "triggered":
            factor = 1.0 if blink_on else 0.32   # blink bright ↔ dim
            border, sub = "#fde047", "QUEUED"
        else:
            factor, border, sub = 0.4, None, "fire"
        bg = deck_ui.dim_hex(base, factor)
        txt = deck_ui.text_for(base, factor)
        return deck_ui.btn(bg, [
            (f"S{sidx + 1}", 12, txt),
            (name, 14, txt),
            (sub, 10, txt),
        ], border=border)

    def _paint_scene_row(self, blink_on: bool) -> None:
        if not self.running:
            return
        st = self.client.state
        with st.lock:
            num_scenes = st.num_scenes
            ntr = min(st.num_tracks, MAX_COLS)
            names = dict(st.scene_names)
            colors = dict(st.scene_color)
            sp = dict(st.slot_playing)
            stg = dict(st.slot_triggered)
        for col, pos in enumerate(SCENE_ROW):
            sidx = self.scene_offset + col
            if sidx < num_scenes:
                name = names.get(sidx) or f"Scene {sidx + 1}"
                base = colors.get(sidx)
                if base is None or base <= 0:
                    base = deck_ui.palette_color(sidx)
                status = self._scene_status(sidx, ntr, sp, stg)
                self.set_key(pos, self._scene_img(sidx, name, base, status, blink_on))
            else:
                self.set_key(pos, deck_ui.btn("#0b0f1a", []))

    def _paint_scenes_if_blinking(self) -> None:
        """Animate queued scenes — repaint the row while any slot is triggered."""
        st = self.client.state
        lo, hi = self.scene_offset, self.scene_offset + MAX_COLS
        with st.lock:
            blinking = any(v for (t, s), v in st.slot_triggered.items() if lo <= s < hi and v)
        if blinking:
            self._paint_scene_row(int(time.monotonic() * 4) % 2 == 0)

    def _paint_vu_row(self, visible: int) -> None:
        """Repaint only the 8 track keys with current VU (fast path)."""
        if not self.running:
            return
        st = self.client.state
        with st.lock:
            names = dict(st.track_names)
            mute = dict(st.track_mute)
            solo = dict(st.track_solo)
            color = dict(st.track_color)
            sel = st.selected_track
        for col, pos in enumerate(MUTE_ROW):
            if col < visible:
                self.set_key(pos, self._mute_img(
                    names.get(col, f"T{col + 1}"),
                    mute.get(col, False),
                    self._vu_display.get(col, 0.0),
                    color.get(col),
                    selected=(col == sel),
                    soloed=solo.get(col, False),
                ))

    def render(self) -> None:
        if not self.running:
            return
        st = self.client.state
        if not self.client.connected:
            self._render_disconnected()
            return
        with st.lock:
            num_scenes = st.num_scenes
            num_tracks = st.num_tracks
            track_names = dict(st.track_names)
            track_mute = dict(st.track_mute)
            track_arm = dict(st.track_arm)
            track_color = dict(st.track_color)
            track_solo = dict(st.track_solo)
            rec = st.session_record
            tempo = st.tempo
            looper = st.looper
            looper_state = st.looper_state
            sel_track = st.selected_track

        visible = min(num_tracks, MAX_COLS)

        # Row 0 — scenes: blink while queued, solid while playing (like Live).
        self._paint_scene_row(blink_on=True)

        # Row 1 — track on/off with colour + VU + selected-track border.
        for col, pos in enumerate(MUTE_ROW):
            if col < visible:
                self.set_key(pos, self._mute_img(
                    track_names.get(col, f"T{col + 1}"),
                    track_mute.get(col, False),
                    self._vu_display.get(col, 0.0),
                    track_color.get(col),
                    selected=(col == sel_track),
                    soloed=track_solo.get(col, False),
                ))
            else:
                self.set_key(pos, deck_ui.btn("#0b0f1a", []))

        # Row 2 — track arm (record activate).
        for col, pos in enumerate(ARM_ROW):
            if col < visible:
                name = track_names.get(col, f"T{col + 1}")
                armed = track_arm.get(col, False)
                bg = "#dc2626" if armed else "#1f2937"
                self.set_key(pos, deck_ui.btn(bg, [
                    (name, 12, "#e5e7eb"),
                    ("● REC" if armed else "arm", 14,
                     "#ffffff" if armed else "#6b7280"),
                ]))
            else:
                self.set_key(pos, deck_ui.btn("#0b0f1a", []))

        # Row 3 — transport + paging + home. PLAY doubles as the beat clock.
        self._paint_beat()
        self.set_key(KEY_STOP, deck_ui.btn("#374151", [
            ("■ STOP", 13, "#d1d5db"),
            (f"{tempo:.1f}", 24, "#fcd34d"),
            ("BPM", 10, "#9ca3af"),
        ]))
        # With a Looper present, keys 26/28/29 become Clear / ÷2 / ×2
        # (these go out as MIDI). Otherwise they're stop-all-clips + paging.
        if looper is not None:
            self.set_key(KEY_STOP_CLIPS, deck_ui.btn("#7f1d1d",
                                                     [("CLEAR", 13, "#fecaca"), ("loop", 10, "#f87171")]))
        else:
            self.set_key(KEY_STOP_CLIPS, deck_ui.btn("#422006",
                                                     [("■■", 24, "#fbbf24"), ("ALL CLIPS", 11, "#fcd34d")]))
        self.set_key(KEY_REC, deck_ui.btn("#dc2626" if rec else "#3f1d1d",
                                          [("●", 30, "#ffffff" if rec else "#f87171"), ("REC", 12, "#fecaca")]))

        if looper is not None:
            self.set_key(KEY_SCENES_PREV, deck_ui.btn("#1e3a5f",
                                                      [("÷2", 24, "#bfdbfe"), ("length", 9, "#93c5fd")]))
            self.set_key(KEY_SCENES_NEXT, deck_ui.btn("#1e3a5f",
                                                      [("×2", 24, "#bfdbfe"), ("length", 9, "#93c5fd")]))
        else:
            more_prev = self.scene_offset > 0
            more_next = (self.scene_offset + MAX_COLS) < num_scenes
            self.set_key(KEY_SCENES_PREV, deck_ui.btn("#1e293b" if more_prev else "#0b0f1a",
                                                      [("▲", 22, "#94a3b8" if more_prev else "#334155"),
                                                       ("scenes", 10, "#64748b")]))
            self.set_key(KEY_SCENES_NEXT, deck_ui.btn("#1e293b" if more_next else "#0b0f1a",
                                                      [("▼", 22, "#94a3b8" if more_next else "#334155"),
                                                       ("scenes", 10, "#64748b")]))
        # Key 30 = Looper control when a Looper device exists, else sync.
        if looper is not None:
            self.set_key(KEY_REFRESH, self._loop_img(
                looper_state, looper[0], track_names.get(looper[0], "")))
        else:
            self.set_key(KEY_REFRESH, deck_ui.btn("#0c4a6e",
                                                  [("↻", 24, "#7dd3fc"), ("sync", 11, "#38bdf8")]))
        self.render_home_key()

    def _render_disconnected(self) -> None:
        for k in range(32):
            if k == KEY_HOME:
                self.render_home_key()
            elif k == KEY_REFRESH:
                self.set_key(k, deck_ui.btn("#0c4a6e", [("↻", 24, "#7dd3fc"), ("retry", 11, "#38bdf8")]))
            else:
                self.set_key(k, deck_ui.btn("#1c1917", []))
        msgs = [
            ("ABLETON", 14, "#fbbf24"), ("OPEN", 14, "#e5e7eb"), ("LIVE", 14, "#e5e7eb"),
            ("+", 18, "#6b7280"), ("SELECT", 12, "#e5e7eb"), ("Ableton", 12, "#e5e7eb"),
            ("OSC", 13, "#e5e7eb"), ("surface", 11, "#9ca3af"),
        ]
        for k, m in enumerate(msgs):
            self.set_key(k, deck_ui.btn("#27272a", [m]))

    # -- input -----------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        # LOOP key (30) needs both edges: a tap advances the Looper's
        # multi-purpose transport (rec → play → dub), a hold stops it.
        if key == KEY_REFRESH and self.client.has_looper():
            if pressed:
                self._loop_press_ts = time.monotonic()
            elif self._loop_press_ts is not None:
                held = time.monotonic() - self._loop_press_ts
                self._loop_press_ts = None
                if held >= self.LOOP_HOLD:
                    self.client.looper_stop()
                else:
                    self._looper_midi(LOOP_MIDI_TRANSPORT)
            return
        # Track on/off key (both edges):
        #   hold        → toggle solo
        #   tap (soloed)→ clear solo (convenient exit, doesn't mute)
        #   tap (else)  → on/off (mute toggle)
        if key in MUTE_ROW:
            col = key - MUTE_ROW.start
            with self.client.state.lock:
                ntr = self.client.state.num_tracks
                soloed = self.client.state.track_solo.get(col, False)
            if col >= ntr:
                return
            if pressed:
                self._mute_press_ts[key] = time.monotonic()
            elif key in self._mute_press_ts:
                held = time.monotonic() - self._mute_press_ts.pop(key)
                if held >= self.MUTE_HOLD or soloed:
                    self.client.toggle_solo(col)
                else:
                    self.client.toggle_mute(col)
            # else: stray release (e.g. the key that launched this page) — ignore
            return
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
            return
        if key == KEY_REFRESH:  # no Looper present → manual re-query
            self.client.refresh()
            self.render()
            return

        # Commands are fire-and-forget UDP — always sent, never gated on
        # "connected" (gating was the "works once then goes deaf" bug).
        st = self.client.state
        with st.lock:
            num_scenes = st.num_scenes
            num_tracks = st.num_tracks

        if key in SCENE_ROW:
            sidx = self.scene_offset + (key - SCENE_ROW.start)
            if sidx < num_scenes:
                self.client.fire_scene(sidx)
        elif key in ARM_ROW:
            col = key - ARM_ROW.start
            if col < num_tracks:
                self.client.toggle_arm(col)
        elif key == KEY_PLAY:
            self.client.play()
        elif key == KEY_STOP:
            self.client.stop_transport()
        elif key == KEY_STOP_CLIPS:
            if self.client.has_looper():
                self._looper_midi(LOOP_MIDI_CLEAR)
            else:
                self.client.stop_all_clips()
        elif key == KEY_REC:
            self.client.toggle_session_record()
        elif key == KEY_SCENES_PREV:
            if self.client.has_looper():
                self._looper_midi(LOOP_MIDI_HALF)
            elif self.scene_offset > 0:
                self.scene_offset = max(0, self.scene_offset - MAX_COLS)
                self.render()
        elif key == KEY_SCENES_NEXT:
            if self.client.has_looper():
                self._looper_midi(LOOP_MIDI_DOUBLE)
            elif (self.scene_offset + MAX_COLS) < num_scenes:
                self.scene_offset += MAX_COLS
                self.render()
