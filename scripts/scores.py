"""Persistent high scores for Stream Deck Arcade.

Stores best scores in ~/.streamdeck-arcade/scores.json.
"""

import json
import os
import threading

_SCORES_DIR = os.path.expanduser("~/.streamdeck-arcade")
_SCORES_FILE = os.path.join(_SCORES_DIR, "scores.json")
_lock = threading.Lock()


def _load_all() -> dict:
    try:
        with open(_SCORES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_best(game: str, default: int = 0) -> int:
    """Load best score for a game. Returns default if no record."""
    data = _load_all()
    return data.get(game, default)


def save_best(game: str, score: int) -> None:
    """Save best score for a game."""
    with _lock:
        os.makedirs(_SCORES_DIR, exist_ok=True)
        data = _load_all()
        data[game] = score
        with open(_SCORES_FILE, "w") as f:
            json.dump(data, f, indent=2)
