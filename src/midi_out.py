"""Virtual MIDI output port — Stream Deck as a MIDI controller.

Opens a virtual MIDI output that any DAW can subscribe to (REAPER →
Preferences → MIDI Devices → enable the "StreamDeck" virtual port).

Used by:
- the MIDI page (32 buttons mapped to notes / CCs)
- the Drum page (16-step sequencer; on each clock tick it emits
  NoteOn events for the steps that are armed)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Iterable

try:
    import rtmidi
except ImportError:  # pragma: no cover — pinned in pyproject
    rtmidi = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

PORT_NAME = "StreamDeck"


class MidiOut:
    """Wraps a virtual MIDI output port. Thread-safe note send."""

    def __init__(self, port_name: str = PORT_NAME):
        if rtmidi is None:
            raise RuntimeError("python-rtmidi not installed")
        self._midi = rtmidi.MidiOut()
        self._midi.open_virtual_port(port_name)
        self._lock = threading.Lock()
        log.info("MIDI virtual port opened: %s", port_name)

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
