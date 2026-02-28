"""Shared audio engine for Stream Deck Arcade.

Provides process-tracked audio playback with toggle flags.
Import this instead of calling subprocess.Popen(["afplay",...]) directly.
"""

import subprocess
import threading

# ── toggle flags (arcade.py can flip these) ──────────────────────────
voices_enabled: bool = True
sfx_enabled: bool = True
global_mute: bool = False  # overrides all sound when True

# ── process tracking ─────────────────────────────────────────────────
_processes: list[subprocess.Popen] = []
_lock = threading.Lock()
_MAX_CONCURRENT = 4


def _reap():
    """Remove finished processes — prevents zombie accumulation."""
    with _lock:
        alive = []
        for p in _processes:
            ret = p.poll()
            if ret is None:
                alive.append(p)
            # poll() already reaps on macOS/POSIX
        _processes[:] = alive


def _play(filepath: str) -> None:
    """Core: spawn afplay with tracking + zombie cleanup."""
    if global_mute:
        return
    _reap()
    with _lock:
        # kill oldest if too many concurrent
        while len(_processes) >= _MAX_CONCURRENT:
            old = _processes.pop(0)
            try:
                old.kill()
                old.wait()
            except Exception:
                pass
        try:
            p = subprocess.Popen(
                ["afplay", filepath],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            _processes.append(p)
        except Exception:
            pass


def play_voice(filepath: str) -> None:
    """Play character voice line (respects voices_enabled flag)."""
    if not voices_enabled:
        return
    _play(filepath)


def play_sfx_file(filepath: str) -> None:
    """Play SFX sound (respects sfx_enabled flag)."""
    if not sfx_enabled:
        return
    _play(filepath)


def stop_all() -> None:
    """Kill all running audio — call on game exit."""
    with _lock:
        for p in _processes:
            try:
                p.kill()
                p.wait()
            except Exception:
                pass
        _processes.clear()
