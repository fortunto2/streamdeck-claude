# Vocal Looper page — setup

A multi-track live looping station on the deck (Music Hub → **LOOPER**): one row
per vocal layer (voice, backing, khomus drone, live khomus…), each a Live track
with a Looper device. Up to 4 layers.

Per row, left → right:

| LOOP | CLR | FX1 | FX2 | FX3 | MUTE | ARM | LEN |
|---|---|---|---|---|---|---|---|

LEN sets loop length: **tap = ×2**, **hold = ÷2**. (The 4th row's LEN is HOME.)

## How it works

- **FX1-3, MUTE, ARM, VU, looper LED** — all over **AbletonOSC, no mapping**.
  FX1-3 bypass the track's non-looper devices (reverb / distortion / comp …) in
  chain order; lit = on, dim = bypassed; names read from Live. The LOOP key's
  colour mirrors Live's looper state (grey=stop, red=rec, green=play, amber=dub).
- **LOOP / CLR** — go out as **MIDI** (channel 16 → IAC Bus 1), because setting
  the Looper State over OSC won't reliably start a fresh recording — the
  multi-purpose button always does. So these two you map once per layer.

## Which tracks show up

Any track that has a **Looper** device becomes a layer (in track order, first 4).
So just add a Looper to each vocal track — the page populates itself. Put the FX
(reverb, distortion, compressor) as the other devices on the same track; the
first three non-looper devices become FX1/FX2/FX3.

## One-time MIDI mapping

**Row 1 (top) is the main looper — it reuses the Ableton page's notes, so if you
already mapped the looper there, the top row already works. You only map the
extra loopers (rows 2-4).** MIDI is **channel 16**:

| Layer (row) | LOOP → note | CLR → note | ÷2 → note | ×2 → note |
|---|---|---|---|---|
| 1 (top, = Ableton page) | 113 | 110 | 111 | 112 |
| 2 | 114 | 115 | 116 | 117 |
| 3 | 118 | 119 | 120 | 121 |
| 4 | 122 | 123 | 124 | 125 |

To map an extra looper: in Live, MIDI-map mode (Cmd-M) → click that Looper's big
multi-purpose button → press the deck's **LOOP** key for that row → it learns the
note. Then map its **Clear** to **CLR**, and **÷2**/**×2** to the **LEN** key
(tap learns ×2, hold learns ÷2). The input port must have **Remote ✓** for Bus 1
(Settings → Tempo & MIDI → Input Ports → sdeck Bus 1).

At minimum map **LOOP** per row — that alone gives per-looper record/play/overdub.
CLR and ÷2/×2 are optional extras.

## Notes

- Only one Live-controlling page can hold the OSC feedback port at a time, so the
  Vocal Looper and the Ableton page each own it while open and release it on exit.
- Tempo/launch still sync via Ableton Link, independent of this page.
- FX bypass is the device's on/off switch (param 0) — toggling it is identical to
  clicking the device's power button in Live.
