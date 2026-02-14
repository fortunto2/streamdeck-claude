"""Reaction Speed Test — Stream Deck mini-game.

Test your reflexes! Wait for the green light, then smash the button ASAP.
10 rounds, tracks best and average reaction times.

Usage:
    uv run python scripts/reaction_game.py
"""

import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

import sound_engine
import scores

# -- config ----------------------------------------------------------------
GAME_KEYS = list(range(8, 32))  # rows 2-4 = game area
HUD_KEYS = list(range(0, 8))    # row 1 = HUD
TOTAL_ROUNDS = 10
DELAY_MIN = 1.0       # min random delay before green (seconds)
DELAY_MAX = 4.0       # max random delay before green (seconds)
WINDOW_START = 2.0    # initial seconds before target disappears (round 1)
WINDOW_END = 0.8      # fastest window (round 10)
PENALTY_MS = 999      # penalty for wrong press or early press
START_KEY = 20        # center-ish button for start
SIZE = (96, 96)

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3

# -- orc voice lines (peon-ping packs) ------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
ORC_VOICES = {
    # Reaction = Duke Nukem (fast, aggressive, perfect for reflexes)
    "start": [
        "duke_nukem/sounds/DaddysHere.wav",
        "duke_nukem/sounds/KickAssChewGum.wav",
        "duke_nukem/sounds/ShowMeSomething.wav",
        "duke_nukem/sounds/WhatAreYouWaiting.wav",
    ],
    "fast_reaction": [
        "duke_nukem/sounds/Groovy.wav",
        "duke_nukem/sounds/HellYeah.wav",
        "duke_nukem/sounds/HailToTheKing.wav",
        "duke_nukem/sounds/HotDamnBaby.wav",
        "duke_nukem/sounds/KaboomBaby.wav",
    ],
    "slow_fail": [
        "duke_nukem/sounds/DamnIt.wav",
        "duke_nukem/sounds/OhShit.wav",
        "duke_nukem/sounds/SonOfABitch.wav",
        "duke_nukem/sounds/WhatTheHell.wav",
    ],
    "newbest": [
        "duke_nukem/sounds/HailToTheKing.wav",
        "duke_nukem/sounds/LegendsNeverDie.wav",
        "duke_nukem/sounds/LastOneStanding.wav",
        "duke_nukem/sounds/BallsOfSteel.wav",
    ],
}

_last_orc_time: float = 0
ORC_COOLDOWN = 4.0


def play_orc(event: str):
    """Play a random orc voice line -- with cooldown to avoid spam."""
    global _last_orc_time
    now = time.monotonic()
    if now - _last_orc_time < ORC_COOLDOWN:
        return
    paths = ORC_VOICES.get(event, [])
    if not paths:
        return
    random.shuffle(paths)
    for rel in paths:
        full = os.path.join(PEON_DIR, rel)
        if os.path.exists(full):
            _last_orc_time = now
            sound_engine.play_voice(full)
            return


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


# -- 8-bit sound engine ----------------------------------------------------
SAMPLE_RATE = 22050
_sfx_cache: dict[str, str] = {}
_sfx_dir: str = ""


def _triangle(freq: float, dur: float, vol: float = 1.0) -> list[float]:
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        if freq == 0:
            samples.append(0)
        else:
            t = i / SAMPLE_RATE
            phase = (t * freq) % 1.0
            val = (4 * abs(phase - 0.5) - 1) * vol
            env = min(1.0, i / (SAMPLE_RATE * 0.003))
            tail = max(0.0, 1.0 - (i / n) * 0.5)
            samples.append(val * env * tail)
    return samples


def _square(freq: float, dur: float, vol: float = 1.0, duty: float = 0.5) -> list[float]:
    samples = []
    n = int(SAMPLE_RATE * dur)
    for i in range(n):
        if freq == 0:
            samples.append(0)
        else:
            t = i / SAMPLE_RATE
            phase = (t * freq) % 1.0
            val = vol if phase < duty else -vol
            env = min(1.0, i / (SAMPLE_RATE * 0.003))
            tail = max(0.0, 1.0 - (i / n) * 0.8)
            samples.append(val * env * tail)
    return samples


def _write_wav(path: str, samples: list[float]):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        for s in samples:
            s = max(-0.95, min(0.95, s))
            w.writeframes(struct.pack("<h", int(s * 32767)))


def _generate_sfx():
    """Generate all game sound effects as WAV files."""
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="reaction-sfx-")
    v = SFX_VOLUME

    # GO -- short bright blip when green button appears (high C6)
    s = _triangle(1047, 0.05, v * 0.6) + _triangle(1319, 0.04, v * 0.5)
    _write_wav(os.path.join(_sfx_dir, "go.wav"), s)
    _sfx_cache["go"] = os.path.join(_sfx_dir, "go.wav")

    # HIT -- rising happy tone (C5 -> E5 -> G5)
    s = (_square(523, 0.04, v * 0.5, 0.25) +
         _square(659, 0.04, v * 0.6, 0.25) +
         _triangle(784, 0.06, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "hit.wav"), s)
    _sfx_cache["hit"] = os.path.join(_sfx_dir, "hit.wav")

    # MISS -- descending sad tone (A4 -> E4 -> C4)
    s = (_square(440, 0.08, v * 0.4, 0.5) +
         _square(330, 0.10, v * 0.35, 0.5) +
         _square(262, 0.14, v * 0.3, 0.5))
    _write_wav(os.path.join(_sfx_dir, "miss.wav"), s)
    _sfx_cache["miss"] = os.path.join(_sfx_dir, "miss.wav")

    # COMPLETE -- victory jingle (C5 -> E5 -> G5 -> C6, triumphant)
    s = (_triangle(523, 0.08, v * 0.5) +
         _triangle(659, 0.08, v * 0.55) +
         _triangle(784, 0.08, v * 0.6) +
         _triangle(1047, 0.25, v * 0.7))
    _write_wav(os.path.join(_sfx_dir, "complete.wav"), s)
    _sfx_cache["complete"] = os.path.join(_sfx_dir, "complete.wav")


def play_sfx(name: str):
    """Play sound non-blocking via afplay."""
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# -- renderers -------------------------------------------------------------

def render_dark(size=SIZE) -> Image.Image:
    """Dark/off button -- waiting state."""
    img = Image.new("RGB", size, "#0a0a0a")
    return img


def render_green_target(size=SIZE) -> Image.Image:
    """Bright green target -- HIT ME!"""
    img = Image.new("RGB", size, "#22c55e")
    d = ImageDraw.Draw(img)
    # Draw a bright crosshair / target circle
    d.ellipse([18, 18, 78, 78], outline="#15803d", width=4)
    d.ellipse([30, 30, 66, 66], fill="#4ade80", outline="#16a34a", width=2)
    d.ellipse([40, 40, 56, 56], fill="#bbf7d0")
    return img


def render_penalty(size=SIZE) -> Image.Image:
    """Red X -- penalty for wrong/early press."""
    img = Image.new("RGB", size, "#7f1d1d")
    d = ImageDraw.Draw(img)
    d.line([(20, 20), (76, 76)], fill="#ef4444", width=6)
    d.line([(76, 20), (20, 76)], fill="#ef4444", width=6)
    d.text((48, 48), "999", font=_font(18), fill="#fca5a5", anchor="mm")
    return img


def render_hit_feedback(ms: int, size=SIZE) -> Image.Image:
    """Show the reaction time on the pressed button."""
    # Color based on speed
    if ms < 200:
        bg, fg = "#14532d", "#4ade80"
    elif ms < 300:
        bg, fg = "#065f46", "#34d399"
    elif ms < 500:
        bg, fg = "#713f12", "#fbbf24"
    else:
        bg, fg = "#7c2d12", "#f87171"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 38), f"{ms}", font=_font(28), fill=fg, anchor="mm")
    d.text((48, 62), "ms", font=_font(14), fill=fg, anchor="mm")
    return img


def render_hud_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "REACT", font=_font(18), fill="#22c55e", anchor="mm")
    return img


def render_hud_round(current: int, total: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "ROUND", font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), f"{current}/{total}", font=_font(22), fill="#60a5fa", anchor="mt")
    return img


def render_hud_best(best_ms: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "BEST", font=_font(14), fill="#9ca3af", anchor="mt")
    label = f"{best_ms}" if best_ms < 9999 else "---"
    d.text((48, 48), label, font=_font(24), fill="#34d399", anchor="mt")
    d.text((48, 76), "ms", font=_font(12), fill="#6b7280", anchor="mt")
    return img


def render_hud_avg(avg_ms: int, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "AVG", font=_font(14), fill="#9ca3af", anchor="mt")
    label = f"{avg_ms}" if avg_ms < 9999 else "---"
    d.text((48, 48), label, font=_font(24), fill="#fbbf24", anchor="mt")
    d.text((48, 76), "ms", font=_font(12), fill="#6b7280", anchor="mt")
    return img


def render_hud_last(ms: int, size=SIZE) -> Image.Image:
    """Show last reaction time on HUD."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "LAST", font=_font(14), fill="#9ca3af", anchor="mt")
    if ms <= 0:
        label = "---"
        clr = "#6b7280"
    elif ms >= PENALTY_MS:
        label = "FAIL"
        clr = "#ef4444"
    elif ms < 300:
        label = f"{ms}"
        clr = "#4ade80"
    elif ms < 500:
        label = f"{ms}"
        clr = "#fbbf24"
    else:
        label = f"{ms}"
        clr = "#f87171"
    d.text((48, 48), label, font=_font(24), fill=clr, anchor="mt")
    if ms > 0 and ms < PENALTY_MS:
        d.text((48, 76), "ms", font=_font(12), fill="#6b7280", anchor="mt")
    return img


def render_hud_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_hud_wait(size=SIZE) -> Image.Image:
    """WAIT indicator on HUD."""
    img = Image.new("RGB", size, "#450a0a")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "WAIT", font=_font(18), fill="#f87171", anchor="mm")
    return img


def render_hud_go(size=SIZE) -> Image.Image:
    """GO! indicator on HUD."""
    img = Image.new("RGB", size, "#14532d")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GO!", font=_font(22), fill="#4ade80", anchor="mm")
    return img


def render_start(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 38), "PRESS", font=_font(16), fill="white", anchor="mm")
    d.text((48, 58), "START", font=_font(16), fill="#34d399", anchor="mm")
    return img


def render_game_over_btn(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white", anchor="mm", align="center")
    return img


def render_final_avg(avg_ms: int, size=SIZE) -> Image.Image:
    """Big average display for game over."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), "FINAL", font=_font(12), fill="#9ca3af", anchor="mt")
    d.text((48, 42), f"{avg_ms}", font=_font(26), fill="#fbbf24", anchor="mt")
    d.text((48, 74), "ms avg", font=_font(12), fill="#6b7280", anchor="mt")
    return img


def render_personal_best(ms: int, is_new: bool, size=SIZE) -> Image.Image:
    """Personal best display for game over."""
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    header = "NEW!" if is_new else "P.B."
    hdr_clr = "#4ade80" if is_new else "#9ca3af"
    d.text((48, 20), header, font=_font(14), fill=hdr_clr, anchor="mt")
    d.text((48, 42), f"{ms}", font=_font(26), fill="#34d399", anchor="mt")
    d.text((48, 74), "ms best", font=_font(12), fill="#6b7280", anchor="mt")
    return img


# -- game logic ------------------------------------------------------------

class ReactionGame:
    def __init__(self, deck):
        self.deck = deck
        self.round = 0
        self.times: list[int] = []       # reaction times per round (ms)
        self.best_ever: int = scores.load_best("reaction", 99999)      # personal best across games
        self.state = "idle"              # idle | waiting | ready | reacting | feedback | gameover
        self.lock = threading.Lock()
        self.target_key = -1             # which key is lit green
        self.green_time: float = 0       # monotonic time when green appeared
        self.round_thread: threading.Thread | None = None
        self.expire_timer: threading.Timer | None = None
        # Pre-render reusable images
        self.img_title = render_hud_title()
        self.img_hud_empty = render_hud_empty()
        self.img_dark = render_dark()
        self.img_start = render_start()
        self.img_penalty = render_penalty()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _window_for_round(self, rnd: int) -> float:
        """How long the green target stays visible -- shrinks over rounds."""
        # Linear interpolation: round 1 = WINDOW_START, round 10 = WINDOW_END
        t = (rnd - 1) / max(1, TOTAL_ROUNDS - 1)
        return WINDOW_START + (WINDOW_END - WINDOW_START) * t

    def _current_best(self) -> int:
        """Best time in current game's rounds (excluding penalties)."""
        valid = [t for t in self.times if t < PENALTY_MS]
        return min(valid) if valid else 99999

    def _current_avg(self) -> int:
        """Average of all rounds so far."""
        return int(sum(self.times) / len(self.times)) if self.times else 0

    def _update_hud(self):
        self.set_key(0, self.img_title)
        self.set_key(1, render_hud_round(self.round, TOTAL_ROUNDS))
        self.set_key(2, render_hud_best(self._current_best()))
        self.set_key(3, render_hud_avg(self._current_avg()) if self.times else render_hud_avg(0))
        last_ms = self.times[-1] if self.times else 0
        self.set_key(4, render_hud_last(last_ms))
        # Slot 5-6: state indicator
        if self.state == "waiting":
            self.set_key(5, render_hud_wait())
        elif self.state in ("ready", "reacting"):
            self.set_key(5, render_hud_go())
        else:
            self.set_key(5, self.img_hud_empty)
        # Slot 6-7: personal best
        self.set_key(6, render_hud_best(self.best_ever) if self.best_ever < 99999 else self.img_hud_empty)
        self.set_key(7, self.img_hud_empty)

    def _darken_game_area(self):
        """Set all game keys to dark/off."""
        for k in GAME_KEYS:
            self.set_key(k, self.img_dark)

    def show_idle(self):
        """Show start screen."""
        self.state = "idle"
        self.round = 0
        self.times = []
        # HUD
        self.set_key(0, self.img_title)
        self.set_key(1, render_hud_round(0, TOTAL_ROUNDS))
        best_disp = self.best_ever if self.best_ever < 99999 else 99999
        self.set_key(2, render_hud_best(best_disp))
        for k in range(3, 8):
            self.set_key(k, self.img_hud_empty)
        # Game area -- all dark except start button
        for k in GAME_KEYS:
            if k == START_KEY:
                self.set_key(k, self.img_start)
            else:
                self.set_key(k, self.img_dark)

    def start_game(self):
        """Begin a new 10-round game."""
        with self.lock:
            self.round = 0
            self.times = []
            self.state = "waiting"
        play_orc("start")
        self._darken_game_area()
        self._update_hud()
        # Start first round
        self.round_thread = threading.Thread(target=self._run_round, daemon=True)
        self.round_thread.start()

    def _run_round(self):
        """Execute one round: dark -> random delay -> green target."""
        with self.lock:
            self.round += 1
            self.state = "waiting"
            self.target_key = -1
        self._darken_game_area()
        self._update_hud()

        # Random delay before showing green
        delay = random.uniform(DELAY_MIN, DELAY_MAX)
        # Sleep in small increments so we can check state
        elapsed = 0.0
        while elapsed < delay:
            time.sleep(0.05)
            elapsed += 0.05
            with self.lock:
                if self.state not in ("waiting",):
                    # Player hit early -- penalty already applied in on_key
                    return

        # Show the green target
        target = random.choice(GAME_KEYS)
        with self.lock:
            if self.state != "waiting":
                return
            self.state = "ready"
            self.target_key = target
            self.green_time = time.monotonic()

        self.set_key(target, render_green_target())
        play_sfx("go")
        self._update_hud()

        # Start expiration timer -- target disappears after window
        window = self._window_for_round(self.round)
        self.expire_timer = threading.Timer(window, self._target_expired)
        self.expire_timer.daemon = True
        self.expire_timer.start()

    def _target_expired(self):
        """Target disappeared before player reacted -- penalty."""
        with self.lock:
            if self.state != "ready":
                return
            self.state = "feedback"
            key = self.target_key
            self.target_key = -1

        # Too slow -- penalty
        self.times.append(PENALTY_MS)
        self.set_key(key, self.img_penalty)
        play_sfx("miss")
        play_orc("slow_fail")
        self._update_hud()

        time.sleep(0.8)
        self.set_key(key, self.img_dark)
        self._advance()

    def _cancel_expire_timer(self):
        if self.expire_timer:
            self.expire_timer.cancel()
            self.expire_timer = None

    def _advance(self):
        """Move to next round or end game."""
        if self.round >= TOTAL_ROUNDS:
            self._end_game()
        else:
            # Brief pause then next round
            time.sleep(0.5)
            self.round_thread = threading.Thread(target=self._run_round, daemon=True)
            self.round_thread.start()

    def _end_game(self):
        """Show final results."""
        with self.lock:
            self.state = "gameover"

        avg = self._current_avg()
        best_this_game = self._current_best()
        is_new_best = best_this_game < self.best_ever and best_this_game < PENALTY_MS
        if is_new_best:
            self.best_ever = best_this_game
            scores.save_best("reaction", self.best_ever)

        play_sfx("complete")
        if is_new_best:
            play_orc("newbest")

        # Update HUD with final stats
        self.set_key(0, self.img_title)
        self.set_key(1, render_hud_round(TOTAL_ROUNDS, TOTAL_ROUNDS))
        self.set_key(2, render_hud_best(self._current_best()))
        self.set_key(3, render_hud_avg(avg))
        last_ms = self.times[-1] if self.times else 0
        self.set_key(4, render_hud_last(last_ms))
        for k in range(5, 8):
            self.set_key(k, self.img_hud_empty)

        # Game area -- show results
        self._darken_game_area()
        # Center area: final avg and personal best
        self.set_key(11, render_final_avg(avg))
        self.set_key(12, render_personal_best(
            self.best_ever if self.best_ever < 99999 else best_this_game,
            is_new_best,
        ))
        # Game over labels
        self.set_key(18, render_game_over_btn())
        self.set_key(19, render_game_over_btn())
        # Restart button
        self.set_key(START_KEY, self.img_start)

    def on_key(self, _deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and self.state in ("idle", "gameover"):
            self.start_game()
            return

        if key not in GAME_KEYS:
            return

        with self.lock:
            state = self.state

        if state == "waiting":
            # Player hit a key before green appeared -- early press penalty
            with self.lock:
                self.state = "feedback"
                self._cancel_expire_timer()

            self.times.append(PENALTY_MS)
            self.set_key(key, self.img_penalty)
            play_sfx("miss")
            play_orc("slow_fail")
            self._update_hud()

            def _after_early():
                time.sleep(0.8)
                self.set_key(key, self.img_dark)
                self._advance()

            threading.Thread(target=_after_early, daemon=True).start()
            return

        if state == "ready":
            self._cancel_expire_timer()
            reaction_ms = int((time.monotonic() - self.green_time) * 1000)

            with self.lock:
                target = self.target_key
                self.target_key = -1

            if key == target:
                # Correct hit!
                with self.lock:
                    self.state = "feedback"
                self.times.append(reaction_ms)
                self.set_key(key, render_hit_feedback(reaction_ms))
                play_sfx("hit")

                # Orc voice for fast reactions
                if reaction_ms < 300:
                    play_orc("fast_reaction")

                self._update_hud()

                def _after_hit():
                    time.sleep(0.8)
                    self.set_key(key, self.img_dark)
                    self._advance()

                threading.Thread(target=_after_hit, daemon=True).start()
            else:
                # Wrong button -- penalty
                with self.lock:
                    self.state = "feedback"
                self.times.append(PENALTY_MS)
                # Show penalty on pressed key, clear the target
                self.set_key(key, self.img_penalty)
                if target >= 0:
                    self.set_key(target, self.img_dark)
                play_sfx("miss")
                play_orc("slow_fail")
                self._update_hud()

                def _after_wrong():
                    time.sleep(0.8)
                    self.set_key(key, self.img_dark)
                    self._advance()

                threading.Thread(target=_after_wrong, daemon=True).start()


# -- main ------------------------------------------------------------------

def main():
    decks = DeviceManager().enumerate()
    deck = None
    for d in decks:
        if d.is_visual():
            deck = d
            break

    if not deck:
        print("No Stream Deck found!")
        sys.exit(1)

    # Generate 8-bit sound effects
    try:
        _generate_sfx()
        print("Sound effects: ON")
    except Exception:
        print("Sound effects: OFF (generation failed)")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("REACTION SPEED TEST! Press the center button to start.")

    game = ReactionGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        best = game.best_ever if game.best_ever < 99999 else "N/A"
        print(f"\nBye! Personal best: {best}ms")
    finally:
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
