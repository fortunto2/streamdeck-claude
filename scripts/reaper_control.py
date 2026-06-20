"""REAPER control surface — transport + live mixer (presentation).

Hosted by ``scripts/dashboard.py``; wraps the OSC client in
``src/reaper.py`` (shared with the standalone ``src/daemon.py`` REAPER
mode, so interface auto-discovery and feedback are identical).

REAPER setup (once): Preferences → Control/OSC/Web → Add → OSC.
  Mode: "Configure device IP+local port"
  Device IP: 127.0.0.1   Device port: 9000   Local port: 8000
  Pattern: "Default.ReaperOSC" (ships with REAPER).

Layout:
  Row 0  — transport: rew / play / stop / rec / loop / mark / |< / >|
  Row 1  — tracks 1-8 MUTE (track name + VU bar + LED)
  Row 2  — tracks 1-8 SOLO
  Row 3  — markers 1-6 + sync + home
"""

from __future__ import annotations

import os
import threading

from PIL import Image, ImageDraw

import deck_ui
from control_surface import ControlSurface  # also bootstraps the src/ path

from src.reaper import ReaperClient

N_TRACKS = 8
TRANSPORT_ROW = range(0, 8)
MUTE_ROW = range(8, 16)
SOLO_ROW = range(16, 24)
MARKER_ROW = range(24, 30)   # markers 1-6
KEY_SYNC = 30
KEY_HOME = ControlSurface.HOME_KEY  # 31

# Transport keys: (pos, reaper_method, glyph, label, active_colour)
_TRANSPORT = [
    (0, "transport_rewind", "⏮", "REW", "#374151"),
    (1, "transport_play", "▶", "PLAY", "#16a34a"),
    (2, "transport_stop", "■", "STOP", "#374151"),
    (3, "transport_record", "●", "REC", "#dc2626"),
    (4, "transport_loop_toggle", "↻", "LOOP", "#a855f7"),
    (5, "insert_marker_here", "📍", "MARK", "#0c4a6e"),
    (6, "transport_rewind", "|◀", "START", "#374151"),
    (7, "transport_fast_forward", "▶|", "END", "#374151"),
]


class ReaperControl(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self.client: ReaperClient | None = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.running = True
        try:
            self.client = ReaperClient(
                send_host=os.environ.get("REAPER_OSC_SEND_HOST", "auto"),
                send_port=int(os.environ.get("REAPER_OSC_SEND_PORT", 8000)),
                listen_host=os.environ.get("REAPER_OSC_LISTEN_HOST", "127.0.0.1"),
                listen_port=int(os.environ.get("REAPER_OSC_LISTEN_PORT", 9000)),
                state_changed=lambda _st: self.request_repaint(),
            )
            self.client.start_listening()
            threading.Thread(target=self._discover, daemon=True).start()
            self.client.action(40769)  # unselect all tracks — cheap state refresh
        except Exception:
            self.client = None
        self.render()

    def _discover(self) -> None:
        if self.client is None:
            return
        try:
            self.client.discover_active_host(timeout=1.0)
        except Exception:
            pass
        self.request_repaint()

    def on_teardown(self) -> None:
        if self.client is not None:
            try:
                self.client.stop_listening()
            except Exception:
                pass

    # -- rendering -------------------------------------------------------

    def render(self) -> None:
        if not self.running:
            return
        st = self.client.state if self.client else None
        playing = looping = recording = False
        mute: dict[int, bool] = {}
        solo: dict[int, bool] = {}
        names: dict[int, str] = {}
        vu: dict[int, float] = {}
        if st is not None:
            with st._lock:
                playing, looping, recording = st.playing, st.looping, st.recording
                mute = dict(st.track_mute)
                solo = dict(st.track_solo)
                names = dict(st.track_name)
                vu = dict(st.track_vu)

        for pos, method, glyph, label, on_color in _TRANSPORT:
            lit = (
                (method == "transport_play" and playing)
                or (method == "transport_loop_toggle" and looping)
                or (method == "transport_record" and recording)
            )
            bg = on_color if lit else "#1e3a5f"
            self.set_key(pos, deck_ui.btn(bg, [(glyph, 26, "#ffffff"), (label, 11, "#cbd5e1")]))

        for col in range(N_TRACKS):
            track = col + 1
            name = names.get(track) or f"T{track}"
            level = vu.get(track, 0.0)
            color = deck_ui.palette_color(col)  # REAPER OSC has no track colour
            self.set_key(MUTE_ROW.start + col, self._track_img("M", name, level, mute.get(track, False), color))
            self.set_key(SOLO_ROW.start + col, self._track_img("S", name, level, solo.get(track, False), color))

        for i, pos in enumerate(MARKER_ROW):
            self.set_key(pos, deck_ui.btn("#0f766e", [("M", 12, "#99f6e4"), (str(i + 1), 26, "#ffffff")]))
        sync_bg = "#0c4a6e"
        if self.client is not None and not self.client.state.track_name:
            sync_bg = "#7c2d12"  # hint: no feedback yet — check REAPER OSC config
        self.set_key(KEY_SYNC, deck_ui.btn(sync_bg, [("↻", 22, "#7dd3fc"), ("sync", 11, "#38bdf8")]))
        self.render_home_key()

    def _track_img(self, kind: str, name: str, level: float, active: bool,
                   color: int | None = None) -> Image.Image:
        if active:
            bg = "#7f1d1d" if kind == "M" else "#92400e"
            txt = "#fde68a"
        else:
            bg, txt = "#1e3a5f", "#cbd5e1"
        img = Image.new("RGB", deck_ui.SIZE, bg)
        d = ImageDraw.Draw(img)
        # Track-identity colour header.
        if color is not None:
            d.rectangle([0, 0, 95, 12], fill=deck_ui.rgb(color))
        chip_color = ("#ef4444" if kind == "M" else "#facc15") if active else "#475569"
        d.text((6, 16), kind, font=deck_ui.font(14), fill=chip_color)
        f = deck_ui.font(16)
        d.text((48, 52), deck_ui.fit(d, name, f, 70), font=f, fill=txt, anchor="mm")
        deck_ui.vu_bar(d, level, top=20)
        return img

    # -- input -----------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if not pressed:
            return
        if key == KEY_HOME:
            self.on_home()
            return
        if key == KEY_SYNC:
            if self.client is not None:
                threading.Thread(target=self._discover, daemon=True).start()
                self.client.action(40769)
            return
        if self.client is None:
            return
        rc = self.client

        for pos, method, *_ in _TRANSPORT:
            if key == pos:
                fn = getattr(rc, method, None)
                if fn:
                    fn()
                return
        if key in MUTE_ROW:
            rc.track_mute(track=(key - MUTE_ROW.start) + 1)
        elif key in SOLO_ROW:
            rc.track_solo(track=(key - SOLO_ROW.start) + 1)
        elif key in MARKER_ROW:
            rc.goto_marker((key - MARKER_ROW.start) + 1)
