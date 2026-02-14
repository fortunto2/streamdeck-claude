"""Tests for config loader — YAML to dataclasses."""

import tempfile
from pathlib import Path

import yaml


def test_load_config_parses_buttons():
    """load_config should parse YAML into ButtonConfig list."""
    raw = {
        "deck": {"brightness": 30, "poll_interval": 3},
        "tmux": {"session": "claude", "default_pane": "0"},
        "buttons": [
            {
                "pos": 0,
                "type": "monitor",
                "monitor": "git_status",
                "icon": "git.png",
                "label": "Git",
            },
            {
                "pos": 8,
                "type": "action",
                "action": "tmux_send",
                "command": "/research",
                "icon": "research.png",
                "label": "Research",
            },
        ],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = f.name

    from src.config import load_config

    cfg = load_config(Path(path))
    assert cfg.deck.brightness == 30
    assert len(cfg.buttons) == 2
    assert cfg.buttons[0].type == "monitor"
    assert cfg.buttons[0].monitor == "git_status"
    assert cfg.buttons[1].type == "action"
    assert cfg.buttons[1].action == "tmux_send"
    assert cfg.buttons[1].command == "/research"


def test_load_config_defaults():
    """Missing optional fields should get defaults."""
    raw = {
        "deck": {},
        "buttons": [{"pos": 0, "type": "monitor", "monitor": "git_status"}],
    }
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(raw, f)
        path = f.name

    from src.config import load_config

    cfg = load_config(Path(path))
    assert cfg.deck.brightness == 30
    assert cfg.deck.poll_interval == 3
    assert cfg.buttons[0].icon is None
    assert cfg.buttons[0].label is None
