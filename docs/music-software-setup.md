# Music software setup — Ableton Link, AbletonOSC, Virtual DJ, MIDI

How to wire the deck's music suite (generators, drum machine, Ableton page)
to live software so everything shares one tempo and bar grid. Companion to
`reaper-setup.md`.

## The big picture

```
Virtual DJ ──┐
             ├── Ableton Link session (one shared tempo + phase) ──┐
Ableton Live─┤                                                     │
             └── Stream Deck daemon (GEN voices A–F, drum machine) ┘
```

- **Ableton Link** is the clock everyone agrees on. There is **no master/slave** —
  any peer that moves the tempo moves it for everyone, instantly.
- **AbletonOSC** is a separate channel — only for the deck's Ableton page
  (clip launch, VU meters, track colours). It is *not* how tempo syncs.
- **IAC MIDI** carries the actual notes the generators/drum machine play.

---

## 1. Ableton Link (tempo + phase sync)

This is what makes the GEN voices and drum machine lock to Live and Virtual DJ.

**In Ableton Live:** click the **Link** badge (top-left, next to the tempo).
When it shows a peer count ("2 Links") you're connected. Optional:
`Preferences → Link, Tempo & MIDI → Start Stop Sync` ties transport play/stop
across peers too (handy or surprising — turn off if you get unexpected starts).

**Our daemon** holds one shared Link instance (`isobar_engine._get_link()`),
so all six GEN voices + the drum machine are one peer phase-locked to the bar
(quantum = 4 beats). Start is **bar-quantised**: `▶ALL` enters exactly on the
next Link bar.

**Verify from the deck:** the Music Hub TEMPO key shows `● N LINK` — N = number
of other peers seen. 0 peers = `solo` (nothing else connected, or Link off in
the other app).

**One leader rule:** since the last app to move the tempo wins, pick ONE source.
If Virtual DJ leads the set, **don't touch TAP / ×2 / ÷2 / 100 on the deck** —
one tap overrides VDJ and everything drifts.

---

## 2. Virtual DJ → Link (make VDJ broadcast its BPM)

> Verified against the VirtualDJ forum + Ableton docs (June 2026).

**Key fact: in Virtual DJ, Ableton Link is an EFFECT, not a settings toggle.**
By default VDJ only *follows* the session — it time-stretches tracks to the
session tempo but never pushes its own BPM. That's why Live's tempo can sit
frozen (e.g. stuck at a track's 80.76) no matter how you move the pitch.

**To make VDJ the tempo source:**
1. Put the **Ableton Link** effect in the **Master FX** slot (master effects —
   NOT an individual deck's FX slot).
2. Inside the effect, enable the **Master** button.

Now VDJ pushes the master deck's tempo into the Link session → Live and our
daemon follow.

**Known VirtualDJ bug:** when you switch which deck is master, the Link tempo
**freezes at the previous deck's value** ("on Ableton nothing happens, the time
remains at the value of deck 1"). Workaround: **toggle the Ableton Link effect
off→on** in Master FX each time you change decks. Atomix hasn't shipped a proper
settings-level integration yet.

> Gotcha: if the Ableton Link effect is on a single *deck* slot (not Master FX),
> it only broadcasts that deck's tempo — a common cause of "won't follow on deck
> change".

### Matching decks (VDJ "assist" features)

For clean beat-matching that keeps the Link downbeat stable:

| Feature | Set | Why |
|---|---|---|
| Auto Match BPM | **ON** | new track snaps to master tempo → smooth mix, Link tempo doesn't jump on crossfade |
| Auto Sync on Play | **ON** | syncs tempo **and phase** on Play — keeps downbeats aligned, so our `▶ALL` voices land on beat 1 |
| Auto Pitch Lock (keylock) | **ON** | preserves pitch when time-stretching — vocals/melody don't detune |
| Auto Match KEY | optional | harmonic mixing; no effect on sync |

With both decks matched (same BPM) and Auto Sync holding phase, the Link
downbeat is stable → the generative layer enters in the pocket.

---

## 3. AbletonOSC (deck's Ableton clip-launcher page)

Only needed for the Ableton control page (scenes, per-track mute/arm, VU, colours).

- Remote script cloned to
  `~/Music/Ableton/User Library/Remote Scripts/AbletonOSC`.
- In Live: `Preferences → Link, Tempo & MIDI → MIDI` → add **AbletonOSC** as a
  Control Surface. **Input = None** (it listens on UDP, not MIDI).
- Restart Live so it appears in the Control Surface dropdown.
- Listens UDP **11000**, replies on **11001**. Addresses used:
  `/live/scene/fire`, `/live/track/set/{mute,arm,solo}`,
  `/live/track/get/{color,output_meter_level}`, `/live/clip_slot/get/is_*`,
  `/live/song/get/beat`, `/live/view/get/selected_track`.
- We patched a noisy `logger.info` in `clip_slot.py` (upstream PR #213) — the
  flood slowed startup. If you re-clone AbletonOSC, re-apply or pull a build
  with the fix.

---

## 4. IAC MIDI buses (notes from generators / drum machine)

The generators and drum machine send MIDI notes; the Looper control sends on a
separate bus so they never collide.

- **Audio MIDI Setup → IAC Driver** → enable "Device is online", add two buses.
- **Bus 1 = Looper control** (Remote-mapped in Live).
- **Bus 2 = notes** — GEN voices A–F each on their own channel; the drum
  machine on **channel 10** (GM drums). In Live, set the instrument/Drum Rack
  track's MIDI input to **IAC Bus 2** (+ channel filter as needed).
- The daemon prefers an IAC bus automatically (`MidiOut(iac_prefer=...)`).

GM Drum Rack mapping: Bass Drum = C1 = note 36, chromatic up (kick 36 … claves 51).

---

## 5. Troubleshooting

**"Is the session connected / is tempo moving?"** — probe Link directly:

```python
# uv run python - <<'PY'
import link as L, time
lk = L.Link(120.0); lk.enabled = True
for _ in range(20):
    s = lk.captureSessionState()
    print(f"tempo={s.tempo():.2f}  peers={lk.numPeers()}")
    time.sleep(1)
PY
```

`peers` counts everyone but this probe (daemon + Live + VDJ = 3). If tempo is
frozen while you move VDJ's pitch → VDJ isn't broadcasting (see §2).

**Deck went totally unresponsive ("no control")** — historically an unhandled
exception in a key callback killed the StreamDeck reader thread. Now guarded
(`Dashboard._bind_keys`): a surface error prints a traceback to
`/tmp/streamdeck-dashboard.log` but never bricks input. Check `make logs`.

**Tempo jitter from TAP** — tap-tempo needs ≥3 taps, then locks to the median
of the last 3 intervals and rounds to a whole BPM (`isobar_engine.tap_tempo`).

**`make logs`** tails the daemon; **Music Hub TEMPO key** shows live peer count.
