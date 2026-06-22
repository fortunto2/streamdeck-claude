"""Base class for deck-owning control surfaces (REAPER, Ableton, …).

A control surface takes over the whole Stream Deck while active and hands
control back to the home dashboard via `on_home`. This base owns the bits
every surface shares — key blitting, a debounced repaint, and lifecycle
flags — so each concrete surface only implements its own layout + domain
wiring (start / render / on_key / on_teardown).
"""

from __future__ import annotations

import os
import sys
import threading

# Surfaces wrap OSC clients that live in the src/ package (src/reaper.py,
# src/ableton.py). These scripts run from scripts/, so put the project root
# on the path once, here, for every surface that imports this base.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from StreamDeck.ImageHelpers import PILHelper

import deck_ui


class ControlSurface:
    """Owns the deck while active; returns home on the HOME key."""

    HOME_KEY = 31

    def __init__(self, deck, on_home):
        self.deck = deck
        self.on_home = on_home
        self.running = False
        self._repaint_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        # Last image blitted per key — skip re-encoding + re-sending a key whose
        # pixels didn't change. Surfaces repaint whole rows every frame; without
        # this, ~200 redundant USB writes/sec fight the key-reader for the deck
        # lock and show up as input lag.
        self._key_cache: dict[int, int] = {}
        # Injected by the host launcher: goto(factory, back=...) opens
        # another surface (used by the Music Hub to switch instruments).
        self.goto = None

    # -- subclass contract ----------------------------------------------

    def start(self) -> None:
        """Connect to the domain, then render. Sets self.running = True."""
        raise NotImplementedError

    def render(self) -> None:
        """Paint every key for the current state."""
        raise NotImplementedError

    def on_key(self, deck, key: int, pressed: bool) -> None:
        """Handle a physical key press/release."""
        raise NotImplementedError

    def on_teardown(self) -> None:
        """Release domain resources (OSC servers, threads). Optional."""

    # -- shared lifecycle / IO ------------------------------------------

    def stop(self) -> None:
        self.running = False
        with self._lock:
            if self._repaint_timer:
                self._repaint_timer.cancel()
                self._repaint_timer = None
        try:
            self.on_teardown()
        except Exception:
            pass

    def set_key(self, pos: int, img) -> None:
        try:
            digest = hash(img.tobytes())
            if self._key_cache.get(pos) == digest:
                return  # unchanged since last blit — skip the encode + USB write
            native = PILHelper.to_native_key_format(self.deck, img)
            with self.deck:
                self.deck.set_key_image(pos, native)
            self._key_cache[pos] = digest
        except Exception:
            pass

    def request_repaint(self, delay: float = 0.12) -> None:
        """Coalesce a burst of feedback into a single render() ~8 Hz."""
        if not self.running:
            return
        with self._lock:
            if self._repaint_timer is not None:
                return

            def _go():
                with self._lock:
                    self._repaint_timer = None
                if self.running:
                    self.render()
            self._repaint_timer = threading.Timer(delay, _go)
            self._repaint_timer.daemon = True
            self._repaint_timer.start()

    def render_home_key(self) -> None:
        self.set_key(self.HOME_KEY, deck_ui.home_img())
