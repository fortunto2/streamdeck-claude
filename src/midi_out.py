"""MIDI output port — Stream Deck as a MIDI controller.

Three strategies, in order of preference:

1. **macOS IAC Driver** — if `Audio MIDI Setup.app → MIDI Studio →
   IAC Driver` is online with at least one bus, we open the first
   available bus. The IAC port is system-persistent — survives daemon
   restarts. REAPER's MIDI Devices preference holds the enable state
   forever once you turn it on.
2. **Existing port matching `port_name`** — if the user has another
   loopback (loopMIDI on Windows, etc.) that matches "StreamDeck".
3. **Virtual port** — open a fresh virtual port named `StreamDeck`.
   Disappears when the daemon exits, so REAPER may forget to re-enable
   it on next launch (the well-known macOS virtual-port quirk).
"""

from __future__ import annotations

import logging
import threading
import time

try:
    import rtmidi
except ImportError:  # pragma: no cover — pinned in pyproject
    rtmidi = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

PORT_NAME = "StreamDeck"


class MidiOut:
    """Wraps a MIDI output port. Thread-safe note send."""

    def __init__(self, port_name: str = PORT_NAME, prefer_iac: bool = True,
                 iac_prefer: list[str] | None = None):
        if rtmidi is None:
            raise RuntimeError("python-rtmidi not installed")
        self._midi = rtmidi.MidiOut()
        self._lock = threading.Lock()
        self.port_name = port_name
        # macOS shows IAC ports as "IAC Driver Bus 1", "IAC Driver Bus 2", …
        # `iac_prefer` is a priority list of substrings — lets callers target
        # a specific bus (e.g. notes on Bus 2, control on Bus 1) and still
        # fall back to any IAC bus.
        iac_prefer = iac_prefer or ["IAC Driver"]
        # Probe available output ports.
        names = self._midi.get_ports()
        chosen_idx: int | None = None
        chosen_label: str | None = None
        if prefer_iac:
            for want in iac_prefer:
                for i, n in enumerate(names):
                    if want in n:
                        chosen_idx = i
                        chosen_label = n
                        break
                if chosen_idx is not None:
                    break
        if chosen_idx is None:
            for i, n in enumerate(names):
                if port_name.lower() in n.lower():
                    chosen_idx = i
                    chosen_label = n
                    break
        if chosen_idx is not None:
            self._midi.open_port(chosen_idx)
            log.info("MIDI: opened existing port %s", chosen_label)
            lbl = chosen_label or ""
            self.opened_kind = "iac" if ("IAC Driver" in lbl or "Bus " in lbl) else "existing"
            self.opened_name = chosen_label or port_name
        else:
            # Fall back to a virtual port (ephemeral — REAPER may need a
            # "Reset all MIDI devices" after each daemon restart).
            self._midi.open_virtual_port(port_name)
            log.info("MIDI: opened virtual port %s", port_name)
            self.opened_kind = "virtual"
            self.opened_name = port_name

    def close(self) -> None:
        with self._lock:
            self._midi.close_port()
            del self._midi

    # MIDI status byte cheat sheet:
    #   0x80 + ch — NoteOff
    #   0x90 + ch — NoteOn
    #   0xB0 + ch — CC
    #   0xE0 + ch — Pitch bend

    def note_on(self, note: int, velocity: int = 100, channel: int = 0) -> None:
        with self._lock:
            self._midi.send_message([0x90 | (channel & 0x0F), note & 0x7F, velocity & 0x7F])

    def note_off(self, note: int, channel: int = 0) -> None:
        with self._lock:
            self._midi.send_message([0x80 | (channel & 0x0F), note & 0x7F, 0])

    def cc(self, controller: int, value: int, channel: int = 0) -> None:
        with self._lock:
            self._midi.send_message([0xB0 | (channel & 0x0F), controller & 0x7F, value & 0x7F])

    def all_notes_off(self, channel: int = 0) -> None:
        """Panic — kills any held notes (CC 123)."""
        self.cc(123, 0, channel)
