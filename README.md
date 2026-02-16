# Stream Deck XL Dashboard

A 32-key Stream Deck XL dashboard that combines **live environment monitoring**, **productivity tools**, and a **26-game arcade** — all rendered in real-time on physical keys.

Built entirely with Claude Code on a Stream Deck XL (8x4 grid, 96x96px per key).

## What It Does

### Home Screen (Live Data)

| Keys | What |
|------|------|
| 0-3 | Big clock (4-key wide) |
| 4-5 | Weather: temperature, wind, humidity (Open-Meteo) |
| 6-7 | Crypto prices: BTC, ETH (live via ccxt) |
| 8-10 | Section navigation: Games, Agents, Crypto |
| 11 | Siri activation |
| 16-23 | Environment row: PM2.5, PM10, indoor temp/humidity, pressure, UV index, AQI, sea waves |
| 24-27 | Pomodoro timer (15/30/60 min with voice alerts) |
| 28-31 | Activity tracker: uptime, work session, daily total, idle detection |

### Environment Monitoring

- **Local air sensor** — PM2.5, PM10, temperature, humidity, pressure from a [Luftdaten/airRohr](https://sensor.community/) sensor (auto-discovered via mDNS)
- **Air quality** — European AQI, UV index from Open-Meteo
- **Marine** — Wave height, swell, wave direction from Open-Meteo
- **Weather** — Temperature, wind, humidity, weather code from Open-Meteo

All values color-coded: green (good) through red (hazardous) using US EPA / European AQI breakpoints.

### Productivity

- **Pomodoro timer** — 15/30/60 min focus sessions with Silicon Valley voice pack sounds, pause/resume, session counter
- **Activity tracker** — Computer uptime, continuous work session, daily total, idle detection (flashes health alert when you need a break, tap to reset session)
- **State persistence** — Pomodoro and activity state survives daemon restarts

### Arcade (26 Games)

Every game runs directly on the Stream Deck keys with sound effects and voice packs.

| Game | Description |
|------|-------------|
| Snake | Classic snake on a 3x8 grid |
| Pac-Man | Ghost chase with pellets |
| Space Invaders | Shoot descending aliens |
| Breakout | Paddle and bricks |
| Beaver Hunt | Duck Hunt tribute |
| Bunny Cross | Frogger-style crossing |
| Dodge | Avoid falling obstacles |
| Simon Says | Memory color sequence |
| Memory | Card matching pairs |
| N-Back | Working memory trainer |
| Pattern | Pattern recognition |
| Sequence | Number sequence puzzles |
| Math Sequence | Math pattern completion |
| Quick Math | Speed arithmetic |
| Number Grid | Find numbers in order |
| Reaction | Reaction time test |
| Lights Out | Toggle puzzle |
| Minesweeper | Classic mines |
| Dungeon | Roguelike dungeon crawl |
| Tower Defense | Place towers, stop waves |
| Factory Chain | Conveyor belt puzzle (belts, furnaces, 10+ levels, speed control) |
| Colony | Ant colony simulation |
| Empire | Civilization builder |
| Trader | Buy low, sell high |
| Crypto Tycoon | Simulated crypto trading |
| Crypto Real | Live crypto portfolio tracker |

Games feature persistent high scores, voice packs per game, and sound effects via a shared audio engine.

## Requirements

- macOS (uses `afplay` for audio, `launchd` for daemon, `ioreg` for idle detection)
- [Stream Deck XL](https://www.elgato.com/stream-deck-xl) (32 keys)
- Python 3.12+
- [uv](https://github.com/astral-sh/uv) package manager
- `hidapi` (`brew install hidapi`)

## Setup

```bash
# Clone
git clone https://github.com/fortunto2/streamdeck-claude.git
cd streamdeck-claude

# Configure location (for weather, air quality, marine data)
cp .env.example .env
# Edit .env with your coordinates and optional sensor config

# Install
make install

# Run (foreground)
make dashboard

# Or install as login daemon (auto-starts on login)
make install-daemon
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `STREAMDECK_LAT` | 40.71 | Latitude for weather/air/marine APIs |
| `STREAMDECK_LON` | -74.00 | Longitude |
| `STREAMDECK_SENSOR_MDNS` | — | Local air sensor mDNS hostname (e.g., `airRohr-12345678.local`) |
| `STREAMDECK_SENSOR_IP` | — | Fallback IP if mDNS fails |

## Commands

```bash
make dashboard        # Run dashboard (foreground)
make restart          # Kill + restart in background
make install-daemon   # Install as launchd daemon
make restart-daemon   # Restart daemon
make uninstall-daemon # Remove daemon
make status           # Check daemon status
make logs             # Tail daemon logs
make ps               # List running processes
make kill             # Kill all Stream Deck processes
```

## Architecture

```
scripts/
  dashboard.py      # Main entry point — home screen, navigation, live data
  sound_engine.py   # Audio engine (afplay, process tracking, voice/sfx toggles)
  scores.py         # Persistent high scores (~/.streamdeck-arcade/)
  weather.py        # Open-Meteo weather API
  airquality.py     # Local sensor + Open-Meteo air quality + marine
  pomodoro.py       # Pomodoro timer with voice alerts
  activity.py       # System uptime, session tracking, idle detection
  *_game.py         # 26 game modules

src/                # Claude Code dashboard mode (monitors, tmux, pipeline)
  daemon.py         # Main daemon
  config.py         # YAML config loader
  actions.py        # Button actions
  monitors.py       # Background pollers
  renderer.py       # Key image rendering
```

### Game Interface

Every game follows a standard interface:

```python
class MyGame:
    def start(self, deck): ...              # Init state, render keys
    def stop(self): ...                     # Cleanup timers, stop sounds
    def on_key(self, deck, key, pressed): ...  # Handle input (key 0 = back)
```

### APIs Used (No Keys Needed)

- [Open-Meteo Weather](https://open-meteo.com/) — temperature, wind, humidity
- [Open-Meteo Air Quality](https://open-meteo.com/) — PM2.5, PM10, European AQI, UV index
- [Open-Meteo Marine](https://open-meteo.com/) — wave height, swell, direction
- [Luftdaten/sensor.community](https://sensor.community/) — local air sensor via HTTP

## License

MIT
