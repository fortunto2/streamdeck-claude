# Virtual DJ control page — setup

The deck's **VDJ** instrument (Music Hub → VDJ) turns the Stream Deck into a
MIDI controller for Virtual DJ: play / cue / sync / load, pitch, volume, hot
cues, crossfader. One-way control (deck → VDJ); tempo sync is separate (Ableton
Link — see `music-software-setup.md`).

## How it works

The VDJ page uses its **own dedicated MIDI port** so VirtualDJ never sees the
instrument notes (drum machine on Bus 2 ch 10, generators on Bus 2 ch 1–6) or
the Looper (Bus 1). It opens, in order:

1. **IAC Driver Bus 3** if you've created one (persistent — survives restarts), else
2. a **virtual port "StreamDeck VDJ"** (works immediately, but disappears when the
   daemon stops, so VDJ may need a remap after a cold restart).

Either way, VDJ only ever receives VDJ button presses — Learn can't grab a drum
note. Each button sends a unique note (note number = key index, 0–30).

The daemon log (`make logs`) prints which port it opened:
`VDJ control → MIDI port: …  (map this one in VirtualDJ)`.

### Recommended: make a dedicated IAC Bus 3

For a stable, persistent mapping:

1. **Audio MIDI Setup → MIDI Studio → IAC Driver** → add a 3rd bus (so you have
   Bus 1, Bus 2, Bus 3). Keep "Device is online" checked.
2. Restart the daemon (`make restart-daemon`). The VDJ page will now open Bus 3.

## One-time mapping in Virtual DJ

1. **Settings → CONTROLLERS** → pick the port the log named (**IAC Driver Bus 3**,
   or **StreamDeck VDJ** if you skipped the IAC bus) → **Edit mapping**.
2. For each button: click an empty mapping slot → **Learn** → press the button
   on the deck → it captures the note → type the VDJ action in the box.
3. Save the mapping.

Because the port is dedicated, you can map even while the drums are playing — VDJ
won't see them.

## Suggested actions (button → VDJ verb)

Deck 1 is the left half, Deck 2 the right. VDJ's scripting verbs:

| Button | VDJ action |
|---|---|
| PLAY (d1 / d2) | `deck 1 play` / `deck 2 play` |
| CUE | `deck 1 cue` / `deck 2 cue` |
| SYNC | `deck 1 sync` / `deck 2 sync` |
| LOAD | `deck 1 load` / `deck 2 load` (loads the highlighted browser track) |
| PITCH- / PITCH+ | `deck 1 pitch -0.02` / `deck 1 pitch +0.02` |
| VOL- / VOL+ | `deck 1 volume -5%` / `deck 1 volume +5%` |
| CUE 1–4 | `deck 1 hot_cue 1` … `hot_cue 4` |
| XF ◀ / ■ / ▶ | `crossfader 0%` / `crossfader 50%` / `crossfader 100%` |
| MAST- / MAST+ | `master_volume -5%` / `master_volume +5%` |
| FX 1 / FX 2 | `deck 1 effect_active 1` / `deck 2 effect_active 1` |

(Exact verb names vary slightly by VDJ version — VDJ autocompletes them in the
action box. `play`, `cue`, `sync`, `load`, `hot_cue n`, `crossfander`,
`pitch`, `volume` are the stable ones.)

## Notes

- **Momentary:** press = NoteOn, release = NoteOff. VDJ triggers on NoteOn, so a
  tap fires the action once (play toggles, cue jumps, etc.).
- **No feedback yet:** the deck doesn't read VDJ's state back, so a button's
  colour is a press flash, not deck-play status. Two-way (VDJ → deck LEDs) is
  possible later via VDJ's MIDI output mapping.
- **Tempo still syncs via Link**, not this page. Use the VDJ Master FX → Ableton
  Link → Master so VDJ drives the shared tempo (see `music-software-setup.md`).
