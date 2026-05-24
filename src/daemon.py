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
from src.renderer import render_button, render_text_button, status_to_color

# Music-production layer — REAPER OSC + virtual MIDI port. Imports are
# guarded so the daemon still starts if the music deps aren't installed
# (e.g. CI image without rtmidi system headers).
try:
    from src.reaper import ReaperClient
except Exception:  # pragma: no cover
    ReaperClient = None  # type: ignore[assignment]
try:
    from src.midi_out import MidiOut
except Exception:  # pragma: no cover
    MidiOut = None  # type: ignore[assignment]


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
        # ---- Pages ------------------------------------------------
        # Legacy mode: config has flat `buttons` list, no `pages` dict.
        # New mode: config has `pages`, the active page is `current_page`.
        # Either way, `self.pages` always has at least one entry called
        # `default_page` so the rest of the daemon can stay page-agnostic.
        if config.pages:
            self.pages = {name: list(btns) for name, btns in config.pages.items()}
        else:
            self.pages = {config.default_page: list(config.buttons)}
        self.current_page = config.default_page
        if self.current_page not in self.pages:
            # Fall back to the first defined page if default is missing.
            self.current_page = next(iter(self.pages.keys()))
        self.button_map: dict[int, ButtonConfig] = {}
        self.brightness_level = 0
        self.brightness_levels = [30, 70, 100]
        # Dynamic tmux session buttons (dashboard page only): 16-23.
        self.tmux_session_range = range(16, 24)
        self.tmux_sessions: list[dict] = []
        # Lazy music-production clients — created on first need so the
        # daemon still starts when REAPER isn't running.
        self.reaper: ReaperClient | None = None
        self.midi: MidiOut | None = None

    def start(self):
        """Initialize deck and start monitoring."""
        self.deck.open()
        self.deck.reset()
        self.deck.set_brightness(self.config.deck.brightness)

        # Open REAPER OSC at start if enabled. Failures are non-fatal —
        # daemon still runs other pages if REAPER isn't listening.
        if self.config.reaper.enabled and self.config.reaper.auto_connect:
            self._connect_reaper()

        self._render_current_page()

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
        if self.reaper is not None:
            try:
                self.reaper.stop_listening()
            except Exception:
                pass
        if self.midi is not None:
            try:
                self.midi.all_notes_off()
                self.midi.close()
            except Exception:
                pass
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
        # Update monitor buttons with big text
        for btn in self.config.buttons:
            if btn.type != "monitor":
                continue
            if btn.monitor == "pipeline_state":
                self._render_pipeline(btn, new_state.get("pipeline_detail"))
                continue
            self._render_monitor_text(btn, new_state)

        # Update dynamic tmux session buttons (row 3)
        sessions = new_state.get("tmux_sessions", [])
        self._render_tmux_sessions(sessions)

    def _render_monitor_text(self, btn: ButtonConfig, state: dict):
        """Render monitor button as big readable text, no icon."""
        m = btn.monitor
        label = btn.label or ""

        if m == "git_status":
            git = state.get("git", "unknown")
            bg = status_to_color(git)
            img = render_text_button(
                lines=[label, git.upper()],
                bg_color=bg,
                font_sizes=[14, 22],
                colors=["#dddddd", "#ffffff"],
            )
        elif m in ("claude_session_1", "claude_session_2"):
            count = state.get("claude_count", 0)
            needed = 1 if m == "claude_session_1" else 2
            active = count >= needed
            bg = "#22c55e" if active else "#6b7280"
            img = render_text_button(
                lines=[label, str(count)],
                bg_color=bg,
                font_sizes=[12, 28],
                colors=["#dddddd", "#ffffff"],
            )
        elif m == "cpu_load":
            cpu = state.get("cpu", 0)
            bg = "#ef4444" if cpu > 80 else "#eab308" if cpu > 50 else "#22c55e"
            img = render_text_button(
                lines=[label, f"{cpu:.0f}%"],
                bg_color=bg,
                font_sizes=[12, 28],
                colors=["#dddddd", "#ffffff"],
            )
        elif m == "disk_free":
            free = state.get("disk_free_pct", 100)
            bg = "#ef4444" if free < 10 else "#eab308" if free < 25 else "#22c55e"
            img = render_text_button(
                lines=[label, f"{free:.0f}%"],
                bg_color=bg,
                font_sizes=[12, 28],
                colors=["#dddddd", "#ffffff"],
            )
        elif m == "test_status":
            ts = state.get("test_status", "unknown")
            bg = status_to_color(ts)
            img = render_text_button(
                lines=[label, ts.upper()],
                bg_color=bg,
                font_sizes=[14, 18],
                colors=["#dddddd", "#ffffff"],
            )
        elif m == "build_status":
            bs = state.get("build_status", "unknown")
            bg = status_to_color(bs)
            img = render_text_button(
                lines=[label, bs.upper()],
                bg_color=bg,
                font_sizes=[14, 18],
                colors=["#dddddd", "#ffffff"],
            )
        else:
            bg = "#6b7280"
            img = render_text_button(lines=[label, "?"], bg_color=bg)

        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(btn.pos, native)

    def _render_pipeline(self, btn: ButtonConfig, detail: dict | None):
        """Render pipeline button with big text progress."""
        if not detail:
            img = render_text_button(
                lines=["Pipeline", "IDLE"],
                bg_color="#6b7280",
                font_sizes=[12, 20],
            )
            native = PILHelper.to_native_key_format(self.deck, img)
            with self.deck:
                self.deck.set_key_image(btn.pos, native)
            return

        project = detail["project"]
        current = detail["current_stage"]
        done = detail["done_count"]
        total = detail["total"]
        iteration = detail["iteration"]

        if current == "done":
            bg = "#22c55e"
        else:
            bg = "#3b82f6"

        # Truncate project name
        if len(project) > 11:
            project = project[:10] + "\u2026"

        # Progress bar
        filled = int(done / total * 6) if total > 0 else 0
        bar = "\u2588" * filled + "\u2591" * (6 - filled)

        img = render_text_button(
            lines=[project, current, f"{bar} {done}/{total}", f"iter {iteration}"],
            bg_color=bg,
            font_sizes=[12, 18, 11, 10],
            colors=["#dddddd", "#ffffff", "#cccccc", "#aaaaaa"],
        )

        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(btn.pos, native)

    def _render_tmux_sessions(self, sessions: list[dict]):
        """Render dynamic tmux session buttons on row 3 — big text."""
        self.tmux_sessions = sessions
        for i, pos in enumerate(self.tmux_session_range):
            if i < len(sessions):
                sess = sessions[i]
                name = sess["name"]
                cmd = sess["command"]
                attached = sess["attached"]
                # Truncate long names
                if len(name) > 11:
                    name = name[:10] + "\u2026"
                bg = "#22c55e" if attached else "#3b82f6"
                img = render_text_button(
                    lines=[name, cmd[:10]],
                    bg_color=bg,
                    font_sizes=[14, 18],
                    colors=["#dddddd", "#ffffff"],
                )
            else:
                img = render_text_button(bg_color="#111111")
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
        elif btn.type == "page":
            if btn.page:
                self._switch_page(btn.page)
        elif btn.type == "reaper":
            self._handle_reaper(btn)
        elif btn.type == "midi":
            self._handle_midi(btn)
        elif btn.type == "drum_step":
            # TODO: wire to DrumSequencer once the Drum page lands.
            if self.verbose:
                print(f"  drum_step: voice={btn.drum_voice} step={btn.drum_step}")

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

    # ── Page switching ─────────────────────────────────────────────

    def _render_current_page(self) -> None:
        """Re-render every button on the active page."""
        active = self.pages.get(self.current_page, [])
        self.button_map = {btn.pos: btn for btn in active}
        # Blank out everything first so leftovers from the previous
        # page don't stay visible on positions the new page doesn't use.
        blank_img = render_text_button(bg_color="#000000")
        native_blank = PILHelper.to_native_key_format(self.deck, blank_img)
        with self.deck:
            for k in range(self.deck.key_count()):
                self.deck.set_key_image(k, native_blank)
        for btn in active:
            self._render_button(btn)

    def _switch_page(self, page_name: str) -> None:
        """Jump to another named page. Re-renders all buttons."""
        if page_name not in self.pages:
            print(f"  unknown page: {page_name} (have: {list(self.pages)})")
            return
        if self.verbose:
            print(f"  → page: {page_name}")
        self.current_page = page_name
        self._render_current_page()

    # ── REAPER (lazy) ─────────────────────────────────────────────

    def _connect_reaper(self) -> ReaperClient | None:
        if self.reaper is not None:
            return self.reaper
        if ReaperClient is None:
            print("  reaper: python-osc not installed — skipping")
            return None
        rc_cfg = self.config.reaper
        try:
            self.reaper = ReaperClient(
                send_host=rc_cfg.send_host,
                send_port=rc_cfg.send_port,
                listen_host=rc_cfg.listen_host,
                listen_port=rc_cfg.listen_port,
            )
            self.reaper.start_listening()
            if self.verbose:
                print(
                    f"  reaper: sending → {rc_cfg.send_host}:{rc_cfg.send_port}, "
                    f"listening on {rc_cfg.listen_host}:{rc_cfg.listen_port}"
                )
        except Exception as e:
            print(f"  reaper: connect failed — {e}")
            self.reaper = None
        return self.reaper

    def _handle_reaper(self, btn: ButtonConfig) -> None:
        rc = self._connect_reaper()
        if rc is None or not btn.reaper_method:
            return
        method = getattr(rc, btn.reaper_method, None)
        if method is None:
            print(f"  reaper: unknown method {btn.reaper_method!r}")
            return
        try:
            method(**(btn.reaper_args or {}))
        except Exception as e:
            print(f"  reaper: {btn.reaper_method} failed — {e}")

    # ── MIDI (lazy) ───────────────────────────────────────────────

    def _connect_midi(self) -> MidiOut | None:
        if self.midi is not None:
            return self.midi
        if MidiOut is None:
            print("  midi: python-rtmidi not installed — skipping")
            return None
        try:
            self.midi = MidiOut()
            if self.verbose:
                print("  midi: virtual port 'StreamDeck' opened")
        except Exception as e:
            print(f"  midi: open failed — {e}")
            self.midi = None
        return self.midi

    def _handle_midi(self, btn: ButtonConfig) -> None:
        mo = self._connect_midi()
        if mo is None or btn.midi_note is None:
            return
        try:
            mo.note_on(btn.midi_note, btn.midi_velocity, btn.midi_channel)
            # Brief auto-off — Stream Deck buttons aren't pressure-sensitive.
            threading.Timer(
                0.15,
                lambda: mo.note_off(btn.midi_note, btn.midi_channel),
            ).start()
        except Exception as e:
            print(f"  midi: note_on {btn.midi_note} failed — {e}")


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
