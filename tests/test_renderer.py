"""Tests for PIL-based icon renderer."""

from PIL import Image


def test_render_button_returns_pil_image():
    """render_button should return a 96x96 PIL Image."""
    from src.renderer import render_button

    img = render_button(
        size=(96, 96),
        label="Git",
        bg_color="#22c55e",
        icon_path=None,
    )
    assert isinstance(img, Image.Image)
    assert img.size == (96, 96)


def test_render_button_different_colors():
    """Different status colors should produce different images."""
    from src.renderer import render_button

    green = render_button(size=(96, 96), label="OK", bg_color="#22c55e")
    red = render_button(size=(96, 96), label="OK", bg_color="#ef4444")
    assert green.tobytes() != red.tobytes()


def test_status_to_color_mapping():
    """status_to_color should map known statuses to hex colors."""
    from src.renderer import status_to_color

    assert status_to_color("clean") == "#22c55e"
    assert status_to_color("dirty") == "#ef4444"
    assert status_to_color("running") == "#3b82f6"
    assert status_to_color("unknown") == "#6b7280"
