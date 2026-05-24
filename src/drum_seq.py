"""16-step drum sequencer state — drives the Drum Stream Deck page.

Layout on Stream Deck XL (32 buttons total):
- 16 step buttons (4×4 grid of the lower-right quadrant by convention)
- 6 voice select buttons (Kick / Snare / HHc / HHo / Clap / Cowbell —
  matches SuperDuper Drum's MIDI note layout)
- Transport (play/stop) + tempo + clear-pattern

Clock runs on a background thread. On each step, fires NoteOn for
every armed step on every voice. Notes go out through the shared
`MidiOut` so SuperDuper Drum (or any drum plugin) plays them.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

from src.midi_out import MidiOut

log = logging.getLogger(__name__)


# MIDI notes for SuperDuper Drum's white-keys-C-A layout (C3 = MIDI 48).
VOICE_NOTES: dict[str, int] = {
    "kick": 48,
    "snare": 50,
    "hhc": 52,
    "hho": 53,
    "clap": 55,
    "cowbell": 57,
}
VOICES = tuple(VOICE_NOTES.keys())
N_STEPS = 16


@dataclass
class DrumPattern:
    """Per-voice step armed-flags grid: pattern[voice][step] = bool."""

    pattern: dict[str, list[bool]] = field(
        default_factory=lambda: {v: [False] * N_STEPS for v in VOICES}
    )

    def toggle(self, voice: str, step: int) -> None:
        if voice in self.pattern and 0 <= step < N_STEPS:
            self.pattern[voice][step] = not self.pattern[voice][step]

    def is_armed(self, voice: str, step: int) -> bool:
        return self.pattern.get(voice, [False] * N_STEPS)[step]

    def clear(self) -> None:
        for v in VOICES:
            self.pattern[v] = [False] * N_STEPS

    def clear_voice(self, voice: str) -> None:
        if voice in self.pattern:
            self.pattern[voice] = [False] * N_STEPS


class DrumSequencer:
    """Background-thread sequencer. Fires NoteOn on armed steps."""

    def __init__(self, midi: MidiOut, channel: int = 9):
        self.midi = midi
        self.channel = channel  # GM convention: MIDI channel 10 (= 9 zero-indexed)
        self.pattern = DrumPattern()
        self.bpm = 120.0
        self.selected_voice: str = "kick"
        self._running = False
        self._step = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._step_callback = None  # set by daemon to redraw step LEDs

    def set_step_callback(self, fn):
        """Callback fired on every step tick with current step index."""
        self._step_callback = fn

    def toggle_step(self, voice: str, step: int) -> None:
        self.pattern.toggle(voice, step)

    def select_voice(self, voice: str) -> None:
        if voice in VOICES:
            self.selected_voice = voice

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._step = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="drum-seq")
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        # Panic — kill any held notes.
        self.midi.all_notes_off(self.channel)

    def is_running(self) -> bool:
        return self._running

    def clear(self) -> None:
        self.pattern.clear()

    def _run(self) -> None:
        """16-step loop at `self.bpm` (16th notes)."""
        while not self._stop_event.is_set():
            # 16th note interval at BPM.
            step_sec = 60.0 / max(self.bpm, 1.0) / 4.0
            voices_to_play = [
                (v, n) for v, n in VOICE_NOTES.items()
                if self.pattern.is_armed(v, self._step)
            ]
            for voice, note in voices_to_play:
                try:
                    self.midi.note_on(note, velocity=110, channel=self.channel)
                except Exception as e:
                    log.warning("midi note_on failed: %s", e)
            # Short note off after a small offset so the synth has time to
            # trigger the envelope; drum voices are one-shot anyway.
            time.sleep(min(step_sec * 0.5, 0.05))
            for _, note in voices_to_play:
                try:
                    self.midi.note_off(note, channel=self.channel)
                except Exception:
                    pass
            # Sleep the remainder of the step.
            time.sleep(max(0.0, step_sec * 0.5))
            self._step = (self._step + 1) % N_STEPS
            if self._step_callback is not None:
                try:
                    self._step_callback(self._step)
                except Exception as e:
                    log.warning("step callback failed: %s", e)
