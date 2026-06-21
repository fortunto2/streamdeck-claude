"""808/909-style drum machine — a 16-step sequencer, Link-synced.

Independent of the generator voices but shares their Link session (locks to
the same bar grid) and MIDI port (IAC Bus 2). Plays GM-percussion notes on
MIDI channel 10 — point a Live drum rack / 808 kit at Bus 2, channel 10.

Background singleton: keeps playing while the deck flips pages. Launch is
bar-quantised (queued → starts/stops on the next Link bar), like the voices.
"""

from __future__ import annotations

import random
import threading

from isobar_engine import _get_link, _get_midi, QUANTUM, VOICES

N_STEPS = 16
CH = 9   # GM drums = MIDI channel 10

# A drum lane can be driven by its own step pattern, or follow a GEN voice's
# rhythm (so the generator's euclidean/evolving pattern plays a drum sound).
SRC_CYCLE = [None, "A", "B", "C", "D", "E", "F"]

# Built-in grooves (lane → steps). Lanes: 0 Kick 1 Rim 2 Snare 3 Clap
# 4 LoConga 5 MidConga 6 HatC 7 HiConga 8 LoTom 9 MidTom 10 HatO 11 HiTom
# 12 Maracas 13 Cymbal 14 CowBell 15 Claves.
BEATS = [
    ("house", {     # club kick + clap, "tss" open hats, shaker drive, conga color
        0: [0, 4, 8, 12], 3: [4, 12], 6: [0, 4, 8, 12], 10: [2, 6, 10, 14],
        12: list(range(16)), 5: [7, 15]}),
    ("4floor", {    # relentless tech: 8th hats, rim ghost, claves, lift
        0: [0, 4, 8, 12], 2: [4, 12], 6: [0, 2, 4, 6, 8, 10, 12],
        1: [8], 10: [14], 15: [2, 10]}),
    ("boombap", {   # swung kick, backbeat, ghost rim, conga, open-hat lift
        0: [0, 3, 8, 11], 2: [4, 12], 1: [7, 15], 6: [0, 2, 4, 6, 8, 10, 12],
        10: [14], 4: [6]}),
    ("trap", {      # sparse 808 kick, hat rolls, cowbell, maracas
        0: [0, 7, 10], 2: [8], 6: [0, 2, 4, 6, 8, 9, 10, 12, 13, 14],
        10: [15], 14: [3, 11], 12: [1, 5, 9, 13]}),
    ("techno", {    # 4-floor, open offbeats + 8th closed, cowbell, rim
        0: [0, 4, 8, 12], 6: [0, 4, 8, 12], 10: [2, 6, 10, 14],
        1: [8], 14: [7, 15], 3: [12]}),
    ("kino", {      # post-punk drive: 16th hats, cowbell, tom fill, shaker
        0: [0, 4, 8, 12], 2: [4, 12], 6: list(range(16)), 14: [2, 10],
        9: [14, 15], 12: [0, 4, 8, 12]}),
    ("joydiv", {    # iconic tom-led groove across all three toms + cymbal
        0: [0, 8], 2: [4, 12], 9: [2, 6, 10, 14], 8: [3, 11],
        11: [7], 13: [0]}),
    ("breaks", {    # chopped break: kick/snare hits, hats, ghost rim, maracas
        0: [0, 10], 2: [4, 7, 12], 6: [0, 4, 6, 8, 10, 12],
        10: [2, 14], 1: [9, 15], 12: [1, 5, 11]}),
    ("funk", {      # syncopated kick, ghost rim, 16th hats, cowbell anchor
        0: [0, 6, 10], 2: [4, 12], 1: [2, 7, 14],
        6: [0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15],
        10: [3, 11], 14: [0, 8]}),
    ("electro", {   # Kraftwerk: cowbell 8ths, claves, clap-doubled snare
        0: [0, 8], 2: [4, 12], 3: [4, 12], 14: list(range(0, 16, 2)),
        15: [2, 6, 10, 14], 10: [7, 15]}),
]

# All 16 Drum-Rack lanes (Ableton 4×4: Bass Drum = C1 = 36, chromatic up).
# (name, GM note) — colour/grouping is applied by the control surface.
DRUMS = [
    ("Kick", 36), ("Rim", 37), ("Snare", 38), ("Clap", 39),
    ("LoConga", 40), ("MidConga", 41), ("HatC", 42), ("HiConga", 43),
    ("LoTom", 44), ("MidTom", 45), ("HatO", 46), ("HiTom", 47),
    ("Maracas", 48), ("Cymbal", 49), ("CowBell", 50), ("Claves", 51),
]


class DrumMachine:

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.armed = False
        self.pending = None
        self.channel = CH
        self._step = 0
        self.patterns = [[0] * N_STEPS for _ in DRUMS]
        self.source = [None] * len(DRUMS)   # per-lane: None or a GEN voice key
        # A simple default groove so it's immediately useful.
        for s in (0, 4, 8, 12):
            self.patterns[0][s] = 1                 # Kick (lane 0) — four on the floor
        for s in (4, 12):
            self.patterns[2][s] = 1                 # Snare (lane 2) — backbeat
        for s in range(0, N_STEPS, 2):
            self.patterns[6][s] = 1                 # Closed Hat (lane 6) — 8ths
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._midi = None

    def _ensure_midi(self):
        if self._midi is None:
            self._midi = _get_midi()
        return self._midi

    # -- pattern -------------------------------------------------------

    def toggle_step(self, voice: int, step: int) -> None:
        with self.lock:
            if 0 <= voice < len(DRUMS) and 0 <= step < N_STEPS:
                self.patterns[voice][step] ^= 1

    def clear_voice(self, voice: int) -> None:
        with self.lock:
            if 0 <= voice < len(DRUMS):
                self.patterns[voice] = [0] * N_STEPS

    def clear_all(self) -> None:
        with self.lock:
            self.patterns = [[0] * N_STEPS for _ in DRUMS]
            self.source = [None] * len(DRUMS)

    def cycle_source(self, lane: int) -> None:
        """Link a lane to a GEN voice's rhythm (off → A … F → off)."""
        with self.lock:
            if 0 <= lane < len(self.source):
                cur = self.source[lane]
                i = SRC_CYCLE.index(cur) if cur in SRC_CYCLE else 0
                self.source[lane] = SRC_CYCLE[(i + 1) % len(SRC_CYCLE)]

    def load_beat(self, mapping: dict) -> None:
        with self.lock:
            self.patterns = [[0] * N_STEPS for _ in DRUMS]
            self.source = [None] * len(DRUMS)
            for lane, steps in mapping.items():
                if 0 <= lane < len(DRUMS):
                    for s in steps:
                        if 0 <= s < N_STEPS:
                            self.patterns[lane][s] = 1

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "armed": self.armed, "pending": self.pending,
                    "step": self._step, "patterns": [list(p) for p in self.patterns],
                    "source": list(self.source)}

    # -- transport (bar-quantised, same as the voices) -----------------

    def toggle(self) -> None:
        self.stop() if self.running else self.start()

    def start(self) -> None:
        if self.running:
            return
        self._ensure_midi()
        self.running = True
        self.armed = False
        self.pending = "start"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="drum-machine")
        self._thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        with self.lock:
            armed = self.armed
        if armed:
            with self.lock:
                self.pending = "stop"
        else:
            self._stop.set()

    def _on(self, note: int) -> None:
        if self._midi:
            try:
                self._midi.note_on(note, 110, self.channel)
            except Exception:
                pass

    def _off(self, note: int) -> None:
        if self._midi:
            try:
                self._midi.note_off(note, self.channel)
            except Exception:
                pass

    def _run(self) -> None:
        link = _get_link()
        if link is None:
            with self.lock:
                self.running = False
            return
        clock = link.clock()
        qb = int(QUANTUM * 4)
        last16 = None
        held: list[tuple[int, int]] = []
        ending = False
        while not self._stop.is_set() and not ending:
            now = clock.micros()
            sixteenth = int(link.captureSessionState().beatAtTime(now, QUANTUM) * 4)
            if held:
                still = []
                for note, off in held:
                    if now >= off:
                        self._off(note)
                    else:
                        still.append((note, off))
                held = still
            if sixteenth != last16:
                last16 = sixteenth
                bar = (sixteenth % qb == 0)
                with self.lock:
                    if bar and self.pending == "start":
                        self.pending = None
                        self.armed = True
                    elif bar and self.pending == "stop":
                        self.pending = None
                        self.armed = False
                        ending = True
                    armed = self.armed
                    step = sixteenth % N_STEPS
                    self._step = step
                    lane_state = [(self.patterns[i][step], self.source[i]) for i in range(len(DRUMS))]
                if not ending and armed:
                    hits = []
                    for i, (on_local, src) in enumerate(lane_state):
                        if src is not None:   # follow a GEN voice's rhythm, ghosts included
                            sv = VOICES.get(src)
                            if sv is not None:
                                p = sv.step_prob(sixteenth)
                                if p > 0 and (p >= 1.0 or random.random() < p):
                                    hits.append(DRUMS[i][1])
                        elif on_local:
                            hits.append(DRUMS[i][1])
                    for note in hits:
                        self._on(note)
                        held.append((note, now + 25_000))   # 25 ms one-shot
            self._stop.wait(0.004)
        for note, _ in held:
            self._off(note)
        with self.lock:
            self.running = False
            self.armed = False
            self.pending = None
        if self._midi is not None:
            try:
                self._midi.all_notes_off(self.channel)
            except Exception:
                pass


machine = DrumMachine()
