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
class ButtonConfig:
    pos: int
    type: str  # "monitor" | "action" | "toggle"
    monitor: str | None = None
    action: str | None = None
    command: str | None = None
    target: str | None = None
    pane: str | None = None
    prompt: str | None = None
    allowed_tools: str | None = None
    icon: str | None = None
    label: str | None = None


@dataclass
class AppConfig:
    deck: DeckConfig = field(default_factory=DeckConfig)
    tmux: TmuxConfig = field(default_factory=TmuxConfig)
    buttons: list[ButtonConfig] = field(default_factory=list)


def load_config(path: Path) -> AppConfig:
    """Load config from YAML file."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    deck = DeckConfig(**{k: v for k, v in (raw.get("deck") or {}).items()})
    tmux = TmuxConfig(**{k: v for k, v in (raw.get("tmux") or {}).items()})
    buttons = [ButtonConfig(**btn) for btn in (raw.get("buttons") or [])]

    return AppConfig(deck=deck, tmux=tmux, buttons=buttons)
