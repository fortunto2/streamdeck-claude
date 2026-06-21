"""Generative pattern engine — background voice, tempo-locked to Ableton.

A single Euclidean voice clocked by **Ableton Link** (phase-locked to
Live's tempo and bar grid — enable Link in Live's transport bar). isobar
generates the Euclidean rhythm; the pitch follows a selectable algorithm
(up / down / arp / random) over a selectable scale. MIDI goes out a shared
IAC bus (or virtual port) into Live's armed track.

The engine is a module-level singleton so patterns keep playing while the
deck flips between pages — only the control surface comes and goes.
"""

from __future__ import annotations

import os
import random
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from isobar import PEuclidean
except Exception:  # pragma: no cover
    PEuclidean = None

try:
    import link as _link
except Exception:  # pragma: no cover
    _link = None

try:
    from src.midi_out import MidiOut
except Exception:  # pragma: no cover
    MidiOut = None

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Pitch algorithms over the active scale.
MODES = ["UP", "DOWN", "ARP", "RND"]

# (display name, semitone offsets)
SCALES = [
    ("penta-", [0, 3, 5, 7, 10]),
    ("major", [0, 2, 4, 5, 7, 9, 11]),
    ("minor", [0, 2, 3, 5, 7, 8, 10]),
    ("dorian", [0, 2, 3, 5, 7, 9, 10]),
    ("penta+", [0, 2, 4, 7, 9]),
]

QUANTUM = 4.0  # Link bar length (beats) for phase alignment


def note_name(note: int) -> str:
    return f"{NOTE_NAMES[note % 12]}{note // 12 - 1}"


def _euclid(pulses: int, steps: int) -> list[int]:
    """Euclidean rhythm as a 0/1 list of length `steps` with `pulses` hits."""
    if steps <= 0:
        return []
    if PEuclidean is not None:
        try:
            # isobar patterns are infinite iterators — pull exactly `steps`.
            p = PEuclidean(pulses, steps)
            return [1 if next(p) else 0 for _ in range(steps)]
        except Exception:
            pass
    return [1 if (i * pulses) // steps != ((i - 1) * pulses) // steps else 0
            for i in range(steps)]


class GenEngine:
    """One Euclidean voice, Link-clocked, played out over MIDI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.tempo = 120.0       # read from Link
        self.peers = 0           # Link peers (>0 ⇒ synced to Live)
        self.steps = 8
        self.pulses = 5
        self.root = 48           # C3
        self.channel = 0
        self.gate = 0.5
        self.mode_idx = 0
        self.scale_idx = 0
        self._pattern = _euclid(self.pulses, self.steps)
        self._step = 0
        self._hit = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._midi = None
        self._link = None

    # -- resources (lazy) ----------------------------------------------

    def _ensure_midi(self):
        if self._midi is None and MidiOut is not None:
            try:
                # Notes go to IAC Bus 2 (Live: Track on) so they play the
                # armed track — kept separate from Looper control on Bus 1.
                self._midi = MidiOut(port_name="StreamDeck Gen",
                                     iac_prefer=["IAC Driver Bus 2", "IAC Driver"])
            except Exception:
                self._midi = None
        return self._midi

    def _ensure_link(self):
        if self._link is None and _link is not None:
            try:
                self._link = _link.Link(self.tempo)
                self._link.enabled = True
            except Exception:
                self._link = None
        return self._link

    def midi_kind(self) -> str:
        return getattr(self._midi, "opened_kind", "—") if self._midi else "—"

    # -- params --------------------------------------------------------

    def set_steps(self, n: int) -> None:
        with self.lock:
            self.steps = max(1, min(16, n))
            self.pulses = min(self.pulses, self.steps)
            self._pattern = _euclid(self.pulses, self.steps)

    def set_pulses(self, k: int) -> None:
        with self.lock:
            self.pulses = max(0, min(self.steps, k))
            self._pattern = _euclid(self.pulses, self.steps)

    def nudge_root(self, semitones: int) -> None:
        with self.lock:
            self.root = max(0, min(120, self.root + semitones))

    def cycle_mode(self) -> None:
        with self.lock:
            self.mode_idx = (self.mode_idx + 1) % len(MODES)

    def cycle_scale(self) -> None:
        with self.lock:
            self.scale_idx = (self.scale_idx + 1) % len(SCALES)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running, "tempo": self.tempo, "peers": self.peers,
                "steps": self.steps, "pulses": self.pulses, "root": self.root,
                "step": self._step, "pattern": list(self._pattern),
                "mode": MODES[self.mode_idx], "scale": SCALES[self.scale_idx][0],
            }

    def current_tempo(self) -> tuple[float, int]:
        """Read tempo + peer count from Link on demand (also when stopped)."""
        link = self._ensure_link()
        if link is None:
            return self.tempo, 0
        try:
            state = link.captureSessionState()
            t, p = state.tempo(), link.numPeers()
            with self.lock:
                self.tempo, self.peers = t, p
            return t, p
        except Exception:
            return self.tempo, self.peers

    # -- note choice ---------------------------------------------------

    def _note_for_hit(self, hit: int) -> int:
        scale = SCALES[self.scale_idx][1]
        n = len(scale)
        mode = MODES[self.mode_idx]
        if mode == "DOWN":
            deg = (n - 1) - (hit % n)
            octv = 2 - (hit // n) % 3
        elif mode == "ARP":
            seq = list(range(n)) + list(range(n - 2, 0, -1))
            deg = seq[hit % len(seq)]
            octv = (hit // len(seq)) % 2
        elif mode == "RND":
            deg = random.randrange(n)
            octv = random.randrange(3)
        else:  # UP
            deg = hit % n
            octv = (hit // n) % 3
        return self.root + scale[deg] + 12 * octv

    # -- transport -----------------------------------------------------

    def toggle(self) -> None:
        self.stop() if self.running else self.start()

    def start(self) -> None:
        if self.running:
            return
        self._ensure_midi()
        self._ensure_link()
        self.running = True
        self._stop.clear()
        self._hit = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="gen-engine")
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        m = self._midi
        if m is not None:
            try:
                m.all_notes_off(self.channel)
            except Exception:
                pass

    # -- helpers -------------------------------------------------------

    def _on(self, note: int) -> None:
        if self._midi:
            try:
                self._midi.note_on(note, 100, self.channel)
            except Exception:
                pass

    def _off(self, note: int) -> None:
        if self._midi:
            try:
                self._midi.note_off(note, self.channel)
            except Exception:
                pass

    # -- clock thread (Link phase-locked) ------------------------------

    def _run(self) -> None:
        link = self._link
        if link is None:
            self._run_freewheel()
            return
        clock = link.clock()
        last16 = None
        held: tuple[int, int] | None = None  # (note, off_micros)
        while not self._stop.is_set():
            now = clock.micros()
            state = link.captureSessionState()
            tempo = state.tempo()
            with self.lock:
                self.tempo = tempo
                self.peers = link.numPeers()
            beat = state.beatAtTime(now, QUANTUM)
            sixteenth = int(beat * 4)
            if held is not None and now >= held[1]:
                self._off(held[0])
                held = None
            if sixteenth != last16:
                last16 = sixteenth
                with self.lock:
                    idx = sixteenth % self.steps
                    hit = self._pattern[idx] if idx < len(self._pattern) else 0
                    self._step = idx
                    gate = self.gate
                if hit:
                    note = self._note_for_hit(self._hit)
                    if held is not None:
                        self._off(held[0])
                    self._on(note)
                    dur_us = int(60.0 / max(tempo, 1.0) / 4.0 * gate * 1_000_000)
                    held = (note, now + dur_us)
                    with self.lock:
                        self._hit += 1
            self._stop.wait(0.005)
        if held is not None:
            self._off(held[0])

    def _run_freewheel(self) -> None:
        """Fallback clock if Ableton Link is unavailable (no tempo sync)."""
        step = 0
        while not self._stop.is_set():
            with self.lock:
                step_sec = 60.0 / max(self.tempo, 1.0) / 4.0
                idx = step % self.steps
                hit = self._pattern[idx] if idx < len(self._pattern) else 0
                self._step = idx
                gate = self.gate
            note = self._note_for_hit(self._hit) if hit else None
            if note is not None:
                self._on(note)
                with self.lock:
                    self._hit += 1
            self._stop.wait(step_sec * gate)
            if note is not None:
                self._off(note)
            self._stop.wait(step_sec * (1.0 - gate))
            step += 1


# Module-level singleton — survives page flips.
engine = GenEngine()
