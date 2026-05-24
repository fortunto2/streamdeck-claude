"""REAPER OSC client + state subscriber.

Sends transport / track / FX / marker / envelope commands to REAPER
over OSC (REAPER → Preferences → Control/OSC/Web → Add OSC). Subscribes
to a feedback stream on a separate port so button LEDs can mirror
REAPER's state (current marker, track mute/solo, transport state).

OSC pattern reference: REAPER ships a "Default 8x4.ReaperOSC" pattern
file we mirror here. The addresses we use are all in the stock pattern;
no custom pattern install required.

Usage:
    from src.reaper import ReaperClient
    rc = ReaperClient.from_env()  # reads ports from env vars
    rc.transport_play()
    rc.track_mute(track=3, on=True)
    rc.fx_bypass(track=3, fx=2, on=True)
    rc.goto_marker(5)
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    from pythonosc.dispatcher import Dispatcher
    from pythonosc.osc_server import ThreadingOSCUDPServer
    from pythonosc.udp_client import SimpleUDPClient
except ImportError:  # pragma: no cover — pyproject pins python-osc
    SimpleUDPClient = None
    Dispatcher = None
    ThreadingOSCUDPServer = None

log = logging.getLogger(__name__)


# Default ports — same as REAPER's "Default" OSC config screenshot.
DEFAULT_SEND_PORT = 8000
DEFAULT_LISTEN_PORT = 9000


@dataclass
class ReaperState:
    """Cached REAPER state populated by the OSC feedback subscriber.

    Stream Deck render loop reads from here to colour buttons (mute/
    solo highlight, play/stop transport indicator, current marker).
    """

    playing: bool = False
    recording: bool = False
    looping: bool = False
    bpm: float = 120.0
    current_marker: int | None = None
    # Per-track state. Sparse — only tracks we've heard from are present.
    track_mute: dict[int, bool] = field(default_factory=dict)
    track_solo: dict[int, bool] = field(default_factory=dict)
    track_arm: dict[int, bool] = field(default_factory=dict)
    # Per-(track, fx) bypass state.
    fx_bypass: dict[tuple[int, int], bool] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)


def _enumerate_local_ipv4() -> list[str]:
    """List every local-host IPv4 address we should try to reach REAPER on.

    REAPER's OSC Local IP picks "first active non-loopback interface"
    and shifts every time you swap Wi-Fi, plug Ethernet, or toggle
    Tailscale (100.96.x.x). Sending the same OSC message to every
    candidate means we don't care which one REAPER picked today.

    Three discovery strategies, layered:
      1. The default-route trick — connect a UDP socket to some
         external address; the OS picks an outbound interface and
         `getsockname()` reveals which local IP it used. Gives us
         the IP REAPER most likely binds to.
      2. macOS `ifconfig -l` + `ipconfig getifaddr <iface>` —
         enumerates every active interface (utun for Tailscale,
         en0/en1 for Wi-Fi / Ethernet, lo0 for loopback).
      3. `socket.getaddrinfo(hostname)` as a last fallback.

    Always includes 127.0.0.1.
    """
    import socket
    import subprocess

    addrs: set[str] = {"127.0.0.1"}

    # 1) Default route IP — works on every UNIX without extra deps.
    for probe in ("8.8.8.8", "1.1.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.1)
            s.connect((probe, 80))  # UDP connect doesn't send anything
            ip = s.getsockname()[0]
            s.close()
            if ip:
                addrs.add(ip)
            break
        except Exception:
            continue

    # 2) macOS — enumerate every active interface (utun = Tailscale).
    try:
        ifaces = subprocess.run(
            ["ifconfig", "-l"], capture_output=True, text=True, timeout=1
        ).stdout.split()
        for iface in ifaces:
            r = subprocess.run(
                ["ipconfig", "getifaddr", iface],
                capture_output=True, text=True, timeout=1,
            )
            ip = r.stdout.strip()
            if ip and not ip.startswith("169.254."):
                addrs.add(ip)
    except Exception:
        pass

    # 3) Hostname-based fallback (rarely adds anything on macOS, but
    #    catches edge cases on Linux when ifconfig is absent).
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("169.254."):
                addrs.add(ip)
    except Exception:
        pass

    return sorted(addrs)


class ReaperClient:
    """OSC client to REAPER. Thread-safe; sends are stateless UDP.

    `send_host` can be:
    - "auto"  — broadcast to every local IPv4 interface every send.
                REAPER listens on exactly one of them; the others
                quietly drop the UDP packet. Works across Tailscale
                up/down, Wi-Fi changes, Ethernet plug/unplug — no
                config edit needed.
    - "127.0.0.1" / "192.168.1.2" / etc. — single fixed host.
    """

    def __init__(
        self,
        send_host: str = "127.0.0.1",
        send_port: int = DEFAULT_SEND_PORT,
        listen_host: str = "127.0.0.1",
        listen_port: int = DEFAULT_LISTEN_PORT,
        state_changed: Callable[[ReaperState], None] | None = None,
    ):
        if SimpleUDPClient is None:
            raise RuntimeError(
                "python-osc not installed — `uv pip install python-osc` "
                "(already in pyproject)."
            )
        # Multi-host fan-out when send_host = "auto".
        if send_host == "auto":
            self._hosts = _enumerate_local_ipv4()
            self._clients = [SimpleUDPClient(h, send_port) for h in self._hosts]
        else:
            self._hosts = [send_host]
            self._clients = [SimpleUDPClient(send_host, send_port)]
        self._client = self._clients[0]  # back-compat for code that pokes _client
        self.state = ReaperState()
        self._on_change = state_changed
        self._server: ThreadingOSCUDPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._listen_host = listen_host
        self._listen_port = listen_port

    def _send(self, addr: str, value) -> None:
        """Fan out one OSC message to every send-host client."""
        for c in self._clients:
            try:
                c.send_message(addr, value)
            except Exception:
                pass  # one bad interface shouldn't kill the others

    @classmethod
    def from_env(cls, **kwargs: Any) -> ReaperClient:
        """Build a client from `REAPER_OSC_*` env vars (sensible defaults)."""
        return cls(
            send_host=os.environ.get("REAPER_OSC_SEND_HOST", "127.0.0.1"),
            send_port=int(os.environ.get("REAPER_OSC_SEND_PORT", DEFAULT_SEND_PORT)),
            listen_host=os.environ.get("REAPER_OSC_LISTEN_HOST", "127.0.0.1"),
            listen_port=int(os.environ.get("REAPER_OSC_LISTEN_PORT", DEFAULT_LISTEN_PORT)),
            **kwargs,
        )

    # ── State subscriber ──────────────────────────────────────────

    def start_listening(self) -> None:
        """Start the OSC feedback server in a background thread."""
        if self._server is not None:
            return
        disp = Dispatcher()
        disp.map("/play", self._handle_play)
        disp.map("/stop", self._handle_stop)
        disp.map("/record", self._handle_record)
        disp.map("/repeat", self._handle_repeat)
        disp.map("/tempo/raw", self._handle_tempo)
        disp.map("/marker/current", self._handle_marker)
        disp.map("/track/*/mute", self._handle_track_mute)
        disp.map("/track/*/solo", self._handle_track_solo)
        disp.map("/track/*/recarm", self._handle_track_arm)
        self._server = ThreadingOSCUDPServer((self._listen_host, self._listen_port), disp)
        self._server_thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="reaper-osc"
        )
        self._server_thread.start()
        log.info("REAPER OSC listening on %s:%d", self._listen_host, self._listen_port)

    def stop_listening(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def _notify(self) -> None:
        if self._on_change is not None:
            self._on_change(self.state)

    def _handle_play(self, _addr: str, value: float) -> None:
        with self.state._lock:
            self.state.playing = value >= 0.5
        self._notify()

    def _handle_stop(self, _addr: str, value: float) -> None:
        with self.state._lock:
            if value >= 0.5:
                self.state.playing = False
        self._notify()

    def _handle_record(self, _addr: str, value: float) -> None:
        with self.state._lock:
            self.state.recording = value >= 0.5
        self._notify()

    def _handle_repeat(self, _addr: str, value: float) -> None:
        with self.state._lock:
            self.state.looping = value >= 0.5
        self._notify()

    def _handle_tempo(self, _addr: str, value: float) -> None:
        with self.state._lock:
            self.state.bpm = float(value)
        self._notify()

    def _handle_marker(self, _addr: str, value: float) -> None:
        with self.state._lock:
            self.state.current_marker = int(value)
        self._notify()

    @staticmethod
    def _track_idx_from_addr(addr: str) -> int | None:
        parts = addr.split("/")
        # "/track/3/mute" → "3"
        if len(parts) >= 3 and parts[1] == "track":
            try:
                return int(parts[2])
            except ValueError:
                return None
        return None

    def _handle_track_mute(self, addr: str, value: float) -> None:
        idx = self._track_idx_from_addr(addr)
        if idx is None:
            return
        with self.state._lock:
            self.state.track_mute[idx] = value >= 0.5
        self._notify()

    def _handle_track_solo(self, addr: str, value: float) -> None:
        idx = self._track_idx_from_addr(addr)
        if idx is None:
            return
        with self.state._lock:
            self.state.track_solo[idx] = value >= 0.5
        self._notify()

    def _handle_track_arm(self, addr: str, value: float) -> None:
        idx = self._track_idx_from_addr(addr)
        if idx is None:
            return
        with self.state._lock:
            self.state.track_arm[idx] = value >= 0.5
        self._notify()

    # ── Transport ────────────────────────────────────────────────

    def transport_play(self) -> None:
        self._send("/play", 1)

    def transport_stop(self) -> None:
        self._send("/stop", 1)

    def transport_record(self) -> None:
        self._send("/record", 1)

    def transport_loop_toggle(self) -> None:
        self._send("/repeat", 1)

    def transport_rewind(self) -> None:
        self._send("/rewind", 1)

    def transport_fast_forward(self) -> None:
        self._send("/forward", 1)

    def set_tempo(self, bpm: float) -> None:
        # REAPER's `TEMPO` action accepts `/tempo/raw` for raw BPM.
        self._send("/tempo/raw", float(bpm))

    # ── Tracks ───────────────────────────────────────────────────

    def track_select(self, track: int) -> None:
        self._send(f"/track/{track}/select", 1)

    def track_mute(self, track: int, on: bool | None = None) -> None:
        """Set mute = on, or toggle if `on` is None.

        REAPER OSC convention (Default + StreamDeck patterns):
          /track/N/mute            ← `b` binary set (0 or 1)
          /track/N/mute/toggle     ← `t` trigger to flip
        """
        if on is None:
            self._send(f"/track/{track}/mute/toggle", 1)
        else:
            self._send(f"/track/{track}/mute", 1 if on else 0)

    def track_solo(self, track: int, on: bool | None = None) -> None:
        if on is None:
            self._send(f"/track/{track}/solo/toggle", 1)
        else:
            self._send(f"/track/{track}/solo", 1 if on else 0)

    def track_arm(self, track: int, on: bool | None = None) -> None:
        if on is None:
            self._send(f"/track/{track}/recarm/toggle", 1)
        else:
            self._send(f"/track/{track}/recarm", 1 if on else 0)

    def track_volume_db(self, track: int, db: float) -> None:
        # REAPER OSC volume is normalised 0..1, with custom curve. The
        # `/track/N/volume/db` address (custom pattern) takes raw dB —
        # users on a default pattern should send the normalised form via
        # `track_volume_normalised`.
        self._send(f"/track/{track}/volume/db", float(db))

    def track_volume_normalised(self, track: int, value: float) -> None:
        """0..1 normalised volume — works with the default REAPER OSC pattern."""
        self._send(f"/track/{track}/volume", float(value))

    # ── FX ───────────────────────────────────────────────────────

    def fx_bypass(self, track: int, fx: int, on: bool | None = None) -> None:
        """Toggle or set FX bypass on a track's FX chain slot.

        REAPER `FX_BYPASS` action:
          /track/N/fxbypass/M           ← `b` binary (1=active, 0=bypassed)
          /track/N/fxbypass/M/toggle    ← `t` trigger to flip
        """
        if on is None:
            self._send(f"/track/{track}/fxbypass/{fx}/toggle", 1)
        else:
            # `on` here means "FX is doing its thing" → active=1, bypassed=0.
            self._send(f"/track/{track}/fxbypass/{fx}", 1 if on else 0)

    def fx_param(self, track: int, fx: int, param: int, value: float) -> None:
        """Set an FX param value, normalised 0..1."""
        self._send(
            f"/track/{track}/fx/{fx}/fxparam/{param}/value", float(value)
        )

    def fx_param_select(self, track: int, fx: int, param: int) -> None:
        """Make this param the focused one (highlighted in the FX UI)."""
        self._send(
            f"/track/{track}/fx/{fx}/fxparam/{param}/select", 1
        )

    # ── Markers ──────────────────────────────────────────────────

    def goto_marker(self, marker_idx: int) -> None:
        # 1-indexed marker number.
        self._send(f"/marker/{marker_idx}", 1)

    def insert_marker_here(self) -> None:
        self._send("/action/40157", 1)  # Insert marker at edit cursor

    # ── Region & scene jumps (live arrangement) ──────────────────

    def goto_region(self, region_idx: int) -> None:
        self._send(f"/region/{region_idx}", 1)

    # ── Generic action dispatch ──────────────────────────────────

    def action(self, command_id: int) -> None:
        """Fire any REAPER action by its command ID (Help → Actions list)."""
        self._send(f"/action/{command_id}", 1)
