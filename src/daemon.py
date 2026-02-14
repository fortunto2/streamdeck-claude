"""Stream Deck Claude — main daemon."""

import sys

from StreamDeck.DeviceManager import DeviceManager


def find_deck():
    """Find first visual Stream Deck device."""
    decks = DeviceManager().enumerate()
    for deck in decks:
        if deck.is_visual():
            return deck
    return None


def main():
    deck = find_deck()
    if deck is None:
        print("No Stream Deck found. Is it plugged in?")
        sys.exit(1)

    deck.open()
    deck.reset()
    print(f"Connected: {deck.deck_type()} ({deck.key_count()} keys)")
    print(f"Serial: {deck.get_serial_number()}")
    print(f"Firmware: {deck.get_firmware_version()}")
    deck.set_brightness(30)

    try:
        input("Press Enter to exit...\n")
    except KeyboardInterrupt:
        pass
    finally:
        deck.reset()
        deck.close()
        print("Deck closed.")


if __name__ == "__main__":
    main()
