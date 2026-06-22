"""Generative pattern engine — background voice, tempo-locked to Ableton.

A single voice clocked by **Ableton Link** (phase-locked to Live's tempo
and bar grid — enable Link in Live's transport bar). The rhythm is a grid
of per-step probabilities (Euclidean seed, hand-editable, dice/mutate);
the pitch follows a selectable algorithm over a selectable scale. MIDI
goes out IAC Bus 2 (notes) into Live's armed track.

Module-level singleton so patterns keep playing while the deck flips
between pages — only the control surface comes and goes.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from datetime import datetime

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
    from src.midi_out import MidiOut, iac_bus_prefer
except Exception:  # pragma: no cover
    MidiOut = None

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Pitch algorithms over the active scale.
#   UP/DOWN/ARP  — deterministic motion
#   RND/WALK     — randomised (RND = jump, WALK = drifting random walk)
#   FLAT         — steady root note (kick / one-note bass)
#   CHORD        — triad stab on every hit
MODES = ["UP", "DOWN", "ARP", "RND", "WALK", "FLAT", "CHORD", "MARK"]

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
            p = PEuclidean(pulses, steps)
            return [1 if next(p) else 0 for _ in range(steps)]
        except Exception:
            pass
    return [1 if (i * pulses) // steps != ((i - 1) * pulses) // steps else 0
            for i in range(steps)]


# Shared resources — one Link session + one MIDI port for ALL voices, so
# six generators don't spin up six Link instances / six IAC connections.
_shared_link = None
_shared_midi = None
_res_lock = threading.Lock()


def _get_link():
    global _shared_link
    with _res_lock:
        if _shared_link is None and _link is not None:
            try:
                _shared_link = _link.Link(120.0)
                _shared_link.enabled = True
            except Exception:
                _shared_link = None
    return _shared_link


def _get_midi():
    global _shared_midi
    with _res_lock:
        if _shared_midi is None and MidiOut is not None:
            try:
                _shared_midi = MidiOut(port_name="StreamDeck Gen",
                                       iac_prefer=iac_bus_prefer(2))
            except Exception:
                _shared_midi = None
    return _shared_midi


class GenEngine:
    """One voice — per-step probability grid, Link-clocked, played over MIDI."""

    def __init__(self, channel: int = 0, name: str = "A", root: int = 48):
        self.lock = threading.Lock()
        self.name = name
        self.running = False    # clock thread alive (queued or playing)
        self.armed = False      # actually firing notes (sounding)
        self.pending = None     # "start" | "stop" — Link-quantised launch
        self.tempo = 120.0
        self.peers = 0
        self.steps = 8
        self.pulses = 5
        self.root = root
        self.channel = channel
        self.gate = 0.5
        self.fill = False       # momentary: every step fires (16th roll)
        self.mode_idx = 0
        self.scale_idx = 0
        # Per-step trigger probability in [0,1]; 1=hit, 0.5=ghost, 0=rest.
        self._pattern: list[float] = [float(v) for v in _euclid(self.pulses, self.steps)]
        # Per-step ratchet: step index -> sub-hit count (2 or 3). Absent = 1.
        self._ratchet: dict[int, int] = {}
        self._step = 0
        self._hit = 0
        self._fired = False     # did the current step actually trigger a note
        self._walk = 4          # WALK-mode degree position
        self._mk = 4            # MARK-mode (Markov) degree position
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._midi = None
        self._link = None

    # -- resources (lazy) ----------------------------------------------

    def _ensure_midi(self):
        # Shared port → IAC Bus 2 (Live: Track on); each voice sends on its
        # own channel. Looper control is on Bus 1, so they never collide.
        if self._midi is None:
            self._midi = _get_midi()
        return self._midi

    def _ensure_link(self):
        if self._link is None:
            self._link = _get_link()
        return self._link

    def current_tempo(self) -> tuple[float, int]:
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

    # -- pattern (per-step probability) --------------------------------

    def set_steps(self, n: int) -> None:
        with self.lock:
            self.steps = max(1, min(16, n))
            self.pulses = min(self.pulses, self.steps)
            self._pattern = [float(v) for v in _euclid(self.pulses, self.steps)]
            self._ratchet = {i: r for i, r in self._ratchet.items() if i < self.steps}

    def set_pulses(self, k: int) -> None:
        with self.lock:
            self.pulses = max(0, min(self.steps, k))
            self._pattern = [float(v) for v in _euclid(self.pulses, self.steps)]

    def tap_step(self, i: int) -> None:
        """Short tap — plain on/off, so you can place and clear hits fast
        without cycling through ghost/ratchet states."""
        with self.lock:
            if not (0 <= i < len(self._pattern)):
                return
            self._pattern[i] = 0.0 if self._pattern[i] > 0 else 1.0
            self._ratchet.pop(i, None)

    def hold_step(self, i: int) -> None:
        """Long press — dial up intensity on the cell (never turns it off):
        hit → ghost(50%) → roll×2 → roll×3 → hit. The roll states are ratchets
        (the step retriggers 2/3 times within its 16th)."""
        with self.lock:
            if not (0 <= i < len(self._pattern)):
                return
            p = self._pattern[i]
            r = self._ratchet.get(i, 1)
            if p <= 0 or (p >= 1.0 and r == 1):   # off/hit → ghost
                self._pattern[i] = 0.5
                self._ratchet.pop(i, None)
            elif 0 < p < 1.0:                      # ghost → roll×2
                self._pattern[i] = 1.0
                self._ratchet[i] = 2
            elif r == 2:                           # roll×2 → roll×3
                self._ratchet[i] = 3
            else:                                  # roll×3 → hit
                self._pattern[i] = 1.0
                self._ratchet.pop(i, None)

    def randomize(self) -> None:
        """Dice — reroll the rhythm, same hit count, random placement."""
        with self.lock:
            n = self.steps
            k = max(1, min(self.pulses, n))
            pat = [0.0] * n
            for i in random.sample(range(n), k):
                pat[i] = 1.0
            self._pattern = pat
            self._ratchet.clear()       # placement rerolled — drop ratchets

    def mutate(self) -> None:
        """Nudge one random step to a *different* state — gradual evolution."""
        with self.lock:
            if self._pattern:
                i = random.randrange(len(self._pattern))
                cur = self._pattern[i]
                self._pattern[i] = random.choice([v for v in (0.0, 0.5, 1.0) if v != cur])

    def clear_pattern(self) -> None:
        with self.lock:
            self._pattern = [0.0] * self.steps
            self._ratchet.clear()

    def rotate(self, d: int) -> None:
        """Shift the whole pattern around the circle (instant variation)."""
        with self.lock:
            n = len(self._pattern)
            if n:
                d %= n
                self._pattern = self._pattern[-d:] + self._pattern[:-d]
                self._ratchet = {(i + d) % n: r for i, r in self._ratchet.items()}

    def cycle_gate(self) -> None:
        """Cycle note length: short → med → long → HOLD (legato/sustain)."""
        with self.lock:
            steps = [0.25, 0.5, 0.9, 1.0]
            cur = min(steps, key=lambda g: abs(g - self.gate))
            self.gate = steps[(steps.index(cur) + 1) % len(steps)]

    def set_fill(self, on: bool) -> None:
        self.fill = on

    def nudge_root(self, semitones: int) -> None:
        with self.lock:
            self.root = max(0, min(120, self.root + semitones))

    def cycle_pulses(self) -> None:
        """Tap-cycle euclid density 0..steps (wraps), regenerating the grid."""
        with self.lock:
            self.pulses = (self.pulses + 1) % (self.steps + 1)
            self._pattern = [float(v) for v in _euclid(self.pulses, self.steps)]

    def cycle_mode(self) -> None:
        with self.lock:
            self.mode_idx = (self.mode_idx + 1) % len(MODES)
            self._walk = len(SCALES[self.scale_idx][1])  # reset walk mid-range

    def cycle_scale(self) -> None:
        with self.lock:
            self.scale_idx = (self.scale_idx + 1) % len(SCALES)

    def step_prob(self, global_step: int) -> float:
        """Probability at a global 16th index (for the drum machine to read
        this voice's rhythm). Loops over the voice's own step count."""
        with self.lock:
            n = len(self._pattern)
            return self._pattern[global_step % n] if n else 0.0

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running, "armed": self.armed, "pending": self.pending,
                "tempo": self.tempo, "peers": self.peers,
                "steps": self.steps, "pulses": self.pulses, "root": self.root,
                "step": self._step, "pattern": list(self._pattern), "fired": self._fired,
                "mode": MODES[self.mode_idx], "scale": SCALES[self.scale_idx][0],
                "gate": self.gate, "fill": self.fill, "ratchet": dict(self._ratchet),
            }

    # -- save / restore ------------------------------------------------

    def get_state(self) -> dict:
        with self.lock:
            return {"steps": self.steps, "pulses": self.pulses, "root": self.root,
                    "gate": self.gate, "mode_idx": self.mode_idx,
                    "scale_idx": self.scale_idx, "pattern": list(self._pattern),
                    "ratchet": {str(i): r for i, r in self._ratchet.items()}}

    def set_state(self, s: dict) -> None:
        with self.lock:
            self.steps = int(s.get("steps", self.steps))
            self.pulses = int(s.get("pulses", self.pulses))
            self.root = int(s.get("root", self.root))
            self.gate = float(s.get("gate", self.gate))
            self.mode_idx = int(s.get("mode_idx", self.mode_idx)) % len(MODES)
            self.scale_idx = int(s.get("scale_idx", self.scale_idx)) % len(SCALES)
            pat = s.get("pattern")
            if isinstance(pat, list):
                self._pattern = [float(x) for x in pat]
            rat = s.get("ratchet")
            if isinstance(rat, dict):
                self._ratchet = {int(i): int(r) for i, r in rat.items()
                                 if int(r) > 1 and int(i) < self.steps}

    # -- note choice ---------------------------------------------------

    def _notes_for_hit(self, hit: int) -> list[int]:
        scale = SCALES[self.scale_idx][1]
        n = len(scale)
        mode = MODES[self.mode_idx]

        def at(deg, octv):
            return self.root + scale[deg % n] + 12 * octv

        if mode == "FLAT":
            return [self.root]
        if mode == "CHORD":
            return [at(0, 0), at(2, 0), at(4, 0)]
        if mode == "WALK":
            self._walk = max(0, min(n * 3 - 1, self._walk + random.choice([-1, -1, 0, 1, 1])))
            return [self.root + scale[self._walk % n] + 12 * (self._walk // n)]
        if mode == "DOWN":
            return [at((n - 1) - (hit % n), 2 - (hit // n) % 3)]
        if mode == "ARP":
            seq = list(range(n)) + list(range(n - 2, 0, -1))
            return [at(seq[hit % len(seq)], (hit // len(seq)) % 2)]
        if mode == "RND":
            return [at(random.randrange(n), random.randrange(3))]
        if mode == "MARK":
            # First-order Markov walk over scale degrees across 3 octaves.
            # Next note depends only on the current one: small steps are likely,
            # leaps rare, with a pull toward stable degrees (tonic/3rd/5th) and
            # the mid register — so the line wanders but keeps resolving.
            span = n * 3
            cur = max(0, min(span - 1, self._mk))
            centre = span // 2
            picks, weights = [], []
            for d, w in ((-3, 1), (-2, 2), (-1, 6), (0, 2), (1, 6), (2, 2), (3, 1)):
                nxt = cur + d
                if not (0 <= nxt < span):
                    continue
                wt = w
                if (nxt % n) in (0, 2, 4):                 # land on stable degrees
                    wt += 2
                if (cur > centre and d < 0) or (cur < centre and d > 0):
                    wt += 3                                # pull back toward centre
                elif (cur > centre and d > 0) or (cur < centre and d < 0):
                    wt = max(1, wt - 1)                    # resist drifting away
                picks.append(nxt)
                weights.append(wt)
            self._mk = random.choices(picks, weights=weights)[0] if picks else cur
            # -1 octave offset so the centre of the span sits in the root octave
            return [self.root + scale[self._mk % n] + 12 * (self._mk // n - 1)]
        return [at(hit % n, (hit // n) % 3)]  # UP

    # -- transport -----------------------------------------------------

    def toggle(self) -> None:
        self.stop() if self.running else self.start()

    def start(self) -> None:
        if self.running:
            return
        self._ensure_midi()
        self._ensure_link()
        self.running = True
        self.armed = False
        self.pending = "start"     # arms on the next bar (Link-quantised)
        self._stop.clear()
        self._hit = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="gen-engine")
        self._thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        with self.lock:
            armed = self.armed
        if armed:
            with self.lock:
                self.pending = "stop"   # finish the bar, stop on the boundary
        else:
            self._stop.set()            # queued but not sounding yet — cancel now

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

    def _fires(self, prob: float) -> bool:
        if self.fill:
            return True   # roll every 16th while FILL is held
        return prob > 0 and (prob >= 1.0 or random.random() < prob)

    # -- clock thread (Link phase-locked) ------------------------------

    def _run(self) -> None:
        link = self._ensure_link()
        if link is None:
            self._run_freewheel()
            return
        clock = link.clock()
        qb = int(QUANTUM * 4)   # sixteenths per bar (launch quantum)
        last16 = None
        sched: list[list] = []  # each: [notes, on_us, off_us, started]
        ending = False
        while not self._stop.is_set() and not ending:
            now = clock.micros()
            state = link.captureSessionState()
            tempo = state.tempo()
            with self.lock:
                self.tempo = tempo
                self.peers = link.numPeers()
            sixteenth = int(state.beatAtTime(now, QUANTUM) * 4)
            # service scheduled (sub-)hits: start due notes, release expired ones
            still = []
            for it in sched:
                if not it[3] and now >= it[1]:
                    for nt in it[0]:
                        self._on(nt)
                    it[3] = True
                if it[3] and now >= it[2]:
                    for nt in it[0]:
                        self._off(nt)
                    continue
                still.append(it)
            sched = still
            if sixteenth != last16:
                last16 = sixteenth
                bar = (sixteenth % qb == 0)   # downbeat → arm / disarm here
                with self.lock:
                    if bar and self.pending == "start":
                        self.pending = None
                        self.armed = True
                        self._hit = 0
                    elif bar and self.pending == "stop":
                        self.pending = None
                        self.armed = False
                        ending = True
                    armed = self.armed
                    idx = sixteenth % self.steps
                    prob = self._pattern[idx] if idx < len(self._pattern) else 0.0
                    ratchet = self._ratchet.get(idx, 1)
                    self._step = idx
                    gate = self.gate
                if not ending and armed:
                    fires = self._fires(prob)
                    with self.lock:
                        self._fired = fires
                    if fires:
                        notes = self._notes_for_hit(self._hit)
                        for it in sched:               # cut anything still ringing
                            if it[3]:
                                for nt in it[0]:
                                    self._off(nt)
                        sched = []
                        step_us = 60.0 / max(tempo, 1.0) / 4.0 * 1_000_000
                        r = max(1, ratchet)
                        if r == 1:
                            for nt in notes:
                                self._on(nt)
                            off_us = now + (3_600_000_000 if gate >= 1.0
                                            else int(step_us * gate))
                            sched.append([notes, now, off_us, True])
                        else:                          # ratchet — r staccato sub-hits
                            sub = step_us / r
                            dur = int(sub * 0.6)
                            for k in range(r):
                                on_us = now + int(k * sub)
                                if k == 0:
                                    for nt in notes:
                                        self._on(nt)
                                sched.append([notes, on_us, on_us + dur, k == 0])
                        with self.lock:
                            self._hit += 1
                else:
                    with self.lock:
                        self._fired = False
            self._stop.wait(0.004)
        for it in sched:
            if it[3]:
                for nt in it[0]:
                    self._off(nt)
        with self.lock:
            self.running = False
            self.armed = False
            self.pending = None
        if self._midi is not None:
            try:
                self._midi.all_notes_off(self.channel)
            except Exception:
                pass

    def _run_freewheel(self) -> None:
        """Fallback clock if Ableton Link is unavailable (no tempo sync, no
        bar quantisation — arms immediately, stops on the next step)."""
        with self.lock:
            self.armed = True
            self.pending = None
        step = 0
        while not self._stop.is_set():
            with self.lock:
                if self.pending == "stop":
                    break
                step_sec = 60.0 / max(self.tempo, 1.0) / 4.0
                idx = step % self.steps
                prob = self._pattern[idx] if idx < len(self._pattern) else 0.0
                self._step = idx
                gate = self.gate
            fires = self._fires(prob)
            with self.lock:
                self._fired = fires
            notes = self._notes_for_hit(self._hit) if fires else []
            for nt in notes:
                self._on(nt)
            if notes:
                with self.lock:
                    self._hit += 1
            self._stop.wait(step_sec * gate)
            for nt in notes:
                self._off(nt)
            self._stop.wait(step_sec * (1.0 - gate))
            step += 1
        with self.lock:
            self.running = False
            self.armed = False
            self.pending = None
        if self._midi is not None:
            try:
                self._midi.all_notes_off(self.channel)
            except Exception:
                pass


# Voices — independent generators, each on its own MIDI channel, all
# phase-locked to the same Ableton Link session. Survive page flips.
VOICES = {
    "A": GenEngine(channel=0, name="A", root=48),   # melody
    "B": GenEngine(channel=1, name="B", root=36),   # bass
    "C": GenEngine(channel=2, name="C", root=60),   # high
    "D": GenEngine(channel=3, name="D", root=48),   # chord
    "E": GenEngine(channel=4, name="E", root=48),   # walk
    "F": GenEngine(channel=5, name="F", root=55),   # arp
}
VOICES["B"].mode_idx = MODES.index("FLAT")          # bass = steady root
VOICES["D"].mode_idx = MODES.index("CHORD")
VOICES["E"].mode_idx = MODES.index("WALK")
VOICES["F"].mode_idx = MODES.index("ARP")
# Distinct default rhythms so the voices (and SRC-linked drum lanes) differ.
VOICES["B"].set_steps(16); VOICES["B"].set_pulses(4)   # four-on-floor
VOICES["C"].set_steps(8);  VOICES["C"].set_pulses(3)   # tresillo
VOICES["D"].set_steps(16); VOICES["D"].set_pulses(7)
VOICES["E"].set_steps(16); VOICES["E"].set_pulses(5)
VOICES["F"].set_steps(8);  VOICES["F"].set_pulses(2)
VOICE_KEYS = ["A", "B", "C", "D", "E", "F"]
engine = VOICES["A"]  # back-compat default


def any_playing() -> bool:
    return any(v.running for v in VOICES.values())


def start_all() -> None:
    for v in VOICES.values():
        v.start()


def stop_all() -> None:
    for v in VOICES.values():
        v.stop()


# ── Tempo control via Link (tap-tempo + ×2 / ÷2) ─────────────────────

_tap_times: list[float] = []


def set_link_tempo(bpm: float) -> None:
    link = _get_link()
    if link is None:
        return
    try:
        bpm = max(20.0, min(300.0, float(bpm)))
        state = link.captureSessionState()
        state.setTempo(bpm, link.clock().micros())
        link.commitSessionState(state)
    except Exception:
        pass


def tap_tempo() -> float | None:
    """Register a tap and set the Link tempo from recent tap intervals.

    Smoothed so it doesn't lurch: needs ≥3 taps before it commits anything,
    then locks to the *median of the last 3 intervals* (a single off-beat tap
    can't yank the tempo) and rounds to a whole BPM. Keep tapping to home in;
    a >2s gap starts a fresh count."""
    global _tap_times
    now = time.monotonic()
    if _tap_times and now - _tap_times[-1] > 2.0:
        _tap_times = []
    _tap_times.append(now)
    _tap_times = _tap_times[-5:]
    gaps = [_tap_times[i + 1] - _tap_times[i] for i in range(len(_tap_times) - 1)]
    if len(gaps) < 2:                       # wait for the 3rd tap — no early jump
        return None
    recent = sorted(gaps[-3:])              # median of the last 3 rejects outliers
    mid = recent[len(recent) // 2] if len(recent) % 2 else \
        (recent[len(recent) // 2 - 1] + recent[len(recent) // 2]) / 2
    if mid > 0:
        bpm = max(20.0, min(300.0, round(60.0 / mid)))
        set_link_tempo(bpm)
        return bpm
    return None


def mult_tempo(factor: float) -> None:
    """Multiply the current Link tempo (×2 / ÷2 buttons)."""
    link = _get_link()
    if link is None:
        return
    try:
        set_link_tempo(link.captureSessionState().tempo() * factor)
    except Exception:
        pass


# ── Presets / snapshots + last-state restore ─────────────────────────

_GEN_DIR = os.path.expanduser("~/.streamdeck-gen")
_PRESET_DIR = os.path.join(_GEN_DIR, "presets")
_LAST_FILE = os.path.join(_GEN_DIR, "last.json")
_NAME_WORDS = ["nova", "luna", "flux", "drift", "pulse", "echo", "comet",
               "ember", "haze", "orbit", "prism", "quartz", "raven", "tide", "void"]


# Extra state providers (e.g. the drum machine) register here so presets and
# crash-restore cover them too — without isobar_engine importing them (which
# would be circular; they import us). Each is (get_state, set_state).
_EXTRA_PROVIDERS: dict[str, tuple] = {}


def register_extra(name: str, get_fn, set_fn) -> None:
    _EXTRA_PROVIDERS[name] = (get_fn, set_fn)


def _capture_all() -> dict:
    voices = {k: v.get_state() for k, v in VOICES.items()}
    extras = {}
    for name, (get_fn, _set) in _EXTRA_PROVIDERS.items():
        try:
            extras[name] = get_fn()
        except Exception:
            pass
    return {"voices": voices, "extras": extras}


def _apply_all(data: dict) -> None:
    if not isinstance(data, dict):
        return
    voices = data.get("voices", data)        # back-compat: old flat {A:{...}} format
    for k, st in (voices or {}).items():
        if k in VOICES and isinstance(st, dict):
            VOICES[k].set_state(st)
    for name, st in (data.get("extras") or {}).items():
        prov = _EXTRA_PROVIDERS.get(name)
        if prov and st is not None:
            try:
                prov[1](st)
            except Exception:
                pass


def _load_last_extra(name: str) -> None:
    """Restore one extra provider's slice of last.json — called after the
    provider registers (it isn't available yet when _load_last() runs at import)."""
    try:
        with open(_LAST_FILE) as f:
            st = (json.load(f).get("extras") or {}).get(name)
        prov = _EXTRA_PROVIDERS.get(name)
        if st is not None and prov:
            prov[1](st)
    except Exception:
        pass


def save_preset(name: str | None = None) -> str:
    """Snapshot all voices to a named preset (auto-named by word + date)."""
    try:
        os.makedirs(_PRESET_DIR, exist_ok=True)
    except Exception:
        pass
    if not name:
        name = f"{random.choice(_NAME_WORDS)}-{datetime.now():%m%d-%H%M%S}"
    try:
        with open(os.path.join(_PRESET_DIR, name + ".json"), "w") as f:
            json.dump(_capture_all(), f)
    except Exception:
        pass
    return name


def list_presets() -> list[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(_PRESET_DIR) if f.endswith(".json"))
    except Exception:
        return []


def load_preset(name: str) -> bool:
    try:
        with open(os.path.join(_PRESET_DIR, name + ".json")) as f:
            _apply_all(json.load(f))
        return True
    except Exception:
        return False


def delete_preset(name: str) -> bool:
    try:
        os.remove(os.path.join(_PRESET_DIR, name + ".json"))
        return True
    except Exception:
        return False


def save_last() -> None:
    try:
        os.makedirs(_GEN_DIR, exist_ok=True)
        with open(_LAST_FILE, "w") as f:
            json.dump(_capture_all(), f)
    except Exception:
        pass


def _load_last() -> None:
    try:
        with open(_LAST_FILE) as f:
            _apply_all(json.load(f))
    except Exception:
        pass


# Restore the last session's patterns on startup (voices stay stopped —
# the user presses play), and keep autosaving it so a crash loses nothing.
_load_last()


def _autosave_loop() -> None:
    while True:
        time.sleep(8)
        save_last()


threading.Thread(target=_autosave_loop, daemon=True, name="gen-autosave").start()
