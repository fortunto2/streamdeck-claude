# src/actions.py
"""Action handlers for button presses."""

import os
import subprocess


def tmux_send(target: str, command: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", target, command, "Enter"],
        timeout=5,
    )


def claude_p(prompt: str, allowed_tools: str | None = None) -> None:
    cmd = ["claude", "-p", prompt]
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    env = {**os.environ}
    env.pop("CLAUDECODE", None)
    subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def shell_exec(command: str) -> None:
    subprocess.run(command, shell=True, timeout=30)


def tmux_select(pane: str) -> None:
    subprocess.run(["tmux", "select-pane", "-t", pane], timeout=5)


def tmux_switch(session: str) -> None:
    """Switch tmux client to a different session."""
    subprocess.run(["tmux", "switch-client", "-t", session], timeout=5)
