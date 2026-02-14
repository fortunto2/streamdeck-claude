# src/daemon.py
"""Stream Deck Claude — main daemon."""

import argparse
import os
import sys
import threading
from pathlib import Path

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

from src.actions import claude_p, shell_exec, tmux_select, tmux_send
from src.config import AppConfig, ButtonConfig, load_config
from src.monitors import MonitorThread
from src.renderer import render_button, status_to_color


def find_deck():
    """Find first visual Stream Deck device."""
    decks = DeviceManager().enumerate()
    for deck in decks:
        if deck.is_visual():
            return deck
    return None


class StreamDeckClaude:
    """Main application class."""

    def __init__(self, config: AppConfig, deck, verbose: bool = False):
        self.config = config
        self.deck = deck
        self.verbose = verbose
        self.state: dict = {}
        self.state_lock = threading.Lock()
        self.button_map: dict[int, ButtonConfig] = {
            btn.pos: btn for btn in config.buttons
        }
        self.brightness_level = 0
        self.brightness_levels = [30, 70, 100]

    def start(self):
        """Initialize deck and start monitoring."""
        self.deck.open()
        self.deck.reset()
        self.deck.set_brightness(self.config.deck.brightness)

        # Render initial button images
        for btn in self.config.buttons:
            self._render_button(btn)

        # Start monitor thread
        project_dir = os.path.expanduser(self.config.deck.project_dir)
        self.monitor = MonitorThread(
            state=self.state,
            lock=self.state_lock,
            interval=self.config.deck.poll_interval,
            project_dir=project_dir,
            on_change=self._on_state_change,
        )
        self.monitor.start()

        # Register button callback
        self.deck.set_key_callback(self._on_key_change)

        if self.verbose:
            print(f"Monitoring {project_dir}, poll every {self.config.deck.poll_interval}s")

    def stop(self):
        """Shutdown cleanly."""
        self.monitor.stop()
        self.deck.reset()
        self.deck.close()

    def _render_button(self, btn: ButtonConfig, status: str | None = None):
        """Render and set a button image on the deck."""
        bg = status_to_color(status) if status else "#1e3a5f"
        icon_path = None
        if btn.icon:
            p = Path(__file__).parent.parent / "assets" / btn.icon
            if p.exists():
                icon_path = str(p)

        img = render_button(
            size=(96, 96),
            label=btn.label,
            bg_color=bg,
            icon_path=icon_path,
        )
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(btn.pos, native)

    def _on_state_change(self, new_state: dict):
        """Called by monitor thread when state changes."""
        for btn in self.config.buttons:
            if btn.type != "monitor":
                continue
            status = self._get_monitor_status(btn, new_state)
            if status:
                self._render_button(btn, status)

    def _get_monitor_status(self, btn: ButtonConfig, state: dict) -> str | None:
        """Map a monitor button to its current status."""
        m = btn.monitor
        if m == "git_status":
            return state.get("git", "unknown")
        if m == "pipeline_state":
            return state.get("pipeline", "idle")
        if m == "claude_session_1":
            count = state.get("claude_count", 0)
            return "active" if count >= 1 else "none"
        if m == "claude_session_2":
            count = state.get("claude_count", 0)
            return "active" if count >= 2 else "none"
        if m == "test_status":
            return state.get("test_status", "unknown")
        if m == "build_status":
            return state.get("build_status", "unknown")
        if m == "cpu_load":
            cpu = state.get("cpu", 0)
            if cpu > 80:
                return "error"
            if cpu > 50:
                return "warning"
            return "clean"
        if m == "disk_free":
            free = state.get("disk_free_pct", 100)
            if free < 10:
                return "error"
            if free < 25:
                return "warning"
            return "clean"
        return "unknown"

    def _on_key_change(self, deck, key: int, pressed: bool):
        """Handle physical button press."""
        if not pressed:
            return

        btn = self.button_map.get(key)
        if not btn:
            return

        if self.verbose:
            print(f"Button {key} pressed: {btn.label} ({btn.type})")

        if btn.type == "action":
            self._handle_action(btn)
        elif btn.type == "monitor":
            # Monitor buttons: press to show detail (future)
            pass

    def _handle_action(self, btn: ButtonConfig):
        """Execute a button action."""
        tmux_target = f"{self.config.tmux.session}:{self.config.tmux.default_pane}"

        if btn.action == "tmux_send":
            cmd = btn.command or ""
            target = btn.target or tmux_target
            tmux_send(target=target, command=cmd)

        elif btn.action == "claude_p":
            prompt = btn.prompt or btn.command or ""
            claude_p(prompt=prompt, allowed_tools=btn.allowed_tools)

        elif btn.action == "shell":
            if btn.command:
                shell_exec(btn.command)

        elif btn.action == "tmux_select":
            tmux_select(pane=btn.pane or "0")

        elif btn.action == "brightness":
            self.brightness_level = (self.brightness_level + 1) % len(self.brightness_levels)
            self.deck.set_brightness(self.brightness_levels[self.brightness_level])

        elif btn.action == "exit":
            self.stop()
            sys.exit(0)


def main():
    parser = argparse.ArgumentParser(description="Stream Deck Claude daemon")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config not found: {config_path}")
        sys.exit(1)

    config = load_config(config_path)

    deck = find_deck()
    if deck is None:
        print("No Stream Deck found. Is it plugged in?")
        sys.exit(1)

    app = StreamDeckClaude(config=config, deck=deck, verbose=args.verbose)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    app.start()

    try:
        # Block main thread
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        app.stop()
        print("Done.")


if __name__ == "__main__":
    main()
