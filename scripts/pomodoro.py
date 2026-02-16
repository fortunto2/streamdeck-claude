"""Pomodoro timer for Stream Deck dashboard.

4-key timer with Silicon Valley voice pack:
  Key 0: Timer display (countdown / pause / resume / reset)
  Key 1: 15 min quick start
  Key 2: 30 min quick start
  Key 3: 60 min quick start

Background health reminders (stand up, drink water) every 30 min.
"""

import json
import os
import random
import threading
import time

from PIL import Image, ImageDraw, ImageFont

import sound_engine

# ── config ────────────────────────────────────────────────────────────

SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
DURATIONS = [15, 30, 60]  # minutes
HEALTH_INTERVAL = 30 * 60  # 30 min between health reminders
BREAK_DURATION = 5 * 60  # 5 min break
STATE_FILE = os.path.expanduser("~/.streamdeck-arcade/pomodoro_state.json")

# Silicon Valley sound pack
SV_PACK = os.path.expanduser("~/.openpeon/packs/silicon_valley/sounds")

# Sound mappings — pick random from list each time
SOUNDS = {
    "start": ["ya_reshil.mp3", "16_poshel_poshel.mp3", "13_dobro_pozhalovat.mp3"],
    "done": ["06_potryasayushche.mp3", "08_otlichno_otlichno.mp3", "zaebs_short.mp3", "pobeditel.mp3"],
    "pause": ["ne_seychas.mp3", "ne_po_sebe.mp3"],
    "resume": ["15_vot_imenno.mp3", "20_chertovski_prav.mp3"],
    "break_start": ["09_ya_vernus.mp3", "progloti_gordost.mp3"],
    "break_done": ["16_poshel_poshel.mp3", "rok_zvezda.mp3", "ThatsWhatIDo.mp3"],
    "health": ["chto_proishodit.mp3", "05_nu_vot_opyat.mp3", "17_polozhenie_hrenovoe.mp3"],
}


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def _play(category: str):
    """Play random sound from category."""
    files = SOUNDS.get(category, [])
    if not files:
        return
    path = os.path.join(SV_PACK, random.choice(files))
    if os.path.exists(path):
        sound_engine.play_voice(path)


# ── states ────────────────────────────────────────────────────────────

IDLE = "idle"
RUNNING = "running"
PAUSED = "paused"
DONE = "done"
BREAK = "break"


# ── pomodoro engine ──────────────────────────────────────────────────

class Pomodoro:
    """Pomodoro timer engine with 4-key rendering."""

    def __init__(self, set_key_fn):
        """set_key_fn(pos, img) — callback to set a key image on the deck."""
        self.set_key = set_key_fn
        self.state = IDLE
        self.duration = 25 * 60  # default 25 min (seconds)
        self.remaining = 0
        self.selected_min = 25  # visual: which button is "active"
        self.sessions_today = 0
        self.total_focus_today = 0  # seconds
        self.lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._health_timer: threading.Timer | None = None
        self._last_health = time.time()
        self._flash_on = True  # for done flashing
        self.running = False

        # Key positions on the deck (set by dashboard)
        self.keys = [24, 25, 26, 27]

        # Restore persisted state
        self._load_state()

    def _load_state(self):
        """Restore sessions/focus from disk (survives restart)."""
        try:
            with open(STATE_FILE) as f:
                data = json.load(f)
            # Only restore if same day
            if data.get("date") == time.strftime("%Y-%m-%d"):
                self.sessions_today = data.get("sessions", 0)
                self.total_focus_today = data.get("total_focus", 0)
                self.selected_min = data.get("selected_min", 25)
        except Exception:
            pass

    def _save_state(self):
        """Persist sessions/focus to disk."""
        try:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            data = {
                "date": time.strftime("%Y-%m-%d"),
                "sessions": self.sessions_today,
                "total_focus": self.total_focus_today,
                "selected_min": self.selected_min,
            }
            with open(STATE_FILE, "w") as f:
                json.dump(data, f)
        except Exception:
            pass

    def start(self):
        """Start the pomodoro system (called by dashboard on show_home)."""
        self.running = True
        self._last_health = time.time()
        self._schedule_health()
        self.render_all()

    def stop(self):
        """Stop everything (called on page switch / shutdown)."""
        self.running = False
        self._cancel_timer()
        self._cancel_health()

    # ── timer control ────────────────────────────────────────────────

    def start_focus(self, minutes: int):
        """Start a focus timer for given minutes."""
        with self.lock:
            self._cancel_timer()
            self.selected_min = minutes
            self.duration = minutes * 60
            self.remaining = self.duration
            self.state = RUNNING
        self._save_state()
        _play("start")
        self._schedule_tick()
        self.render_all()

    def pause(self):
        with self.lock:
            if self.state != RUNNING:
                return
            self._cancel_timer()
            self.state = PAUSED
        _play("pause")
        self.render_all()

    def resume(self):
        with self.lock:
            if self.state != PAUSED:
                return
            self.state = RUNNING
        _play("resume")
        self._schedule_tick()
        self.render_all()

    def reset(self):
        with self.lock:
            self._cancel_timer()
            self.state = IDLE
            self.remaining = 0
        self.render_all()

    def start_break(self):
        with self.lock:
            self._cancel_timer()
            self.remaining = BREAK_DURATION
            self.state = BREAK
        _play("break_start")
        self._schedule_tick()
        self.render_all()

    # ── tick ──────────────────────────────────────────────────────────

    def _schedule_tick(self):
        if not self.running:
            return
        self._timer = threading.Timer(1.0, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _tick(self):
        if not self.running:
            return

        with self.lock:
            if self.state == RUNNING:
                self.remaining -= 1
                if self.remaining <= 0:
                    self.remaining = 0
                    self.state = DONE
                    self.sessions_today += 1
                    self.total_focus_today += self.duration
                    self._last_health = time.time()  # reset health timer
                    self._save_state()
            elif self.state == BREAK:
                self.remaining -= 1
                if self.remaining <= 0:
                    self.remaining = 0
                    self.state = IDLE

            state = self.state

        if state == DONE:
            _play("done")
            self._start_flash()
        elif state == IDLE and self.sessions_today > 0:
            # Break just ended
            _play("break_done")
            self.render_all()
        else:
            self._schedule_tick()
            self._render_timer_key()

    # ── flash animation for DONE ──────────────────────────────────────

    def _start_flash(self):
        if not self.running:
            return
        with self.lock:
            if self.state != DONE:
                return
        self._flash_on = not self._flash_on
        self._render_timer_key()
        self.render_duration_keys()
        # Flash for 30 seconds, then stay lit
        self._timer = threading.Timer(0.8, self._flash_tick)
        self._timer.daemon = True
        self._timer.start()

    def _flash_tick(self):
        if not self.running:
            return
        with self.lock:
            if self.state != DONE:
                return
        self._flash_on = not self._flash_on
        self._render_timer_key()
        self._timer = threading.Timer(0.8, self._flash_tick)
        self._timer.daemon = True
        self._timer.start()

    # ── health reminder ──────────────────────────────────────────────

    def _schedule_health(self):
        if not self.running:
            return
        self._health_timer = threading.Timer(60, self._health_check)
        self._health_timer.daemon = True
        self._health_timer.start()

    def _cancel_health(self):
        if self._health_timer:
            self._health_timer.cancel()
            self._health_timer = None

    def _health_check(self):
        if not self.running:
            return
        now = time.time()
        elapsed = now - self._last_health
        if elapsed >= HEALTH_INTERVAL:
            _play("health")
            self._last_health = now
        self._schedule_health()

    # ── key handler ──────────────────────────────────────────────────

    def on_key(self, key: int):
        """Handle key press. key is the deck position."""
        if key == self.keys[0]:
            # Timer key — pause/resume/reset/break
            with self.lock:
                state = self.state
            if state == RUNNING:
                self.pause()
            elif state == PAUSED:
                self.resume()
            elif state == DONE:
                self.start_break()
            elif state == BREAK:
                # Tap during break → skip break
                with self.lock:
                    self._cancel_timer()
                    self.remaining = 0
                    self.state = IDLE
                self.render_all()
            elif state == IDLE:
                # Start with last selected duration
                self.start_focus(self.selected_min)

        elif key == self.keys[1]:
            self.start_focus(15)
        elif key == self.keys[2]:
            self.start_focus(30)
        elif key == self.keys[3]:
            self.start_focus(60)

    # ── rendering ────────────────────────────────────────────────────

    def render_all(self):
        self._render_timer_key()
        self.render_duration_keys()

    def _render_timer_key(self):
        with self.lock:
            state = self.state
            remaining = self.remaining
            sessions = self.sessions_today
            total = self.total_focus_today
            flash = self._flash_on

        img = Image.new("RGB", SIZE, "#000000")
        d = ImageDraw.Draw(img)

        if state == IDLE:
            # Show session stats or "READY"
            if sessions > 0:
                d.text((48, 12), "FOCUS", font=_font(11), fill="#64748b", anchor="mm")
                total_min = total // 60
                d.text((48, 36), f"{sessions}x", font=_font(24), fill="#4ade80", anchor="mm")
                d.text((48, 60), f"{total_min}min", font=_font(14), fill="#94a3b8", anchor="mm")
                d.text((48, 82), "TAP START", font=_font(9), fill="#475569", anchor="mm")
            else:
                d.text((48, 30), "FOCUS", font=_font(16), fill="#94a3b8", anchor="mm")
                d.text((48, 55), "TIMER", font=_font(16), fill="#64748b", anchor="mm")
                d.text((48, 80), "TAP START", font=_font(9), fill="#475569", anchor="mm")

        elif state == RUNNING:
            mins, secs = divmod(remaining, 60)
            # Progress bar
            progress = 1.0 - (remaining / max(self.duration, 1))
            bar_w = int(80 * progress)
            d.rectangle([8, 82, 88, 90], fill="#1e293b")
            if bar_w > 0:
                d.rectangle([8, 82, 8 + bar_w, 90], fill="#22c55e")
            # Time
            d.text((48, 14), "FOCUS", font=_font(10), fill="#22c55e", anchor="mm")
            d.text((48, 48), f"{mins:02d}:{secs:02d}", font=_font(28), fill="white", anchor="mm")
            d.text((48, 72), "tap=pause", font=_font(9), fill="#475569", anchor="mm")

        elif state == PAUSED:
            mins, secs = divmod(remaining, 60)
            d.text((48, 14), "PAUSED", font=_font(11), fill="#eab308", anchor="mm")
            d.text((48, 48), f"{mins:02d}:{secs:02d}", font=_font(28), fill="#fbbf24", anchor="mm")
            d.text((48, 72), "tap=resume", font=_font(9), fill="#475569", anchor="mm")

        elif state == DONE:
            if flash:
                img = Image.new("RGB", SIZE, "#991b1b")
                d = ImageDraw.Draw(img)
                d.text((48, 30), "DONE!", font=_font(22), fill="white", anchor="mm")
                d.text((48, 58), f"#{sessions}", font=_font(16), fill="#fbbf24", anchor="mm")
                d.text((48, 80), "tap=break", font=_font(9), fill="#fca5a5", anchor="mm")
            else:
                d.text((48, 40), "DONE!", font=_font(20), fill="#7f1d1d", anchor="mm")
                d.text((48, 70), "tap=break", font=_font(9), fill="#374151", anchor="mm")

        elif state == BREAK:
            mins, secs = divmod(remaining, 60)
            img = Image.new("RGB", SIZE, "#0c4a6e")
            d = ImageDraw.Draw(img)
            d.text((48, 12), "BREAK", font=_font(11), fill="#7dd3fc", anchor="mm")
            d.text((48, 42), f"{mins:02d}:{secs:02d}", font=_font(28), fill="white", anchor="mm")
            d.text((48, 68), "STAND UP", font=_font(10), fill="#38bdf8", anchor="mm")
            d.text((48, 82), "DRINK WATER", font=_font(9), fill="#38bdf8", anchor="mm")

        self.set_key(self.keys[0], img)

    def render_duration_keys(self):
        with self.lock:
            state = self.state
            selected = self.selected_min
            flash = self._flash_on

        for i, minutes in enumerate(DURATIONS):
            key_pos = self.keys[i + 1]
            is_active = (state in (RUNNING, PAUSED) and selected == minutes)
            is_done = (state == DONE)

            img = Image.new("RGB", SIZE, "#000000")
            d = ImageDraw.Draw(img)

            if is_done and flash:
                # Celebration flash
                bg_color = "#052e16"
                img = Image.new("RGB", SIZE, bg_color)
                d = ImageDraw.Draw(img)
                d.text((48, 38), str(minutes), font=_font(30), fill="#4ade80", anchor="mm")
                d.text((48, 68), "MIN", font=_font(10), fill="#86efac", anchor="mm")
            elif is_active:
                # Active — green glow
                img = Image.new("RGB", SIZE, "#052e16")
                d = ImageDraw.Draw(img)
                d.text((48, 38), str(minutes), font=_font(30), fill="#4ade80", anchor="mm")
                d.text((48, 68), "MIN", font=_font(10), fill="#86efac", anchor="mm")
                # Pulsing border
                d.rectangle([2, 2, 93, 93], outline="#22c55e", width=2)
            elif state in (RUNNING, PAUSED, BREAK):
                # Dimmed — other durations while timer active
                d.text((48, 38), str(minutes), font=_font(26), fill="#374151", anchor="mm")
                d.text((48, 68), "MIN", font=_font(9), fill="#1f2937", anchor="mm")
            else:
                # Idle — ready to tap
                d.text((48, 38), str(minutes), font=_font(30), fill="#e2e8f0", anchor="mm")
                d.text((48, 68), "MIN", font=_font(10), fill="#64748b", anchor="mm")

            self.set_key(key_pos, img)
