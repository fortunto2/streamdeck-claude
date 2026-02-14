"""Stream Deck Arcade — game launcher menu.

Shows available games on the deck, press a button to launch one.
Press button 0 (top-left) to return to menu from any game.

Usage:
    uv run python scripts/arcade.py
"""

import importlib
import os
import subprocess
import sys
import threading

from PIL import Image, ImageDraw, ImageFont
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper

import sound_engine

SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


# ── menu renderers ───────────────────────────────────────────────────

def render_logo(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#4c1d95")
    d = ImageDraw.Draw(img)
    d.text((48, 24), "STREAM", font=_font(13), fill="#c4b5fd", anchor="mt")
    d.text((48, 42), "DECK", font=_font(13), fill="#c4b5fd", anchor="mt")
    d.text((48, 62), "ARCADE", font=_font(14), fill="#fbbf24", anchor="mt")
    return img


def render_game_btn(title: str, subtitle: str, bg: str, size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    d.text((48, 30), title, font=_font(16), fill="white", anchor="mt")
    d.text((48, 54), subtitle, font=_font(11), fill="#d1d5db", anchor="mt")
    return img


def render_empty(size=SIZE) -> Image.Image:
    return Image.new("RGB", size, "#111827")


def render_back(size=SIZE) -> Image.Image:
    img = Image.new("RGB", size, "#374151")
    d = ImageDraw.Draw(img)
    d.text((48, 30), "<< BACK", font=_font(14), fill="#fbbf24", anchor="mt")
    d.text((48, 52), "MENU", font=_font(14), fill="#9ca3af", anchor="mt")
    return img


VOICE_TOGGLE_KEY = 15  # end of row 2


def render_voice_btn(enabled: bool, size=SIZE) -> Image.Image:
    bg = "#065f46" if enabled else "#7f1d1d"
    img = Image.new("RGB", size, bg)
    d = ImageDraw.Draw(img)
    label = "VOICE" if enabled else "VOICE"
    state = "ON" if enabled else "OFF"
    color = "#34d399" if enabled else "#f87171"
    d.text((48, 30), label, font=_font(14), fill="white", anchor="mt")
    d.text((48, 54), state, font=_font(16), fill=color, anchor="mt")
    return img


# ── game registry ────────────────────────────────────────────────────

GAMES = [
    # Row 1: logo(0) + action games (buttons 1-7)
    {
        "title": "BEAVER",
        "subtitle": "HUNT",
        "bg": "#2d1b0e",
        "script": "beaver_game",
        "pos": 1,
    },
    {
        "title": "SIMON",
        "subtitle": "SAYS",
        "bg": "#4c1d95",
        "script": "simon_game",
        "pos": 2,
    },
    {
        "title": "REACT",
        "subtitle": "SPEED",
        "bg": "#065f46",
        "script": "reaction_game",
        "pos": 3,
    },
    {
        "title": "SNAKE",
        "subtitle": "GAME",
        "bg": "#14532d",
        "script": "snake_game",
        "pos": 4,
    },
    {
        "title": "MEMORY",
        "subtitle": "MATCH",
        "bg": "#1e3a5f",
        "script": "memory_game",
        "pos": 5,
    },
    {
        "title": "BREAK",
        "subtitle": "OUT",
        "bg": "#92400e",
        "script": "breakout_game",
        "pos": 6,
    },
    {
        "title": "CHIMP",
        "subtitle": "TEST",
        "bg": "#991b1b",
        "script": "sequence_game",
        "pos": 7,
    },
    # Row 2: logic & IQ games (buttons 8-15)
    {
        "title": "N-BACK",
        "subtitle": "IQ",
        "bg": "#1e3a5f",
        "script": "nback_game",
        "pos": 8,
    },
    {
        "title": "PATTERN",
        "subtitle": "LOGIC",
        "bg": "#7c3aed",
        "script": "pattern_game",
        "pos": 9,
    },
    {
        "title": "MATH",
        "subtitle": "SEQ",
        "bg": "#0c4a6e",
        "script": "mathseq_game",
        "pos": 10,
    },
    {
        "title": "QUICK",
        "subtitle": "MATH",
        "bg": "#166534",
        "script": "quickmath_game",
        "pos": 11,
    },
    {
        "title": "NUM",
        "subtitle": "GRID",
        "bg": "#4c1d95",
        "script": "numgrid_game",
        "pos": 12,
    },
]


# ── arcade launcher ──────────────────────────────────────────────────

class Arcade:
    def __init__(self, deck):
        self.deck = deck
        self.active_game = None  # currently running game object
        self.active_module = None
        self.in_menu = True

    def set_key(self, pos: int, img: Image.Image):
        native = PILHelper.to_native_key_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(pos, native)

    def show_menu(self):
        """Draw the game selection menu."""
        self.in_menu = True
        self.active_game = None
        self.active_module = None
        self.deck.reset()

        # Logo in top-left
        self.set_key(0, render_logo())

        # Game buttons
        for game in GAMES:
            self.set_key(game["pos"], render_game_btn(game["title"], game["subtitle"], game["bg"]))

        # Voice toggle button
        self.set_key(VOICE_TOGGLE_KEY, render_voice_btn(sound_engine.voices_enabled))

        # Fill rest with empty
        used = {0, VOICE_TOGGLE_KEY} | {g["pos"] for g in GAMES}
        for k in range(1, 32):
            if k not in used:
                self.set_key(k, render_empty())

    def launch_game(self, game_info: dict):
        """Launch a game by importing its module and calling main logic."""
        self.in_menu = False
        script = game_info["script"]

        # Import the game module
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)

        mod = importlib.import_module(script)

        # Generate SFX if needed
        if hasattr(mod, "_generate_sfx") and not mod._sfx_cache:
            try:
                mod._generate_sfx()
            except Exception:
                pass

        # Create game instance — map script names to class names
        class_map = {
            "beaver_game": "BeaverGame",
            "simon_game": "SimonGame",
            "reaction_game": "ReactionGame",
            "snake_game": "SnakeGame",
            "memory_game": "MemoryGame",
            "invaders_game": "InvadersGame",
            "breakout_game": "BreakoutGame",
            "pacman_game": "PacmanGame",
            "sequence_game": "SequenceGame",
            "nback_game": "NBackGame",
            "pattern_game": "PatternGame",
            "mathseq_game": "MathSeqGame",
            "quickmath_game": "QuickMathGame",
            "numgrid_game": "NumGridGame",
        }
        cls_name = class_map.get(script)
        if not cls_name or not hasattr(mod, cls_name):
            return
        game = getattr(mod, cls_name)(self.deck)

        self.active_game = game
        self.active_module = mod

        # Set back button
        self.set_key(0, render_back())

        # Show game idle screen
        game.show_idle()

        # Wrap the game's key callback to intercept back button
        original_on_key = game.on_key

        def wrapped_on_key(deck, key, pressed):
            if pressed and key == 0:
                # Back to menu — stop game
                self._stop_game()
                self.show_menu()
                self.deck.set_key_callback(self.on_key)
                return
            original_on_key(deck, key, pressed)

        self.deck.set_key_callback(wrapped_on_key)

    def _stop_game(self):
        """Clean up active game."""
        sound_engine.stop_all()  # kill any playing audio
        if self.active_game:
            if hasattr(self.active_game, "_cancel_beaver_timer"):
                self.active_game._cancel_beaver_timer()
            if hasattr(self.active_game, "_cancel_tick"):
                self.active_game._cancel_tick()
            if hasattr(self.active_game, "_cancel_all_timers"):
                self.active_game._cancel_all_timers()
            if hasattr(self.active_game, "_cancel_timer"):
                self.active_game._cancel_timer()
            if hasattr(self.active_game, "running"):
                self.active_game.running = False
            self.active_game = None
        self.in_menu = True

    def on_key(self, _deck, key: int, pressed: bool):
        """Menu key handler."""
        if not pressed or not self.in_menu:
            return

        # Voice toggle
        if key == VOICE_TOGGLE_KEY:
            sound_engine.voices_enabled = not sound_engine.voices_enabled
            self.set_key(VOICE_TOGGLE_KEY, render_voice_btn(sound_engine.voices_enabled))
            return

        for game in GAMES:
            if key == game["pos"]:
                self.launch_game(game)
                return


# ── main ─────────────────────────────────────────────────────────────

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

    deck.open()
    deck.reset()
    deck.set_brightness(80)
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print("STREAM DECK ARCADE — choose a game!")

    arcade = Arcade(deck)
    arcade.show_menu()
    deck.set_key_callback(arcade.on_key)

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nBye!")
    finally:
        sound_engine.stop_all()
        deck.reset()
        deck.close()


if __name__ == "__main__":
    main()
