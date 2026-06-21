"""808/909-style drum machine — a 16-step sequencer, Link-synced.

Independent of the generator voices but shares their Link session (locks to
the same bar grid) and MIDI port (IAC Bus 2). Plays GM-percussion notes on
MIDI channel 10 — point a Live drum rack / 808 kit at Bus 2, channel 10.

Background singleton: keeps playing while the deck flips pages. Launch is
bar-quantised (queued → starts/stops on the next Link bar), like the voices.
"""

from __future__ import annotations

import threading

from isobar_engine import _get_link, _get_midi, QUANTUM

N_STEPS = 16
CH = 9   # GM drums = MIDI channel 10

# (name, GM note, colour)
DRUMS = [
    ("KICK", 36, "#ef4444"),
    ("SNARE", 38, "#f97316"),
    ("HAT-C", 42, "#eab308"),
    ("HAT-O", 46, "#84cc16"),
    ("CLAP", 39, "#06b6d4"),
    ("TOM", 45, "#a855f7"),
    ("RIM", 37, "#ec4899"),
    ("PERC", 50, "#14b8a6"),
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
        # A simple default groove so it's immediately useful.
        for s in (0, 4, 8, 12):
            self.patterns[0][s] = 1                 # kick — four on the floor
        for s in (4, 12):
            self.patterns[1][s] = 1                 # snare — backbeat
        for s in range(0, N_STEPS, 2):
            self.patterns[2][s] = 1                 # closed hat — 8ths
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

    def snapshot(self) -> dict:
        with self.lock:
            return {"running": self.running, "armed": self.armed, "pending": self.pending,
                    "step": self._step, "patterns": [list(p) for p in self.patterns]}

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
                    hits = [DRUMS[i][1] for i in range(len(DRUMS)) if self.patterns[i][step]]
                if not ending and armed:
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
