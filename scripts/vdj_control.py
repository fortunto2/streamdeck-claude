"""Virtual DJ control surface — Stream Deck as a MIDI controller for VDJ.

VirtualDJ sees a macOS MIDI port as a controller (Settings → CONTROLLERS →
… → Edit mapping). This surface uses its OWN dedicated port so VDJ never
sees the instrument notes: it opens **IAC Driver Bus 3** if you've made one,
otherwise a virtual port named **"StreamDeck VDJ"**. Either way it's separate
from the notes bus (Bus 2: drum ch10 + generators ch1-6) and the Looper bus
(Bus 1) — so VDJ's Learn can't grab a drum note, and the stream stays clean.

Each button sends a unique MIDI note (note number = key index). Map them once
in VDJ's "Edit mapping": click a slot, hit Learn, press the deck button, then
type the VDJ action (e.g. `deck 1 play`). See docs/virtualdj-control.md.

No feedback (VDJ → deck) yet — one-way control. Buttons flash on press.
"""

from __future__ import annotations

import deck_ui
from control_surface import ControlSurface

from src.midi_out import MidiOut

VDJ_CHANNEL = 0    # dedicated port, so channel is free — use ch 1
VDJ_PORT = "StreamDeck VDJ"
VDJ_IAC_PREFER = ["sdeck Bus 3", "IAC Driver Bus 3", "Bus 3"]   # its own bus, else virtual
KEY_HOME = ControlSurface.HOME_KEY  # 31

# (key, label, sub, colour). The MIDI note sent == key index. A1/B1 = decks.
A = "#22d3ee"   # deck 1 — cyan
B = "#fb923c"   # deck 2 — orange
X = "#a78bfa"   # crossfader / master — lilac
H = "#334155"   # hot cues
BUTTONS = [
    (0,  "LOAD",  "deck 1", A), (1,  "CUE",  "deck 1", A),
    (2,  "PLAY",  "deck 1", A), (3,  "SYNC", "deck 1", A),
    (4,  "SYNC",  "deck 2", B), (5,  "PLAY", "deck 2", B),
    (6,  "CUE",   "deck 2", B), (7,  "LOAD", "deck 2", B),
    (8,  "PITCH-", "d1", A),    (9,  "PITCH+", "d1", A),
    (10, "VOL-",   "d1", A),    (11, "VOL+",   "d1", A),
    (12, "VOL-",   "d2", B),    (13, "VOL+",   "d2", B),
    (14, "PITCH-", "d2", B),    (15, "PITCH+", "d2", B),
    (16, "CUE 1", "d1", H), (17, "CUE 2", "d1", H),
    (18, "CUE 3", "d1", H), (19, "CUE 4", "d1", H),
    (20, "CUE 1", "d2", H), (21, "CUE 2", "d2", H),
    (22, "CUE 3", "d2", H), (23, "CUE 4", "d2", H),
    (24, "XF ◀", "deck 1", X), (25, "XF ■", "center", X), (26, "XF ▶", "deck 2", X),
    (27, "MAST-", "vol", X),   (28, "MAST+", "vol", X),
    (29, "FX 1", "deck 1", "#0f766e"), (30, "FX 2", "deck 2", "#0f766e"),
]
_BTN = {k: (label, sub, color) for k, label, sub, color in BUTTONS}


class VdjControl(ControlSurface):

    def __init__(self, deck, on_home):
        super().__init__(deck, on_home)
        self._midi = None
        self._port = "—"

    def start(self) -> None:
        self.running = True
        try:
            self._midi = MidiOut(port_name=VDJ_PORT, iac_prefer=VDJ_IAC_PREFER)
            self._port = getattr(self._midi, "opened_name", VDJ_PORT)
            print(f"VDJ control → MIDI port: {self._port}  (map this one in VirtualDJ)")
        except Exception as e:
            print(f"VDJ control: MIDI open failed: {e}")
            self._midi = None
        self.render()

    def on_teardown(self) -> None:
        if self._midi is not None:
            try:
                self._midi.all_notes_off(VDJ_CHANNEL)
                self._midi.close()
            except Exception:
                pass
            self._midi = None

    # -- rendering -----------------------------------------------------

    def _btn_img(self, key: int, pressed: bool = False):
        label, sub, color = _BTN[key]
        bg = color if pressed else "#1f2937"
        fg = "#0b0f1a" if pressed else "#ffffff"
        return deck_ui.btn(bg, [(label, 14, fg), (sub, 10, color if not pressed else "#0b0f1a")],
                           border=color if not pressed else None)

    def render(self) -> None:
        if not self.running:
            return
        for k in range(32):
            if k in _BTN:
                self.set_key(k, self._btn_img(k))
            elif k != KEY_HOME:
                self.set_key(k, deck_ui.btn("#0b0f1a", []))
        if self._midi is None:
            self.set_key(0, deck_ui.btn("#7f1d1d", [("no MIDI", 12, "#fecaca"),
                                                    ("IAC off?", 9, "#f87171")]))
        self.render_home_key()

    # -- input ---------------------------------------------------------

    def on_key(self, _deck, key: int, pressed: bool) -> None:
        if key == KEY_HOME:
            if pressed:
                self.on_home()
            return
        if key not in _BTN:
            return
        if self._midi is not None:
            try:
                if pressed:
                    self._midi.note_on(key, 110, VDJ_CHANNEL)
                else:
                    self._midi.note_off(key, VDJ_CHANNEL)
            except Exception:
                pass
        self.set_key(key, self._btn_img(key, pressed))
