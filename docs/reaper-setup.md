# REAPER live page — setup

The Stream Deck talks to REAPER over OSC. One-time setup in REAPER:

## 1. Enable OSC

`Preferences → Control / OSC / Web → Add → OSC`

| Field | Value |
|---|---|
| Device name | StreamDeck |
| Mode | Local port → Configure ports/devices |
| Receive on port | `8000` |
| Local listen IP | `127.0.0.1` |
| Send to IP | `127.0.0.1` |
| Send to port | `9000` |
| Pattern config | Default `Default 8x4.ReaperOSC` (ships with REAPER) |
| Wheel resolution | 1023 |
| Max packet size | 1024 |

Click OK. REAPER now listens on port 8000 and pushes feedback to port 9000.

## 2. Quick test

```bash
cd /Users/rustam/startups/active/streamdeck-claude
uv run python -c "
from src.reaper import ReaperClient
r = ReaperClient.from_env()
r.transport_play(); import time; time.sleep(2); r.transport_stop()
"
```

REAPER should start playing then stop after 2 seconds.

## 3. (Optional) Virtual MIDI port for the Drum / MIDI pages

`Preferences → MIDI Devices` → after the daemon starts you'll see a
new device called **StreamDeck** in the list. Enable it as input.

Route to `SuperDuper Drum` instance: add Drum to a track, in the
track's MIDI input dropdown pick `StreamDeck` → channel 10 (the drum
sequencer fires on channel 10 by GM convention).

## 4. Env vars (override defaults)

```bash
export REAPER_OSC_SEND_PORT=8000      # REAPER listens here
export REAPER_OSC_LISTEN_PORT=9000    # we listen here for feedback
```

## 5. SuperDuper plugin pages

The Stream Deck knows the param tables of our plugins (see
`src/superduper_catalog.py`). Address a plugin's params via:

```
/track/<N>/fx/<M>/fxparam/<P>/value   ← FX slot M on track N, param P
```

Where `P` is the index from the plugin's `PARAMS` table (see the
plugin's `effects/superduper-<name>/src/lib.rs`). The Stream Deck
plugin pages build these addresses from the catalogue — you pick a
plugin and the buttons map to its specific params.
