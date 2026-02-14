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


# ── game registry ────────────────────────────────────────────────────

GAMES = [
    {
        "title": "BEAVER",
        "subtitle": "HUNT",
        "bg": "#2d1b0e",
        "script": "beaver_game",
        "pos": 8,  # first button of row 2
    },
    {
        "title": "SIMON",
        "subtitle": "SAYS",
        "bg": "#4c1d95",
        "script": "simon_game",
        "pos": 9,
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

        # Empty HUD slots
        for k in range(1, 8):
            self.set_key(k, render_empty())

        # Game buttons
        for game in GAMES:
            self.set_key(game["pos"], render_game_btn(game["title"], game["subtitle"], game["bg"]))

        # Fill rest with empty
        used = {0} | {g["pos"] for g in GAMES}
        for k in range(8, 32):
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

        # Create game instance
        if script == "beaver_game":
            game = mod.BeaverGame(self.deck)
        elif script == "simon_game":
            game = mod.SimonGame(self.deck)
        else:
            return

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
        if self.active_game:
            # Stop timers if beaver game
            if hasattr(self.active_game, "_cancel_beaver_timer"):
                self.active_game._cancel_beaver_timer()
            if hasattr(self.active_game, "running"):
                self.active_game.running = False
            self.active_game = None
        self.in_menu = True

    def on_key(self, _deck, key: int, pressed: bool):
        """Menu key handler."""
        if not pressed or not self.in_menu:
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
        deck.reset()
        deck.close()


if __name__ == "__main__":
    main()
