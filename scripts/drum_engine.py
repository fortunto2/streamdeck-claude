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
        self.ratchet: dict[tuple[int, int], int] = {}   # (lane, step) -> 2/3 sub-hits
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

    def tap_step(self, voice: int, step: int) -> None:
        """Short tap — plain on/off, so hits can be placed and cleared fast."""
        with self.lock:
            if not (0 <= voice < len(DRUMS) and 0 <= step < N_STEPS):
                return
            self.patterns[voice][step] = 0 if self.patterns[voice][step] else 1
            self.ratchet.pop((voice, step), None)

    def hold_step(self, voice: int, step: int) -> None:
        """Long press — dial up the ratchet (never turns it off):
        hit → roll×2 → roll×3 → hit. Long-pressing an empty step makes a ×2 roll."""
        with self.lock:
            if not (0 <= voice < len(DRUMS) and 0 <= step < N_STEPS):
                return
            key = (voice, step)
            r = self.ratchet.get(key, 1)
            if not self.patterns[voice][step]:      # off → roll×2
                self.patterns[voice][step] = 1
                self.ratchet[key] = 2
            elif r == 1:                            # hit → roll×2
                self.ratchet[key] = 2
            elif r == 2:                            # roll×2 → roll×3
                self.ratchet[key] = 3
            else:                                   # roll×3 → hit
                self.ratchet.pop(key, None)

    def clear_voice(self, voice: int) -> None:
        with self.lock:
            if 0 <= voice < len(DRUMS):
                self.patterns[voice] = [0] * N_STEPS
                self.ratchet = {k: v for k, v in self.ratchet.items() if k[0] != voice}

    def clear_all(self) -> None:
        with self.lock:
            self.patterns = [[0] * N_STEPS for _ in DRUMS]
            self.source = [None] * len(DRUMS)
            self.ratchet.clear()

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
            self.ratchet.clear()
            for lane, steps in mapping.items():
                if 0 <= lane < len(DRUMS):
                    for s in steps:
                        if 0 <= s < N_STEPS:
                            self.patterns[lane][s] = 1

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "armed": self.armed, "pending": self.pending,
                    "step": self._step, "patterns": [list(p) for p in self.patterns],
                    "source": list(self.source), "ratchet": dict(self.ratchet)}

    # -- save / restore (patterns + per-lane GEN source + ratchets) ----

    def get_state(self) -> dict:
        with self.lock:
            return {"patterns": [list(p) for p in self.patterns], "source": list(self.source),
                    "ratchet": {f"{l}_{s}": c for (l, s), c in self.ratchet.items()}}

    def set_state(self, s: dict) -> None:
        with self.lock:
            pats = s.get("patterns")
            if isinstance(pats, list):
                for i in range(min(len(pats), len(self.patterns))):
                    row = pats[i]
                    if isinstance(row, list):
                        r = [1 if x else 0 for x in row][:N_STEPS]
                        self.patterns[i] = r + [0] * (N_STEPS - len(r))
            src = s.get("source")
            if isinstance(src, list):
                for i in range(min(len(src), len(self.source))):
                    self.source[i] = src[i] if src[i] in SRC_CYCLE else None
            rat = s.get("ratchet")
            if isinstance(rat, dict):
                self.ratchet = {}
                for k, v in rat.items():
                    try:
                        ls, ss = k.split("_")
                        lane, st, cnt = int(ls), int(ss), int(v)
                        if cnt > 1 and 0 <= lane < len(DRUMS) and 0 <= st < N_STEPS:
                            self.ratchet[(lane, st)] = cnt
                    except Exception:
                        pass

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
        sched: list[list] = []   # each: [note, on_us, off_us, started]
        ending = False
        while not self._stop.is_set() and not ending:
            now = clock.micros()
            state = link.captureSessionState()
            sixteenth = int(state.beatAtTime(now, QUANTUM) * 4)
            tempo = state.tempo()
            # service scheduled (sub-)hits: trigger due notes, release expired
            still = []
            for it in sched:
                if not it[3] and now >= it[1]:
                    self._on(it[0])
                    it[3] = True
                if it[3] and now >= it[2]:
                    self._off(it[0])
                    continue
                still.append(it)
            sched = still
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
                    lane_state = [(self.patterns[i][step], self.source[i],
                                   self.ratchet.get((i, step), 1)) for i in range(len(DRUMS))]
                if not ending and armed:
                    step_us = 60.0 / max(tempo, 1.0) / 4.0 * 1_000_000
                    for i, (on_local, src, rat) in enumerate(lane_state):
                        note = DRUMS[i][1]
                        if src is not None:   # follow a GEN voice's rhythm (no ratchet)
                            sv = VOICES.get(src)
                            if sv is not None:
                                p = sv.step_prob(sixteenth)
                                if p > 0 and (p >= 1.0 or random.random() < p):
                                    self._on(note)
                                    sched.append([note, now, now + 25_000, True])
                        elif on_local:
                            r = max(1, rat)
                            if r == 1:
                                self._on(note)
                                sched.append([note, now, now + 25_000, True])
                            else:         # ratchet — r retriggers across the 16th
                                sub = step_us / r
                                dur = int(min(25_000, sub * 0.8))
                                for k in range(r):
                                    on_us = now + int(k * sub)
                                    if k == 0:
                                        self._on(note)
                                    sched.append([note, on_us, on_us + dur, k == 0])
            self._stop.wait(0.004)
        for it in sched:
            if it[3]:
                self._off(it[0])
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

# Make the drum machine part of the GEN preset / crash-restore system.
try:
    from isobar_engine import register_extra, _load_last_extra
    register_extra("drum", machine.get_state, machine.set_state)
    _load_last_extra("drum")       # restore drum slice of last.json (saved after our import)
except Exception:
    pass
