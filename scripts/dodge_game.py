"""Dodge -- Stream Deck survival arcade.

Meteors rain from above. Dodge them with your ship on the bottom row!
Full 4x8 grid = game field. Score = ticks survived.

Usage:
    uv run python scripts/dodge_game.py
"""

import math, os, random, struct, sys, tempfile, threading, time, wave

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

import sound_engine, scores

# -- config ----------------------------------------------------------------
ROWS = 4
COLS = 8
SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
SFX_VOLUME = 0.3
TICK_START = 0.8        # initial tick interval (seconds)
TICK_MIN = 0.25         # fastest tick
TICK_SPEEDUP = 0.02     # decrease per 5 points
START_KEY = 28           # center-bottom row to start


def pos_to_rc(pos: int) -> tuple[int, int]:
    return pos // COLS, pos % COLS


def rc_to_pos(row: int, col: int) -> int:
    return row * COLS + col


# -- voice lines (Dota 2 Axe) ---------------------------------------------
PEON_DIR = os.path.expanduser("~/.claude/hooks/peon-ping/packs")
VOICES = {
    "start": ["dota2_axe/sounds/GoodDayToFight.mp3",
              "dota2_axe/sounds/LetTheCarnageBegin.mp3"],
    "gameover": ["dota2_axe/sounds/FoughtBadly.mp3",
                 "dota2_axe/sounds/YouGetNothing.mp3"],
    "newbest": ["dota2_axe/sounds/CutAbove.mp3",
                "dota2_axe/sounds/AxeIsReady.mp3"],
    "milestone": ["dota2_axe/sounds/ComeAndGetIt.mp3",
                  "dota2_axe/sounds/Forward.mp3"],
}

_last_voice_time: float = 0
VOICE_COOLDOWN = 4.0


def play_voice(event: str):
    """Play a random Axe voice line -- with cooldown to avoid spam."""
    global _last_voice_time
    now = time.monotonic()
    if now - _last_voice_time < VOICE_COOLDOWN:
        return
    paths = VOICES.get(event, [])
    if not paths:
        return
    random.shuffle(paths)
    for rel in paths:
        full = os.path.join(PEON_DIR, rel)
        if os.path.exists(full):
            _last_voice_time = now
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


def _square(freq: float, dur: float, vol: float = 1.0,
            duty: float = 0.5) -> list[float]:
    samples, n = [], int(SAMPLE_RATE * dur)
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


def _triangle(freq: float, dur: float, vol: float = 1.0) -> list[float]:
    samples, n = [], int(SAMPLE_RATE * dur)
    for i in range(n):
        if freq == 0:
            samples.append(0)
        else:
            t = i / SAMPLE_RATE
            phase = (t * freq) % 1.0
            val = (4 * abs(phase - 0.5) - 1) * vol
            env = min(1.0, i / (SAMPLE_RATE * 0.003))
            tail = max(0.0, 1.0 - (i / n) * 0.6)
            samples.append(val * env * tail)
    return samples


def _noise(dur: float, vol: float = 0.5) -> list[float]:
    samples, n = [], int(SAMPLE_RATE * dur)
    for i in range(n):
        env = max(0, 1.0 - (i / n) * 6)
        samples.append(random.uniform(-vol, vol) * env)
    return samples


def _merge(*lists: list[float]) -> list[float]:
    length = max(len(a) for a in lists)
    return [max(-0.95, min(0.95, sum(a[i] if i < len(a) else 0 for a in lists)))
            for i in range(length)]


def _write_wav(path: str, samples: list[float]):
    with wave.open(path, "w") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        for s in samples:
            w.writeframes(struct.pack("<h", int(max(-0.95, min(0.95, s)) * 32767)))


def _generate_sfx():
    """Generate all game sound effects as WAV files."""
    global _sfx_dir
    _sfx_dir = tempfile.mkdtemp(prefix="dodge-sfx-")
    v = SFX_VOLUME

    def _save(name, samples):
        p = os.path.join(_sfx_dir, f"{name}.wav")
        _write_wav(p, samples)
        _sfx_cache[name] = p

    # DODGE -- soft whoosh
    _save("dodge", _noise(0.12, v * 0.15))
    # DIE -- explosion
    _save("die", _merge(
        _noise(0.4, v * 0.6),
        _square(200, 0.1, v * 0.5) + _square(120, 0.15, v * 0.4) +
        _square(60, 0.2, v * 0.3)))
    # NEWBEST -- victory jingle (C5->E5->G5->C6)
    _save("newbest",
          _triangle(523, 0.08, v * 0.5) + _triangle(659, 0.08, v * 0.55) +
          _triangle(784, 0.08, v * 0.6) + _triangle(1047, 0.25, v * 0.7))
    # START -- power-up rising (E4->G4->B4->E5)
    _save("start",
          _triangle(330, 0.06, v * 0.4) + _triangle(392, 0.06, v * 0.45) +
          _triangle(494, 0.06, v * 0.5) + _triangle(659, 0.12, v * 0.6))
    # MILESTONE -- brief fanfare
    _save("milestone",
          _square(523, 0.05, v * 0.4, 0.25) + _square(659, 0.05, v * 0.45, 0.25) +
          _triangle(784, 0.1, v * 0.55))
    # MOVE -- tiny blip
    _save("move", _square(880, 0.02, v * 0.2, 0.25))


def play_sfx(name: str):
    wav = _sfx_cache.get(name)
    if wav and os.path.exists(wav):
        sound_engine.play_sfx_file(wav)


def cleanup_sfx():
    if _sfx_dir and os.path.isdir(_sfx_dir):
        import shutil
        shutil.rmtree(_sfx_dir, ignore_errors=True)


# -- renderers -------------------------------------------------------------
_star_cache: dict[int, list[tuple[int, int]]] = {}


def _get_stars(pos: int) -> list[tuple[int, int]]:
    """Return deterministic star positions for a grid cell."""
    if pos not in _star_cache:
        rng = random.Random(pos * 7 + 31)
        _star_cache[pos] = [(rng.randint(4, 91), rng.randint(4, 91))
                            for _ in range(rng.randint(2, 5))]
    return _star_cache[pos]


def render_empty(pos: int = 0, size=SIZE) -> Image.Image:
    """Dark space tile with tiny stars."""
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    for sx, sy in _get_stars(pos):
        d.rectangle([sx, sy, sx + 1, sy + 1],
                     fill=random.choice(["#334155", "#475569", "#64748b"]))
    return img


def render_player(score: int = 0, size=SIZE) -> Image.Image:
    """Bright green ship/arrow pointing up with score overlay."""
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    cx = 48
    # Ship body
    d.polygon([(cx, 12), (cx - 22, 78), (cx + 22, 78)], fill="#22c55e")
    d.polygon([(cx, 22), (cx - 12, 68), (cx + 12, 68)], fill="#4ade80")
    # Cockpit
    d.ellipse([cx - 6, 30, cx + 6, 42], fill="#86efac")
    # Engine glow
    d.ellipse([cx - 10, 72, cx + 10, 84], fill="#fbbf24")
    d.ellipse([cx - 6, 74, cx + 6, 82], fill="#fef08a")
    # Wing tips
    d.polygon([(cx - 22, 78), (cx - 30, 70), (cx - 18, 60)], fill="#16a34a")
    d.polygon([(cx + 22, 78), (cx + 30, 70), (cx + 18, 60)], fill="#16a34a")
    # Score overlay
    if score > 0:
        d.text((88, 88), str(score), font=_font(12), fill="#94a3b8", anchor="rb")
    return img


def render_meteor(size=SIZE) -> Image.Image:
    """Red/orange meteor with crater details."""
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    # Main body
    d.ellipse([14, 14, 82, 82], fill="#dc2626")
    d.ellipse([18, 18, 78, 78], fill="#ef4444")
    # Hot core
    d.ellipse([28, 24, 62, 56], fill="#f97316")
    d.ellipse([34, 30, 54, 48], fill="#fbbf24")
    # Craters
    d.ellipse([22, 50, 34, 62], fill="#991b1b")
    d.ellipse([54, 56, 66, 68], fill="#991b1b")
    d.ellipse([50, 22, 60, 32], fill="#b91c1c")
    # Trailing fire
    d.polygon([(30, 14), (24, 2), (36, 8)], fill="#f97316")
    d.polygon([(50, 14), (48, 0), (58, 6)], fill="#fb923c")
    d.polygon([(66, 18), (70, 4), (74, 14)], fill="#f97316")
    return img


def render_hit_flash(size=SIZE) -> Image.Image:
    """White burst when player is hit."""
    img = Image.new("RGB", size, "#ffffff")
    d = ImageDraw.Draw(img)
    cx, cy = 48, 48
    for angle in range(0, 360, 20):
        rad = math.radians(angle)
        x2 = cx + int(44 * math.cos(rad))
        y2 = cy + int(44 * math.sin(rad))
        d.line([(cx, cy), (x2, y2)], fill="#fef08a", width=3)
    d.text((cx, cy), "!", font=_font(36), fill="#dc2626", anchor="mm")
    return img


def render_start_btn(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#065f46")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "DODGE", font=_font(14), fill="#34d399", anchor="mm")
    d.text((48, 50), "PRESS", font=_font(12), fill="white", anchor="mm")
    d.text((48, 66), "START", font=_font(14), fill="#34d399", anchor="mm")
    return img


def render_game_over_tile(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#7c2d12")
    d = ImageDraw.Draw(img)
    d.text((48, 48), "GAME\nOVER", font=_font(18), fill="white",
           anchor="mm", align="center")
    return img


def render_score_tile(label: str, value, color: str = "#fbbf24",
                      size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 20), label, font=_font(14), fill="#9ca3af", anchor="mt")
    d.text((48, 52), str(value), font=_font(28), fill=color, anchor="mt")
    return img


def render_title(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#0f172a")
    d = ImageDraw.Draw(img)
    d.text((48, 28), "DODGE", font=_font(20), fill="#f87171", anchor="mm")
    d.text((48, 52), "GAME", font=_font(16), fill="#fb923c", anchor="mm")
    d.text((48, 72), "survive!", font=_font(11), fill="#64748b", anchor="mm")
    return img


def render_new_best(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#111827")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "NEW", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 50), "BEST!", font=_font(18), fill="#fbbf24", anchor="mt")
    return img


# -- game logic ------------------------------------------------------------

class DodgeGame:
    def __init__(self, deck):
        self.deck = deck
        self.score = 0
        self.best = scores.load_best("dodge")
        self.running = False
        self.lock = threading.Lock()
        self.tick_timer: threading.Timer | None = None
        # Game state
        self.grid: list[list[bool]] = [[False] * COLS for _ in range(ROWS)]
        self.player_col = COLS // 2  # start center
        # Pre-render reusable images
        self.img_meteor = render_meteor()
        self.img_hit = render_hit_flash()
        self.img_start = render_start_btn()
        self.img_title = render_title()
        self.img_gameover = render_game_over_tile()
        self.img_newbest = render_new_best()

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def _cancel_tick(self):
        """Cancel pending tick timer."""
        if self.tick_timer:
            self.tick_timer.cancel()
            self.tick_timer = None

    def _tick_interval(self) -> float:
        """Current tick speed based on score."""
        return max(TICK_MIN, TICK_START - (self.score // 5) * TICK_SPEEDUP)

    # -- idle / start ------------------------------------------------------

    def show_idle(self):
        """Show start screen."""
        self.running = False
        for r in range(ROWS):
            for c in range(COLS):
                self.set_key(rc_to_pos(r, c), render_empty(rc_to_pos(r, c)))
        self.set_key(rc_to_pos(0, 3), self.img_title)
        self.set_key(rc_to_pos(0, 4), render_score_tile(
            "BEST", self.best if self.best > 0 else "--", "#34d399"))
        self.set_key(START_KEY, self.img_start)

    def _start_game(self):
        """Begin a new game."""
        with self.lock:
            self.score = 0
            self.player_col = COLS // 2
            self.running = True
            for r in range(ROWS):
                for c in range(COLS):
                    self.grid[r][c] = False
        play_sfx("start")
        play_voice("start")
        # Render initial empty field + player
        for r in range(ROWS):
            for c in range(COLS):
                self.set_key(rc_to_pos(r, c), render_empty(rc_to_pos(r, c)))
        self._draw_player()
        # Schedule first tick
        self.tick_timer = threading.Timer(self._tick_interval(), self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    # -- rendering helpers -------------------------------------------------

    def _draw_player(self):
        self.set_key(rc_to_pos(3, self.player_col),
                     render_player(self.score))

    def _draw_cell(self, row: int, col: int):
        pos = rc_to_pos(row, col)
        if row == 3 and col == self.player_col:
            self.set_key(pos, render_player(self.score))
        elif self.grid[row][col]:
            self.set_key(pos, self.img_meteor)
        else:
            self.set_key(pos, render_empty(pos))

    def _draw_full_grid(self):
        for r in range(ROWS):
            for c in range(COLS):
                self._draw_cell(r, c)

    # -- game tick ---------------------------------------------------------

    def _tick(self):
        """Main game loop: shift meteors down, spawn new, check collision."""
        if not self.running:
            return

        hit = False
        is_milestone = False

        with self.lock:
            if not self.running:
                return
            # Shift all meteors down one row (bottom-up to avoid overwrite)
            for r in range(ROWS - 1, 0, -1):
                for c in range(COLS):
                    self.grid[r][c] = self.grid[r - 1][c]
            for c in range(COLS):
                self.grid[0][c] = False

            # Collision check
            if self.grid[3][self.player_col]:
                hit = True
                self.running = False
            else:
                # Spawn new meteors in row 0
                if self.score < 10:
                    count = random.randint(1, 2)
                elif self.score < 25:
                    count = random.randint(1, 3)
                elif self.score < 50:
                    count = random.randint(2, 3)
                else:
                    count = random.randint(2, 4)

                available = list(range(COLS))
                if self.score == 0 and self.player_col in available:
                    available.remove(self.player_col)
                count = min(count, len(available))
                for c in random.sample(available, count):
                    self.grid[0][c] = True

                self.score += 1
                is_milestone = self.score > 0 and self.score % 10 == 0

        if hit:
            self._game_over()
            return

        self._draw_full_grid()

        if is_milestone:
            play_sfx("milestone")
            play_voice("milestone")
        else:
            play_sfx("dodge")

        # Schedule next tick
        self.tick_timer = threading.Timer(self._tick_interval(), self._tick)
        self.tick_timer.daemon = True
        self.tick_timer.start()

    # -- game over ---------------------------------------------------------

    def _game_over(self):
        """Handle death -- flash, save score, show results."""
        self._cancel_tick()
        self.set_key(rc_to_pos(3, self.player_col), self.img_hit)
        play_sfx("die")

        is_new_best = self.score > self.best and self.score > 0
        if is_new_best:
            self.best = self.score
            scores.save_best("dodge", self.best)

        def _show_results():
            time.sleep(1.0)
            for r in range(ROWS):
                for c in range(COLS):
                    self.set_key(rc_to_pos(r, c),
                                 render_empty(rc_to_pos(r, c)))
            # Row 0: game over
            self.set_key(rc_to_pos(0, 3), self.img_gameover)
            self.set_key(rc_to_pos(0, 4), self.img_gameover)
            # Row 1: score + best
            self.set_key(rc_to_pos(1, 3),
                         render_score_tile("SCORE", self.score, "#fbbf24"))
            self.set_key(rc_to_pos(1, 4),
                         render_score_tile("BEST", self.best, "#34d399"))
            if is_new_best:
                self.set_key(rc_to_pos(1, 5), self.img_newbest)
            # Row 2: final speed
            self.set_key(rc_to_pos(2, 3), render_score_tile(
                "SPEED", f"{self._tick_interval():.2f}s", "#60a5fa"))
            # Row 3: restart
            self.set_key(START_KEY, self.img_start)
            if is_new_best:
                play_sfx("newbest")
                play_voice("newbest")
            else:
                play_voice("gameover")

        threading.Thread(target=_show_results, daemon=True).start()

    # -- input handling ----------------------------------------------------

    def on_key(self, deck, key: int, pressed: bool):
        if not pressed:
            return

        # Start / restart
        if key == START_KEY and not self.running:
            self._start_game()
            return

        if not self.running:
            return

        # Player movement: only bottom row (row 3, keys 24-31)
        row, col = pos_to_rc(key)
        if row != 3:
            return

        with self.lock:
            if not self.running:
                return
            old_col = self.player_col
            if col == old_col:
                return
            # Moving into a meteor = instant death
            if self.grid[3][col]:
                self.player_col = col
                self.running = False

        if not self.running:
            self._draw_cell(3, old_col)
            self._game_over()
            return

        with self.lock:
            self.player_col = col

        self.set_key(rc_to_pos(3, old_col),
                     render_empty(rc_to_pos(3, old_col)))
        self._draw_player()
        play_sfx("move")


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

    try:
        _generate_sfx()
        print("Sound effects: ON")
    except Exception:
        print("Sound effects: OFF (generation failed)")

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("DODGE GAME! Use bottom row to move. Press center-bottom to start.")

    game = DodgeGame(deck)
    game.show_idle()
    deck.set_key_callback(game.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print(f"\nBye! Final score: {game.score}  Best: {game.best}")
    finally:
        game._cancel_tick()
        deck.reset()
        deck.close()
        cleanup_sfx()


if __name__ == "__main__":
    main()
