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
try:
    from src.drum_seq import DrumSequencer, VOICES, N_STEPS
except Exception:  # pragma: no cover
    DrumSequencer = None  # type: ignore[assignment]
    VOICES = ()  # type: ignore[assignment]
    N_STEPS = 16


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
        self.drum = None  # DrumSequencer, instantiated below
        # Map: track_idx → list of button positions on the active page
        # that mirror that track's mute / solo / arm state. Filled on
        # page-render; used by REAPER feedback handler.
        self._mute_btns: dict[int, list[int]] = {}
        self._solo_btns: dict[int, list[int]] = {}
        self._arm_btns: dict[int, list[int]] = {}
        # Transport buttons get a state-aware repaint when /play /repeat
        # state changes.
        self._play_btns: list[int] = []
        self._loop_btns: list[int] = []
        self._record_btns: list[int] = []
        # Drum step buttons indexed by step number for playhead colouring.
        self._drum_step_btns: dict[int, int] = {}

    def start(self):
        """Initialize deck and start monitoring."""
        self.deck.open()
        self.deck.reset()
        self.deck.set_brightness(self.config.deck.brightness)

        # Eagerly open the music-production clients if any page needs
        # them — this way REAPER's MIDI Devices preferences only have
        # to be enabled once (the virtual port stays open for the
        # whole daemon lifetime instead of appearing on first press).
        needs_midi = any(
            btn.type in ("midi", "drum_step")
            for page in self.pages.values()
            for btn in page
        )
        if needs_midi:
            self._connect_midi()
            self._connect_drum()
        if self.config.reaper.enabled and self.config.reaper.auto_connect:
            self._connect_reaper()
            if self.reaper is not None:
                self.reaper._on_change = self._on_reaper_state
                # Ask REAPER to push a fresh snapshot of every track
                # state so button LEDs reflect the project on startup.
                self.reaper.action(40769)  # Track: Unselect all tracks (cheap no-op refresh)

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
        """Called by monitor thread when state changes.

        Only repaints if the dashboard page is active — when the user
        is on the REAPER / MIDI / Drum page, those buttons own positions
        16-23 and a 3-second monitor tick mustn't blank them grey.
        """
        if self.current_page != "dashboard":
            return
        for btn in self.config.buttons:
            if btn.type != "monitor":
                continue
            if btn.monitor == "pipeline_state":
                self._render_pipeline(btn, new_state.get("pipeline_detail"))
                continue
            self._render_monitor_text(btn, new_state)
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
        """Handle physical button press or release."""
        # MIDI buttons want both edges — hold a key, hold the note. The
        # rest of the buttons fire once on press only.
        btn = self.button_map.get(key)
        if btn is not None and btn.type == "midi":
            self._handle_midi(btn, on=pressed)
            return
        if not pressed:
            return
        # Legacy dashboard: dynamic tmux session buttons live on 16-23.
        # Skip this branch on any other page so REAPER / MIDI / Drum
        # buttons at those positions get their normal handler.
        if self.current_page == "dashboard" and key in self.tmux_session_range:
            idx = key - self.tmux_session_range.start
            if idx < len(self.tmux_sessions):
                sess = self.tmux_sessions[idx]
                if self.verbose:
                    print(f"Button {key} pressed: switch to tmux '{sess['name']}'")
                tmux_switch(sess["name"])
            return

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
            self._handle_drum_step(btn)
        elif btn.type == "drum_action":
            self._handle_drum_action(btn)

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
        # Build the reverse indices the feedback handlers use to map
        # REAPER state changes back to button positions.
        self._mute_btns.clear()
        self._solo_btns.clear()
        self._arm_btns.clear()
        self._play_btns = []
        self._loop_btns = []
        self._record_btns = []
        self._drum_step_btns = {}
        for btn in active:
            if btn.type == "reaper" and btn.reaper_method == "track_mute":
                track = (btn.reaper_args or {}).get("track")
                if isinstance(track, int):
                    self._mute_btns.setdefault(track, []).append(btn.pos)
            elif btn.type == "reaper" and btn.reaper_method == "track_solo":
                track = (btn.reaper_args or {}).get("track")
                if isinstance(track, int):
                    self._solo_btns.setdefault(track, []).append(btn.pos)
            elif btn.type == "reaper" and btn.reaper_method == "track_arm":
                track = (btn.reaper_args or {}).get("track")
                if isinstance(track, int):
                    self._arm_btns.setdefault(track, []).append(btn.pos)
            elif btn.type == "reaper" and btn.reaper_method == "transport_play":
                self._play_btns.append(btn.pos)
            elif btn.type == "reaper" and btn.reaper_method == "transport_loop_toggle":
                self._loop_btns.append(btn.pos)
            elif btn.type == "reaper" and btn.reaper_method == "transport_record":
                self._record_btns.append(btn.pos)
            elif btn.type == "drum_step" and btn.drum_step is not None:
                self._drum_step_btns[btn.drum_step] = btn.pos
        # Blank out everything first so leftovers from the previous
        # page don't stay visible on positions the new page doesn't use.
        blank_img = render_text_button(bg_color="#000000")
        native_blank = PILHelper.to_native_key_format(self.deck, blank_img)
        with self.deck:
            for k in range(self.deck.key_count()):
                self.deck.set_key_image(k, native_blank)
        for btn in active:
            # Drum step buttons get their own render path that knows
            # about armed/playhead state — call it instead of the
            # generic render_button.
            if btn.type == "drum_step":
                voice = self.drum.selected_voice if self.drum else "kick"
                self._render_drum_step(btn, voice=voice, is_playhead=False)
            else:
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
        # Env vars override the yaml — useful when REAPER's Local IP
        # changes (different Wi-Fi, Tailscale up/down) and you don't
        # want to edit the config each time.
        send_host = os.environ.get("REAPER_OSC_SEND_HOST", rc_cfg.send_host)
        send_port = int(os.environ.get("REAPER_OSC_SEND_PORT", rc_cfg.send_port))
        listen_host = os.environ.get("REAPER_OSC_LISTEN_HOST", rc_cfg.listen_host)
        listen_port = int(os.environ.get("REAPER_OSC_LISTEN_PORT", rc_cfg.listen_port))
        try:
            self.reaper = ReaperClient(
                send_host=send_host,
                send_port=send_port,
                listen_host=listen_host,
                listen_port=listen_port,
            )
            self.reaper.start_listening()
            if self.verbose:
                hosts = ", ".join(self.reaper._hosts)
                print(
                    f"  reaper: sending → [{hosts}]:{send_port}, "
                    f"listening on {listen_host}:{listen_port}"
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
                kind = self.midi.opened_kind
                print(f"  midi: {kind} port '{self.midi.opened_name}' opened")
                if kind == "iac":
                    print("       (IAC Driver — survives daemon restart; "
                          "enable as MIDI input in REAPER once)")
        except Exception as e:
            print(f"  midi: open failed — {e}")
            self.midi = None
        return self.midi

    def _handle_midi(self, btn: ButtonConfig, on: bool = True) -> None:
        """Send NoteOn on press, NoteOff on release.

        Held-key behaviour: Stream Deck fires `set_key_callback` with
        pressed=True when you push the button and pressed=False when
        you let go. Mapping that directly to NoteOn / NoteOff means
        a held button = a held note — sustained chords, long pad
        notes, drum rolls.
        """
        mo = self._connect_midi()
        if mo is None or btn.midi_note is None:
            return
        try:
            if on:
                mo.note_on(btn.midi_note, btn.midi_velocity, btn.midi_channel)
            else:
                mo.note_off(btn.midi_note, btn.midi_channel)
        except Exception as e:
            print(f"  midi: note {'on' if on else 'off'} {btn.midi_note} failed — {e}")

    # ── Drum sequencer ────────────────────────────────────────────

    def _connect_drum(self):
        if self.drum is not None:
            return self.drum
        if DrumSequencer is None:
            return None
        mo = self._connect_midi()
        if mo is None:
            return None
        self.drum = DrumSequencer(midi=mo)
        # Repaint the active step on every tick — a live playhead so
        # the user can see where the pattern is at.
        self.drum.set_step_callback(self._on_drum_step)
        if self.verbose:
            print("  drum: sequencer ready, channel 10")
        return self.drum

    def _handle_drum_action(self, btn: ButtonConfig) -> None:
        seq = self._connect_drum()
        if seq is None or btn.drum_action is None:
            return
        act = btn.drum_action
        if act == "play":
            seq.start()
        elif act == "stop":
            seq.stop()
            self._render_current_page()  # clear playhead colouring
        elif act == "clear":
            seq.clear()
            self._render_current_page()
        elif act == "clear_voice":
            if btn.drum_voice:
                seq.pattern.clear_voice(btn.drum_voice)
                self._render_current_page()
        elif act == "select_voice":
            if btn.drum_voice:
                seq.select_voice(btn.drum_voice)
                self._render_current_page()  # repaint steps for new voice
        elif act == "set_bpm":
            if btn.drum_bpm is not None:
                seq.bpm = float(btn.drum_bpm)
        if self.verbose:
            print(f"  drum_action: {act}")

    def _handle_drum_step(self, btn: ButtonConfig) -> None:
        seq = self._connect_drum()
        if seq is None:
            return
        # If no drum_voice on the button, use the currently-selected
        # voice (Drum page voice-select buttons set it).
        voice = btn.drum_voice or seq.selected_voice
        if btn.drum_step is None:
            return
        seq.toggle_step(voice, btn.drum_step)
        if self.verbose:
            armed = seq.pattern.is_armed(voice, btn.drum_step)
            print(f"  drum_step: {voice} step {btn.drum_step} → {'on' if armed else 'off'}")
        # Repaint the step button to show armed/unarmed state.
        self._render_button(btn)

    def _on_drum_step(self, step: int) -> None:
        """Called every step tick by DrumSequencer — paints the playhead."""
        # Find the step buttons for the *current* drum voice and brighten
        # the one matching the step we just played.
        if self.drum is None:
            return
        try:
            voice = self.drum.selected_voice
            for s, pos in self._drum_step_btns.items():
                btn = self.button_map.get(pos)
                if btn is None:
                    continue
                # Make the playhead pop, dim the others.
                self._render_drum_step(btn, voice=voice, is_playhead=(s == step))
        except Exception as e:
            if self.verbose:
                print(f"  drum step paint failed: {e}")

    def _render_drum_step(self, btn: ButtonConfig, voice: str, is_playhead: bool) -> None:
        """Repaint a drum step button with armed / playhead colouring."""
        if self.drum is None:
            return
        step = btn.drum_step
        armed = self.drum.pattern.is_armed(voice, step) if step is not None else False
        if is_playhead:
            bg = "#22c55e" if armed else "#475569"  # bright green / mid grey
        else:
            bg = "#1e40af" if armed else "#0f172a"  # deep blue armed / very dark idle
        img = render_text_button(
            lines=[btn.label or f"{(step or 0) + 1}"],
            bg_color=bg,
            font_sizes=[22],
            colors=["#ffffff" if is_playhead else "#cbd5e1"],
        )
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(btn.pos, native)

    # ── REAPER state feedback ─────────────────────────────────────

    def _on_reaper_state(self, state) -> None:
        """ReaperClient feedback subscriber → repaints state-aware buttons."""
        try:
            # Transport buttons (play / loop / record).
            for pos in self._play_btns:
                btn = self.button_map.get(pos)
                if btn:
                    self._render_transport_state(btn, "play", state.playing)
            for pos in self._loop_btns:
                btn = self.button_map.get(pos)
                if btn:
                    self._render_transport_state(btn, "loop", state.looping)
            for pos in self._record_btns:
                btn = self.button_map.get(pos)
                if btn:
                    self._render_transport_state(btn, "record", state.recording)
            # Per-track mute / solo / arm.
            for track, on in state.track_mute.items():
                for pos in self._mute_btns.get(track, []):
                    btn = self.button_map.get(pos)
                    if btn:
                        self._render_track_state(btn, "mute", on)
            for track, on in state.track_solo.items():
                for pos in self._solo_btns.get(track, []):
                    btn = self.button_map.get(pos)
                    if btn:
                        self._render_track_state(btn, "solo", on)
            for track, on in state.track_arm.items():
                for pos in self._arm_btns.get(track, []):
                    btn = self.button_map.get(pos)
                    if btn:
                        self._render_track_state(btn, "arm", on)
        except Exception as e:
            if self.verbose:
                print(f"  reaper state repaint failed: {e}")

    def _render_transport_state(self, btn: ButtonConfig, kind: str, on: bool) -> None:
        if kind == "play":
            bg = "#22c55e" if on else "#1e3a5f"  # bright green when playing
        elif kind == "loop":
            bg = "#a855f7" if on else "#1e3a5f"  # purple
        else:  # record
            bg = "#ef4444" if on else "#1e3a5f"  # red
        img = render_text_button(
            lines=[btn.label or ""],
            bg_color=bg,
            font_sizes=[20],
            colors=["#ffffff"],
        )
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(btn.pos, native)

    def _render_track_state(self, btn: ButtonConfig, kind: str, on: bool) -> None:
        if not on:
            self._render_button(btn)  # back to default colour
            return
        bg = {"mute": "#ef4444", "solo": "#facc15", "arm": "#fb923c"}[kind]
        img = render_text_button(
            lines=[btn.label or ""],
            bg_color=bg,
            font_sizes=[18],
            colors=["#0f172a"],  # dark text on bright bg
        )
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(btn.pos, native)


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
