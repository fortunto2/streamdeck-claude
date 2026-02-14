"""Monitoring threads — poll system state."""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable

import psutil


def check_git_status(project_dir: str) -> str:
    """Check git working tree status for a project directory.

    Returns: "clean", "dirty", "untracked", or "unknown".
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=project_dir, timeout=5,
        )
        output = result.stdout.strip()
        if not output:
            return "clean"
        lines = output.split("\n")
        if all(line.startswith("??") for line in lines):
            return "untracked"
        return "dirty"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unknown"


def check_claude_sessions() -> int:
    """Count running Claude Code sessions (--dangerously flag)."""
    try:
        result = subprocess.run(
            ["pgrep", "-fl", "claude.*--dangerously"],
            capture_output=True, text=True, timeout=5,
        )
        lines = [l for l in result.stdout.strip().split("\n") if l]
        return len(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return 0


def check_pipeline_state(pipelines_dir: str = os.path.expanduser("~/.solo/pipelines")) -> str:
    """Check solo pipeline state from marker files.

    Returns current stage name or "idle".
    """
    pattern = os.path.join(pipelines_dir, "solo-pipeline-*.local.md")
    files = glob.glob(pattern)
    if not files:
        return "idle"
    try:
        with open(files[0]) as f:
            content = f.read()
        match = re.search(r"^stage:\s*(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()
    except (FileNotFoundError, OSError):
        pass
    return "idle"


def check_system() -> dict:
    """Return CPU usage and free disk percentage."""
    return {
        "cpu": psutil.cpu_percent(interval=0.5),
        "disk_free_pct": round(
            shutil.disk_usage("/").free / shutil.disk_usage("/").total * 100, 1
        ),
    }


def check_tmux_panes() -> list[str]:
    """List all tmux panes with their current commands."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}:#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return [l for l in result.stdout.strip().split("\n") if l]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return []


def check_tmux_sessions() -> list[dict]:
    """List tmux sessions with name, window count, and active pane command.

    Returns list of dicts: {"name": str, "windows": int, "command": str, "attached": bool}
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F",
             "#{session_name}\t#{session_windows}\t#{session_attached}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        sessions = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t")
            name = parts[0]
            windows = int(parts[1]) if len(parts) > 1 else 1
            attached = parts[2] == "1" if len(parts) > 2 else False
            # Get active pane command for this session
            cmd = _get_session_command(name)
            sessions.append({
                "name": name,
                "windows": windows,
                "command": cmd,
                "attached": attached,
            })
        return sessions
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def _get_session_command(session_name: str) -> str:
    """Get the current command running in the active pane of a session."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p",
             "#{pane_current_command}"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "?"


class MonitorThread(threading.Thread):
    """Background thread that polls all monitors at a fixed interval.

    Updates shared state dict under lock, calls on_change when values differ.
    """

    def __init__(self, state: dict, lock: threading.Lock,
                 interval: float, project_dir: str,
                 on_change: Callable | None = None):
        super().__init__(daemon=True)
        self.state = state
        self.lock = lock
        self.interval = interval
        self.project_dir = project_dir
        self.on_change = on_change
        self._stop_event = threading.Event()

    def stop(self):
        """Signal the monitor thread to stop."""
        self._stop_event.set()

    def run(self):
        while not self._stop_event.is_set():
            new_state = {
                "git": check_git_status(self.project_dir),
                "claude_count": check_claude_sessions(),
                "pipeline": check_pipeline_state(),
                **check_system(),
                "tmux_panes": check_tmux_panes(),
                "tmux_sessions": check_tmux_sessions(),
            }
            changed = False
            with self.lock:
                for key, val in new_state.items():
                    if self.state.get(key) != val:
                        self.state[key] = val
                        changed = True
            if changed and self.on_change:
                self.on_change(self.state.copy())
            self._stop_event.wait(self.interval)
