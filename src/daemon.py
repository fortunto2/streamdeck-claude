# src/daemon.py
"""Stream Deck Claude — main daemon."""

import argparse
import os
import sys
import threading
from pathlib import Path

from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

from src.actions import claude_p, shell_exec, tmux_select, tmux_send, tmux_switch
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
        # Dynamic tmux session buttons: positions 16-23
        self.tmux_session_range = range(16, 24)
        self.tmux_sessions: list[dict] = []  # live session list

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
        # Update static monitor buttons
        for btn in self.config.buttons:
            if btn.type != "monitor":
                continue
            # Pipeline button gets special rich rendering
            if btn.monitor == "pipeline_state":
                self._render_pipeline(btn, new_state.get("pipeline_detail"))
                continue
            status = self._get_monitor_status(btn, new_state)
            if status:
                self._render_button(btn, status)

        # Update dynamic tmux session buttons (row 3)
        sessions = new_state.get("tmux_sessions", [])
        self._render_tmux_sessions(sessions)

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

    def _render_pipeline(self, btn: ButtonConfig, detail: dict | None):
        """Render pipeline button with rich progress info."""
        if not detail:
            self._render_button(btn, "idle")
            return

        project = detail["project"]
        current = detail["current_stage"]
        done = detail["done_count"]
        total = detail["total"]
        iteration = detail["iteration"]

        # Color based on state
        if current == "done":
            bg = "#22c55e"  # green — all stages done
        else:
            bg = "#3b82f6"  # blue — running

        # Build progress bar: ████░░ 4/6
        filled = int(done / total * 6) if total > 0 else 0
        bar = "\u2588" * filled + "\u2591" * (6 - filled)

        # Truncate project name
        if len(project) > 10:
            project = project[:9] + "\u2026"

        icon_path = None
        if btn.icon:
            p = Path(__file__).parent.parent / "assets" / btn.icon
            if p.exists():
                icon_path = str(p)

        # Render custom image with progress
        from src.renderer import render_button as _rb
        from PIL import ImageDraw, ImageFont
        img = _rb(size=(96, 96), label=None, bg_color=bg, icon_path=icon_path)
        draw = ImageDraw.Draw(img)

        try:
            font_sm = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 11)
            font_xs = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 9)
        except OSError:
            font_sm = ImageFont.load_default()
            font_xs = font_sm

        # Line 1: project name
        draw.text((48, 58), project, font=font_sm, fill="white", anchor="mt")
        # Line 2: current stage
        draw.text((48, 72), current, font=font_sm, fill="#dddddd", anchor="mt")
        # Line 3: progress bar + iter
        draw.text((48, 86), f"{bar} {done}/{total} i{iteration}", font=font_xs, fill="#aaaaaa", anchor="mt")

        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(btn.pos, native)

    def _render_tmux_sessions(self, sessions: list[dict]):
        """Render dynamic tmux session buttons on row 3."""
        self.tmux_sessions = sessions
        for i, pos in enumerate(self.tmux_session_range):
            if i < len(sessions):
                sess = sessions[i]
                name = sess["name"]
                cmd = sess["command"]
                attached = sess["attached"]
                # Truncate long names
                if len(name) > 10:
                    name = name[:9] + "…"
                # Color: green=attached, blue=has activity, gray=idle
                bg = "#22c55e" if attached else "#3b82f6"
                # Show session name + command
                label = f"{name}\n{cmd}"
                icon_path = str(Path(__file__).parent.parent / "assets" / "tmux.png")
                if not Path(icon_path).exists():
                    icon_path = None
                img = render_button(size=(96, 96), label=label, bg_color=bg, icon_path=icon_path)
                native = PILHelper.to_native_key_format(self.deck, img)
                with self.deck:
                    self.deck.set_key_image(pos, native)
            else:
                # Clear unused session slots
                img = render_button(size=(96, 96), label=None, bg_color="#111111")
                native = PILHelper.to_native_key_format(self.deck, img)
                with self.deck:
                    self.deck.set_key_image(pos, native)

    def _on_key_change(self, deck, key: int, pressed: bool):
        """Handle physical button press."""
        if not pressed:
            return

        # Dynamic tmux session buttons
        if key in self.tmux_session_range:
            idx = key - self.tmux_session_range.start
            if idx < len(self.tmux_sessions):
                sess = self.tmux_sessions[idx]
                if self.verbose:
                    print(f"Button {key} pressed: switch to tmux '{sess['name']}'")
                tmux_switch(sess["name"])
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
        # Dynamic default: use first attached tmux session, fallback to config
        if self.tmux_sessions:
            attached = [s for s in self.tmux_sessions if s["attached"]]
            default_session = attached[0]["name"] if attached else self.tmux_sessions[0]["name"]
        else:
            default_session = self.config.tmux.session
        tmux_target = f"{default_session}:{self.config.tmux.default_pane}"

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
