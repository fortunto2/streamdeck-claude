"""Tests for Stream Deck discovery and connection."""

from unittest.mock import MagicMock, patch


def test_find_deck_returns_first_visual_deck():
    """find_deck should return the first visual StreamDeck device."""
    mock_deck = MagicMock()
    mock_deck.is_visual.return_value = True
    mock_deck.deck_type.return_value = "Stream Deck XL"
    mock_deck.key_count.return_value = 32

    with patch("src.daemon.DeviceManager") as MockDM:
        MockDM.return_value.enumerate.return_value = [mock_deck]
        from src.daemon import find_deck

        deck = find_deck()
        assert deck is not None
        assert deck.key_count() == 32


def test_find_deck_returns_none_when_no_devices():
    """find_deck should return None when no devices are connected."""
    with patch("src.daemon.DeviceManager") as MockDM:
        MockDM.return_value.enumerate.return_value = []
        from src.daemon import find_deck

        deck = find_deck()
        assert deck is None
