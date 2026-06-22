"""Ableton Live OSC client + live session state (domain layer).

Talks to AbletonOSC (https://github.com/ideoforms/AbletonOSC), a free Live
Remote Script. Deck-agnostic — mirrors ``src/reaper.py``: a thread-safe
``SessionState`` cache populated by an OSC feedback listener, plus
fire-and-forget command methods. The Stream Deck presentation lives in
``scripts/ableton_control.py``.

Setup once:
  1. AbletonOSC cloned into
       ~/Music/Ableton/User Library/Remote Scripts/AbletonOSC
  2. Live → Settings → Link/Tempo/MIDI → Control Surface → "AbletonOSC"
     (Input/Output left "None" — it talks over UDP, not MIDI).
  3. Restart Live. AbletonOSC listens on UDP 11000, replies on 11001.
"""

from __future__ import annotations

import threading
import time

try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import ThreadingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient
except ImportError:  # pragma: no cover — python-osc pinned in pyproject
    Dispatcher = None
    ThreadingOSCUDPServer = None
    SimpleUDPClient = None

# AbletonOSC defaults (do not change unless you edit AbletonOSC's config).
SEND_HOST = "127.0.0.1"
SEND_PORT = 11000   # AbletonOSC listens here
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 11001  # AbletonOSC replies here

CONNECT_TIMEOUT = 3.0  # seconds of silence before we consider Live gone
METER_CAP = 8          # only meter the first N tracks (one deck row wide)

# Ableton Looper device: the "State" automation parameter is the multi-
# purpose transport (0=Stop, 1=Record, 2=Play, 3=Overdub). Its index in
# the Looper's parameter list is fixed by Live. Every Live device's on/off
# switch is parameter 0 (used for FX bypass on the Vocal Looper page).
LOOPER_STATE_PARAM = 1
DEVICE_ON_PARAM = 0
LOOPER_STOP, LOOPER_RECORD, LOOPER_PLAY, LOOPER_OVERDUB = 0, 1, 2, 3


class SessionState:
    """Snapshot of Live's session, populated by OSC feedback."""

    def __init__(self):
        self.lock = threading.Lock()
        self.num_scenes = 0
        self.num_tracks = 0
        self.scene_names: dict[int, str] = {}
        self.scene_color: dict[int, int] = {}
        # Per clip-slot launch state (track, scene) → bool, for the
        # Ableton-style "queued blinks / playing is solid" scene feedback.
        self.slot_playing: dict[tuple[int, int], bool] = {}
        self.slot_triggered: dict[tuple[int, int], bool] = {}
        self.track_names: dict[int, str] = {}
        self.track_mute: dict[int, bool] = {}
        self.track_arm: dict[int, bool] = {}
        self.track_solo: dict[int, bool] = {}
        self.track_color: dict[int, int] = {}
        self.track_meter: dict[int, float] = {}
        self.playing = False
        self.session_record = False
        self.tempo = 120.0
        self.selected_track: int | None = None  # highlighted track in Live
        # Transport position for the bar/beat clock. `beat` is the global
        # beat index (0-based); bar/beat-in-bar derive from `sig_num`.
        self.beat = 0
        self.beat_ts = 0.0  # monotonic time the last beat arrived (for pulse)
        self.sig_num = 4
        self.sig_denom = 4
        # First Looper device found in the set, as (track_idx, device_idx),
        # plus its current State (see LOOPER_* constants).
        self.looper: tuple[int, int] | None = None
        self.looper_state = LOOPER_STOP
        # Per-track device model for the Vocal Looper page: device names,
        # the looper device index per track, its live State, and the on/off
        # of each device (for FX bypass toggles — no MIDI mapping needed).
        self.track_devices: dict[int, list[str]] = {}
        self.track_loopers: dict[int, int] = {}
        self.looper_states: dict[int, int] = {}
        self.device_on: dict[tuple[int, int], bool] = {}
        # Last scene we fired locally — used to highlight the active row
        # (Live's API has no single "playing scene" property).
        self.fired_scene: int | None = None


class AbletonClient:
    """OSC client to AbletonOSC. Queries the session and mirrors state.

    `on_change` fires only when a mirrored value actually changes, so the
    presentation layer can repaint without churning on keepalive traffic.
    """

    def __init__(self, on_change=None):
        if SimpleUDPClient is None:
            raise RuntimeError("python-osc not installed")
        self._client = SimpleUDPClient(SEND_HOST, SEND_PORT)
        self.state = SessionState()
        self._on_change = on_change
        self._server: ThreadingOSCUDPServer | None = None
        self._thread: threading.Thread | None = None
        self.last_feedback = 0.0  # monotonic timestamp of last reply
        self._slots_subscribed = False  # clip-slot listeners set up once

    # -- low-level send --------------------------------------------------

    def _send(self, addr: str, *args) -> None:
        try:
            if not args:
                self._client.send_message(addr, [])
            elif len(args) == 1:
                self._client.send_message(addr, args[0])
            else:
                self._client.send_message(addr, list(args))
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return (time.monotonic() - self.last_feedback) < CONNECT_TIMEOUT

    # -- feedback listener ----------------------------------------------

    def start(self) -> None:
        if self._server is not None:
            return
        disp = Dispatcher()
        disp.map("/live/song/get/num_scenes", self._h_num_scenes)
        disp.map("/live/song/get/num_tracks", self._h_num_tracks)
        disp.map("/live/song/get/track_names", self._h_track_names)
        disp.map("/live/scene/get/name", self._h_scene_name)
        disp.map("/live/scene/get/color", self._h_scene_color)
        disp.map("/live/clip_slot/get/is_playing", self._h_slot_playing)
        disp.map("/live/clip_slot/get/is_triggered", self._h_slot_triggered)
        disp.map("/live/song/get/beat", self._h_beat)
        disp.map("/live/song/get/signature_numerator", self._h_sig_num)
        disp.map("/live/song/get/signature_denominator", self._h_sig_denom)
        disp.map("/live/track/get/name", self._h_track_name)
        disp.map("/live/track/get/devices/name", self._h_track_devices)
        disp.map("/live/device/get/parameter/value", self._h_device_param)
        disp.map("/live/track/get/mute", self._h_track_mute)
        disp.map("/live/track/get/arm", self._h_track_arm)
        disp.map("/live/track/get/solo", self._h_track_solo)
        disp.map("/live/track/get/color", self._h_track_color)
        disp.map("/live/track/get/output_meter_level", self._h_track_meter)
        disp.map("/live/song/get/is_playing", self._h_is_playing)
        disp.map("/live/song/get/session_record", self._h_session_record)
        disp.map("/live/song/get/tempo", self._h_tempo)
        disp.map("/live/view/get/selected_track", self._h_selected_track)
        disp.set_default_handler(self._h_any)
        try:
            self._server = ThreadingOSCUDPServer((LISTEN_HOST, LISTEN_PORT), disp)
        except OSError:
            # Port busy (a stale listener) — keep the sender so commands
            # still reach Live even without feedback.
            self._server = None
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="ableton-osc"
        )
        self._thread.start()

    def stop_listening(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None

    def _touch(self) -> None:
        self.last_feedback = time.monotonic()

    def _notify(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                pass

    # -- query the whole session ----------------------------------------

    def refresh(self) -> None:
        """Ask Live for transport + scene + track state, and subscribe."""
        self._send("/live/song/get/tempo")
        self._send("/live/song/get/is_playing")
        self._send("/live/song/get/session_record")
        self._send("/live/song/start_listen/is_playing")
        self._send("/live/song/start_listen/session_record")
        self._send("/live/song/get/signature_numerator")
        self._send("/live/song/get/signature_denominator")
        self._send("/live/song/start_listen/signature_numerator")
        self._send("/live/song/start_listen/signature_denominator")
        self._send("/live/song/start_listen/beat")  # → /live/song/get/beat per beat
        self._send("/live/view/get/selected_track")
        self._send("/live/view/start_listen/selected_track")
        self._send("/live/song/get/num_scenes")
        self._send("/live/song/get/num_tracks")
        self._send("/live/song/get/track_names")

    def _subscribe_tracks(self, n: int) -> None:
        # start_listen already replies with the current value, so no
        # separate get queries are needed (keeps the startup burst small).
        for t in range(min(n, 64)):
            self._send("/live/track/start_listen/name", t)
            self._send("/live/track/start_listen/color", t)
            self._send("/live/track/start_listen/mute", t)
            self._send("/live/track/start_listen/arm", t)
            self._send("/live/track/start_listen/solo", t)

    def _subscribe_scenes(self, n: int) -> None:
        for s in range(min(n, 128)):
            self._send("/live/scene/start_listen/name", s)
            self._send("/live/scene/start_listen/color", s)

    def _subscribe_slots(self) -> None:
        """Listen to each clip slot's play/trigger state so scene keys can
        blink while queued and go solid while playing.

        Done ONCE (both counts must be known) and with start_listen only —
        which already replies with the current value — so Live isn't flooded
        with ~1k OSC messages at page open (that stalled the deck briefly).
        """
        with self.state.lock:
            nt = min(self.state.num_tracks, 8)
            ns = min(self.state.num_scenes, 16)
        if self._slots_subscribed or nt == 0 or ns == 0:
            return
        self._slots_subscribed = True
        for s in range(ns):
            for t in range(nt):
                self._send("/live/clip_slot/start_listen/is_playing", t, s)
                self._send("/live/clip_slot/start_listen/is_triggered", t, s)

    # -- handlers (notify only on real change to avoid repaint churn) ----

    def _h_any(self, addr, *args):
        self._touch()

    def _h_num_scenes(self, addr, *args):
        self._touch()
        if args:
            n = int(args[0])
            with self.state.lock:
                changed = self.state.num_scenes != n
                self.state.num_scenes = n
            self._subscribe_scenes(n)
            if changed:
                self._notify()

    def _h_num_tracks(self, addr, *args):
        self._touch()
        if args:
            n = int(args[0])
            with self.state.lock:
                changed = self.state.num_tracks != n
                self.state.num_tracks = n
            self._subscribe_tracks(n)
            # Hunt for a Looper device across the tracks (once per count).
            for t in range(min(n, 64)):
                self._send("/live/track/get/devices/name", t)
            if changed:
                self._notify()

    def _h_track_names(self, addr, *args):
        self._touch()
        changed = False
        with self.state.lock:
            for i, name in enumerate(args):
                if self.state.track_names.get(i) != str(name):
                    self.state.track_names[i] = str(name)
                    changed = True
            if args:
                self.state.num_tracks = max(self.state.num_tracks, len(args))
        if changed:
            self._notify()

    def _h_scene_name(self, addr, *args):
        self._touch()
        if len(args) >= 2:
            with self.state.lock:
                changed = self.state.scene_names.get(int(args[0])) != str(args[1])
                self.state.scene_names[int(args[0])] = str(args[1])
            if changed:
                self._notify()

    def _h_scene_color(self, addr, *args):
        self._touch()
        if len(args) >= 2:
            try:
                c = int(args[1])
            except (TypeError, ValueError):
                return  # uncolored scene — surface falls back to a palette
            with self.state.lock:
                changed = self.state.scene_color.get(int(args[0])) != c
                self.state.scene_color[int(args[0])] = c
            if changed:
                self._notify()

    def _h_slot_playing(self, addr, *args):
        self._touch()
        if len(args) >= 3:
            key = (int(args[0]), int(args[1]))
            v = bool(args[2])
            with self.state.lock:
                changed = self.state.slot_playing.get(key) != v
                self.state.slot_playing[key] = v
            if changed:
                self._notify()

    def _h_slot_triggered(self, addr, *args):
        self._touch()
        if len(args) >= 3:
            key = (int(args[0]), int(args[1]))
            v = bool(args[2])
            with self.state.lock:
                changed = self.state.slot_triggered.get(key) != v
                self.state.slot_triggered[key] = v
            if changed:
                self._notify()

    def _h_beat(self, addr, *args):
        # Fires once per beat. Animated by the presentation poll loop, so
        # update silently (no _notify → no full repaint storm).
        self._touch()
        if args:
            try:
                with self.state.lock:
                    self.state.beat = int(args[0])
                    self.state.beat_ts = time.monotonic()
            except (TypeError, ValueError):
                pass

    def _h_sig_num(self, addr, *args):
        self._touch()
        if args:
            with self.state.lock:
                changed = self.state.sig_num != int(args[0])
                self.state.sig_num = int(args[0])
            if changed:
                self._notify()

    def _h_sig_denom(self, addr, *args):
        self._touch()
        if args:
            with self.state.lock:
                changed = self.state.sig_denom != int(args[0])
                self.state.sig_denom = int(args[0])
            if changed:
                self._notify()

    def _h_track_name(self, addr, *args):
        self._touch()
        if len(args) >= 2:
            with self.state.lock:
                changed = self.state.track_names.get(int(args[0])) != str(args[1])
                self.state.track_names[int(args[0])] = str(args[1])
            if changed:
                self._notify()

    def _h_track_devices(self, addr, *args):
        self._touch()
        if not args:
            return
        track = int(args[0])
        names = [str(nm) for nm in args[1:]]
        with self.state.lock:
            self.state.track_devices[track] = names
        loopers = [i for i, nm in enumerate(names) if "looper" in nm.lower()]
        if not loopers:
            return  # not a vocal-loop layer — don't flood Live with subscriptions
        first_looper = loopers[0]
        with self.state.lock:
            self.state.track_loopers[track] = first_looper
        # Looper State → LED (mirrors Live UI / foot pedal too).
        self._send("/live/device/start_listen/parameter/value", track, first_looper, LOOPER_STATE_PARAM)
        self._send("/live/device/get/parameter/value", track, first_looper, LOOPER_STATE_PARAM)
        # FX bypass LEDs — only the first 3 non-looper devices on this layer.
        fx = [i for i in range(len(names)) if i not in loopers][:3]
        for di in fx:
            self._send("/live/device/start_listen/parameter/value", track, di, DEVICE_ON_PARAM)
            self._send("/live/device/get/parameter/value", track, di, DEVICE_ON_PARAM)
        self._recompute_active_looper()

    def _recompute_active_looper(self) -> None:
        """The Ableton page is a single-looper view bound (in Live) to the first
        looper, so point its LED + Clear/×2/÷2 at the lowest-track looper. Per-
        looper control lives on the Vocal Looper page."""
        with self.state.lock:
            lps = self.state.track_loopers
            t = min(lps) if lps else None
            new = (t, lps[t]) if t is not None else None
            changed = self.state.looper != new
            self.state.looper = new
            if new is not None:
                self.state.looper_state = self.state.looper_states.get(t, LOOPER_STOP)
        if changed:
            self._notify()

    def _h_device_param(self, addr, *args):
        self._touch()
        if len(args) < 4:
            return
        try:
            track, device, param, value = int(args[0]), int(args[1]), int(args[2]), float(args[3])
        except (TypeError, ValueError):
            return
        with self.state.lock:
            is_looper = self.state.track_loopers.get(track) == device
        changed = False
        if param == LOOPER_STATE_PARAM and is_looper:
            v = int(round(value))
            with self.state.lock:
                changed = self.state.looper_states.get(track) != v
                self.state.looper_states[track] = v
                if self.state.looper == (track, device):
                    self.state.looper_state = v   # the Ableton-page (first) looper
            if changed:
                self._notify()
            return
        elif param == DEVICE_ON_PARAM:
            on = value >= 0.5
            with self.state.lock:
                changed = self.state.device_on.get((track, device)) != on
                self.state.device_on[(track, device)] = on
        if changed:
            self._notify()

    def _h_track_mute(self, addr, *args):
        self._touch()
        if len(args) >= 2:
            with self.state.lock:
                changed = self.state.track_mute.get(int(args[0])) != bool(args[1])
                self.state.track_mute[int(args[0])] = bool(args[1])
            if changed:
                self._notify()

    def _h_track_arm(self, addr, *args):
        self._touch()
        if len(args) >= 2:
            with self.state.lock:
                changed = self.state.track_arm.get(int(args[0])) != bool(args[1])
                self.state.track_arm[int(args[0])] = bool(args[1])
            if changed:
                self._notify()

    def _h_track_solo(self, addr, *args):
        self._touch()
        if len(args) >= 2:
            with self.state.lock:
                changed = self.state.track_solo.get(int(args[0])) != bool(args[1])
                self.state.track_solo[int(args[0])] = bool(args[1])
            if changed:
                self._notify()

    def _h_track_color(self, addr, *args):
        self._touch()
        if len(args) >= 2:
            try:
                c = int(args[1])
            except (TypeError, ValueError):
                return
            with self.state.lock:
                changed = self.state.track_color.get(int(args[0])) != c
                self.state.track_color[int(args[0])] = c
            if changed:
                self._notify()

    def _h_track_meter(self, addr, *args):
        # High-rate poll reply — update silently; the presentation layer
        # repaints the VU row on its own schedule (no _notify).
        self._touch()
        if len(args) >= 2:
            try:
                self.state.track_meter[int(args[0])] = float(args[1])
            except (TypeError, ValueError):
                pass

    def _h_is_playing(self, addr, *args):
        self._touch()
        if args:
            with self.state.lock:
                changed = self.state.playing != bool(args[0])
                self.state.playing = bool(args[0])
            if changed:
                self._notify()

    def _h_session_record(self, addr, *args):
        self._touch()
        if args:
            with self.state.lock:
                changed = self.state.session_record != bool(args[0])
                self.state.session_record = bool(args[0])
            if changed:
                self._notify()

    def _h_tempo(self, addr, *args):
        self._touch()
        if args:
            with self.state.lock:
                changed = abs(self.state.tempo - float(args[0])) > 0.01
                self.state.tempo = float(args[0])
            if changed:
                self._notify()

    def _h_selected_track(self, addr, *args):
        self._touch()
        if args:
            try:
                v = int(args[0])
            except (TypeError, ValueError):
                return
            with self.state.lock:
                changed = self.state.selected_track != v
                self.state.selected_track = v
            if changed:
                self._notify()

    # -- actions (fire-and-forget — always sent, never gated) -----------

    def fire_scene(self, scene: int) -> None:
        self._send("/live/scene/fire", scene)
        with self.state.lock:
            self.state.fired_scene = scene
        self._notify()

    def play(self) -> None:
        self._send("/live/song/start_playing")

    def stop_transport(self) -> None:
        self._send("/live/song/stop_playing")

    def stop_all_clips(self) -> None:
        self._send("/live/song/stop_all_clips")
        with self.state.lock:
            self.state.fired_scene = None
        self._notify()

    def toggle_mute(self, track: int) -> None:
        with self.state.lock:
            cur = self.state.track_mute.get(track, False)
            self.state.track_mute[track] = not cur   # optimistic — instant LED
        self._send("/live/track/set/mute", track, 0 if cur else 1)

    def toggle_arm(self, track: int) -> None:
        with self.state.lock:
            cur = self.state.track_arm.get(track, False)
            self.state.track_arm[track] = not cur     # optimistic
        self._send("/live/track/set/arm", track, 0 if cur else 1)

    def toggle_solo(self, track: int) -> None:
        with self.state.lock:
            cur = self.state.track_solo.get(track, False)
        self._send("/live/track/set/solo", track, 0 if cur else 1)

    def toggle_session_record(self) -> None:
        with self.state.lock:
            cur = self.state.session_record
        self._send("/live/song/set/session_record", 0 if cur else 1)

    # -- Looper (multi-purpose transport on the Looper "State" param) ---

    def has_looper(self) -> bool:
        with self.state.lock:
            return self.state.looper is not None

    def _set_looper_state(self, value: int) -> None:
        with self.state.lock:
            lp = self.state.looper
        if lp is None:
            return
        self._send("/live/device/set/parameter/value", lp[0], lp[1], LOOPER_STATE_PARAM, value)
        with self.state.lock:
            self.state.looper_state = value  # optimistic; listener confirms
        self._notify()

    def looper_stop(self) -> None:
        self._set_looper_state(LOOPER_STOP)

    def stop_all_loopers(self) -> None:
        """Stop every looper at once (OSC State→Stop — reliable, unlike record)."""
        with self.state.lock:
            loopers = list(self.state.track_loopers.items())
        for track, dev in loopers:
            self._send("/live/device/set/parameter/value", track, dev, LOOPER_STATE_PARAM, LOOPER_STOP)

    def solo_record(self, active_track: int) -> None:
        """Only one looper records at a time: drop every OTHER looper that's
        recording/overdubbing back to play, so layering one can't accidentally
        leave a second one writing. (record→play over OSC is reliable.)"""
        with self.state.lock:
            loopers = list(self.state.track_loopers.items())
            states = dict(self.state.looper_states)
        for track, dev in loopers:
            if track != active_track and states.get(track, LOOPER_STOP) in (LOOPER_RECORD, LOOPER_OVERDUB):
                self._send("/live/device/set/parameter/value", track, dev, LOOPER_STATE_PARAM, LOOPER_PLAY)

    # -- Vocal Looper: per-track devices / FX bypass -------------------

    def vocal_tracks(self, limit: int = 4) -> list[int]:
        """Tracks that host a Looper — the vocal-loop layers, in order."""
        with self.state.lock:
            return sorted(self.state.track_loopers.keys())[:limit]

    def fx_devices(self, track: int, limit: int = 3) -> list[int]:
        """Non-looper device indices on a track (FX bypass slots)."""
        with self.state.lock:
            names = self.state.track_devices.get(track, [])
            looper = self.state.track_loopers.get(track)
        return [i for i in range(len(names)) if i != looper][:limit]

    def device_name(self, track: int, device: int) -> str:
        with self.state.lock:
            names = self.state.track_devices.get(track, [])
        return names[device] if 0 <= device < len(names) else "—"

    def device_is_on(self, track: int, device: int) -> bool:
        with self.state.lock:
            return self.state.device_on.get((track, device), True)

    def toggle_device(self, track: int, device: int) -> None:
        on = self.device_is_on(track, device)
        self._send("/live/device/set/parameter/value", track, device, DEVICE_ON_PARAM, 0.0 if on else 1.0)
        with self.state.lock:
            self.state.device_on[(track, device)] = not on   # optimistic; surface
            # repaints the one key. The OSC confirm matches → no _notify storm.

    def looper_state_of(self, track: int) -> int:
        with self.state.lock:
            return self.state.looper_states.get(track, LOOPER_STOP)

    def request_meters(self, n: int) -> None:
        for t in range(min(n, METER_CAP)):
            self._send("/live/track/get/output_meter_level", t)

    def keepalive(self) -> None:
        # A cheap query keeps feedback flowing so `connected` stays true
        # even when the user isn't changing anything.
        self._send("/live/song/get/is_playing")
        self._send("/live/song/get/session_record")
