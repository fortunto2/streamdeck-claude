"""Config loader — YAML to dataclasses."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class DeckConfig:
    brightness: int = 30
    poll_interval: int = 3
    project_dir: str = "."


@dataclass
class TmuxConfig:
    session: str = "claude"
    default_pane: str = "0"


@dataclass
class ReaperConfig:
    """OSC connection to REAPER for the live-music pages."""

    enabled: bool = False
    send_host: str = "127.0.0.1"
    send_port: int = 8000
    listen_host: str = "127.0.0.1"
    listen_port: int = 9000
    # Auto-connect at daemon start. If False, the connection opens
    # lazily when the user first switches to a reaper page.
    auto_connect: bool = True


@dataclass
class ButtonConfig:
    pos: int
    type: str  # "monitor" | "action" | "toggle" | "reaper" | "midi" | "drum_step" | "page"
    monitor: str | None = None
    action: str | None = None
    command: str | None = None
    target: str | None = None
    pane: str | None = None
    prompt: str | None = None
    allowed_tools: str | None = None
    icon: str | None = None
    label: str | None = None
    # REAPER OSC actions: type="reaper", reaper_method = name on ReaperClient,
    # reaper_args = kwargs dict.
    reaper_method: str | None = None
    reaper_args: dict | None = None
    # MIDI note buttons: type="midi", midi_note = MIDI note number 0..127.
    midi_note: int | None = None
    midi_velocity: int = 100
    midi_channel: int = 0
    # Drum sequencer step toggle: type="drum_step", drum_voice + drum_step.
    drum_voice: str | None = None
    drum_step: int | None = None
    # Page switch: type="page", page = destination page name.
    page: str | None = None
    # Optional: which colour scheme to use for the button.
    color: str | None = None


@dataclass
class AppConfig:
    deck: DeckConfig = field(default_factory=DeckConfig)
    tmux: TmuxConfig = field(default_factory=TmuxConfig)
    reaper: ReaperConfig = field(default_factory=ReaperConfig)
    # Legacy single-page layout. Used when `pages` is empty.
    buttons: list[ButtonConfig] = field(default_factory=list)
    # Named multi-page layouts. The daemon picks `default_page` to show
    # at startup; page-switch buttons swap which page is active.
    pages: dict[str, list[ButtonConfig]] = field(default_factory=dict)
    default_page: str = "dashboard"


def load_config(path: Path) -> AppConfig:
    """Load config from YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    deck = DeckConfig(**{k: v for k, v in (raw.get("deck") or {}).items()})
    tmux = TmuxConfig(**{k: v for k, v in (raw.get("tmux") or {}).items()})
    reaper = ReaperConfig(**{k: v for k, v in (raw.get("reaper") or {}).items()})
    buttons = [ButtonConfig(**btn) for btn in (raw.get("buttons") or [])]
    pages: dict[str, list[ButtonConfig]] = {}
    for name, btns in (raw.get("pages") or {}).items():
        pages[name] = [ButtonConfig(**btn) for btn in (btns or [])]
    default_page = raw.get("default_page", "dashboard")

    return AppConfig(
        deck=deck,
        tmux=tmux,
        reaper=reaper,
        buttons=buttons,
        pages=pages,
        default_page=default_page,
    )
