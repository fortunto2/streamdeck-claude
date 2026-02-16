# CLAUDE.md

## Project Overview

Stream Deck XL (32-key) dashboard for Claude Code workflows and mini-games arcade. Two modes: **Claude dashboard** (monitors, pipeline actions, tmux control) and **Arcade** (25+ mini-games with voice packs, scores, live data).

## Tech Stack

- **Language:** Python 3.13
- **Package Manager:** uv
- **Hardware:** Stream Deck XL (8x4, 96x96 key images)
- **Libraries:** streamdeck (USB HID), Pillow (image rendering), psutil (system monitors), PyYAML (config), ccxt (crypto prices)
- **Audio:** afplay (macOS) via `scripts/sound_engine.py`
- **Daemon:** launchd (`com.streamdeck.dashboard.plist`)

## Directory Structure

```
src/                    # Claude dashboard mode
  daemon.py             # Main daemon class (StreamDeckClaude)
  config.py             # YAML config loader (AppConfig, ButtonConfig)
  actions.py            # Button actions (claude_p, shell, tmux_send/switch)
  monitors.py           # Background pollers (git, CPU, disk, pipeline, claude sessions)
  renderer.py           # Button image rendering (status colors, icons)
config.yaml             # 32-button layout config (monitors, actions, tmux)
assets/                 # Button icons (96x96 PNG)
scripts/                # Arcade mode (standalone dashboard + games)
  dashboard.py          # Home screen: clock, weather, crypto, game launcher
  arcade.py             # Legacy game menu (use dashboard.py instead)
  sound_engine.py       # Shared audio: afplay with process tracking, voice/sfx toggles
  scores.py             # Persistent high scores (~/.streamdeck-arcade/)
  weather.py            # Weather data fetcher
  *_game.py             # 25 mini-games (snake, empire, memory, breakout, etc.)
tests/                  # pytest tests for src/ modules
com.streamdeck.dashboard.plist  # launchd template (placeholders: __CWD__, __UV__, __HOME__)
```

## Essential Commands

```bash
make install            # Install deps (brew hidapi + uv sync)
make install-daemon     # Install launchd daemon (auto-start on login)
make uninstall-daemon   # Remove daemon
make restart-daemon     # Restart daemon
make status             # Daemon status (launchctl)
make logs               # Tail daemon logs (/tmp/streamdeck-dashboard.log)
make ps                 # List all running streamdeck processes
make kill               # Kill ALL streamdeck processes (daemon + games)
make dashboard          # Run dashboard manually (foreground)
make run                # Run Claude dashboard mode (src/daemon.py)
make dev                # Run Claude dashboard verbose mode
```

## Architecture

### Two Modes

1. **Claude Dashboard** (`src/daemon.py` + `config.yaml`): 4-row layout
   - Row 1 (0-7): Monitors (git, pipeline, claude sessions, tests, CPU, disk)
   - Row 2 (8-15): Solo pipeline actions (/research, /validate, /scaffold, /plan, /build, /deploy, /review, /pipeline)
   - Row 3 (16-23): Dynamic tmux sessions (auto-populated)
   - Row 4 (24-31): Git ops, /ralph-loop, /swarm, KB search, brightness, exit

2. **Arcade Dashboard** (`scripts/dashboard.py`): Home screen with live data + 25 games
   - Page 1: Clock, weather, crypto prices, game buttons
   - Games loaded dynamically via `importlib` from `scripts/*_game.py`
   - Each game class has `start(deck)` / `stop()` / `on_key(deck, key, pressed)` interface

### Daemon Lifecycle

- launchd starts `dashboard.py` at login
- `main()` polls for USB device every 2s via `DeviceManager().enumerate()`
- Device found: opens deck, creates Dashboard, runs game loop
- Device lost: closes deck, cleans up sounds, waits for reconnect
- Crash: launchd restarts after 5s (`ThrottleInterval`)

### Sound Engine (`scripts/sound_engine.py`)

- `play_voice(path)` / `play_sfx_file(path)` — respect toggle flags
- Process tracking: max 4 concurrent afplay, kills oldest on overflow
- `stop_all()` — kill all playing audio (called on game exit)
- Games use voice packs from `~/.claude/hooks/peon-ping/packs/` (symlink to `~/.openpeon/packs/`)

### Game Pattern

Every game in `scripts/` follows this interface:
```python
class SomeGame:
    def start(self, deck): ...    # Init state, render keys
    def stop(self): ...           # Cleanup timers, stop sounds
    def on_key(self, deck, key, pressed): ...  # Handle input
```
Dashboard calls `start()` on launch, `stop()` on exit (key 0 = back to menu).

## Key Files

- `config.yaml` — Button layout for Claude dashboard mode (32 keys)
- `scripts/dashboard.py:main()` — Entry point, USB reconnect loop
- `scripts/sound_engine.py` — All audio goes through here
- `scripts/scores.py` — High scores persistence (`~/.streamdeck-arcade/`)
- `com.streamdeck.dashboard.plist` — launchd template

## Don't

- Hardcode sound file paths — use peon-ping config `active_pack` to resolve pack dynamically
- Leave game processes running after deck disconnect — `sound_engine.stop_all()` in finally blocks
- Modify `com.streamdeck.dashboard.plist` installed copy directly — edit template + `make restart-daemon`
- Use `arcade.py` for new features — `dashboard.py` is the active entry point

## Do

- Run `make ps` before debugging sound issues — stale game processes cause phantom audio
- Use `sound_engine` for all audio (never raw `subprocess.Popen(["afplay", ...])`)
- Test games with deck connected (`make dashboard`)
- Keep game classes self-contained (one file per game, standard interface)
- Use `make kill` to clean up before switching modes
