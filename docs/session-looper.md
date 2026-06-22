# Session Looper page — record loops into Ableton Session clips

Music Hub → **SESSION**. The deck becomes Live's Session grid: **8 tracks
(columns) × 3 scene slots (rows)**, driven over AbletonOSC. The recorded loops
are native Session clips — Live draws their waveforms, the slots show the layer
stack, and you can drag a clip onto a Drum Rack pad / merge / export by hand.

## Use

- **Tap an empty slot** (`+`) → the deck arms that track and fires the slot, so
  Live records the track's input into a new clip. Tap again (or it auto-stops at
  the bar when Session record quantize is set) → the clip loops.
- **Tap an occupied clip** → launch / relaunch it. Playing = bright + white
  border; queued = blink; recording = red blink.
- **Long-press an occupied clip** → **trim + align**: the daemon reads the
  clip's recorded file off disk (soundfile + numpy, like librosa.effects.trim),
  finds where the sound actually starts/ends, and sets a **beat-aligned loop**
  to just that. (OSC can't see audio — we read the file directly.)
- **Bottom row:** SCENE 1-3 fire a whole row (the "master layer"); STOP = stop
  all; **QUANT ↻** cycles global launch/record quantization (1 Bar / 2 Bar / 1/4
  / Off) so loops record on the grid by default; HOME.

## Setup in Live

- Tracks you record into should be **audio tracks** with their input set to the
  mic/instrument and **Monitor = Auto** (the deck arms them on record).
- For clean bar-length loops, set **Record Quantization** (Edit menu) so loops
  snap to the bar; tempo stays on **Link**.
- One Live-controlling deck page holds the OSC feedback port at a time, so the
  Session Looper / Ableton / Vocal Looper pages each own it while open.

## Why this instead of a custom looper

Session clips are the visible layers — Live owns the audio, shows the waveform,
and lets you edit/warp/save with the project. Simultaneous layers live on
separate tracks (one clip per track plays at once); a scene plays them together.
A custom DSP looper would only be needed for true same-source overdub into one
growing loop (it's half-built in superduper-dsp, parked for now).
