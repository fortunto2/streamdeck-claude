"""Shared Stream Deck rendering primitives for control surfaces.

Pure presentation: turns (text, colour) tuples into 96×96 key images.
No deck I/O, no domain state — imported by every control surface so the
look stays consistent and the helpers live in exactly one place.
"""

from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont

SIZE = (96, 96)
FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"
# Helvetica lacks media-control glyphs (▶ ■ ↻ ⌂ …); Arial Unicode has full
# coverage. Lines containing any non-ASCII char render with the symbol font.
SYMBOL_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"

Line = tuple[str, int, str]  # (text, font_size, colour)


def font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def sfont(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(SYMBOL_FONT_PATH, size)
    except OSError:
        return font(size)


def pick_font(text: str, size: int) -> ImageFont.FreeTypeFont:
    """Symbol font for any line with non-ASCII glyphs, else Helvetica."""
    return sfont(size) if any(ord(c) > 127 for c in text) else font(size)


def fit(draw: ImageDraw.ImageDraw, text: str, fnt: ImageFont.FreeTypeFont, max_w: int) -> str:
    """Truncate text with an ellipsis so it fits inside max_w pixels."""
    if draw.textlength(text, font=fnt) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=fnt) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


def btn(bg: str, lines: list[Line], border: str | None = None) -> Image.Image:
    """Render a key: bg colour + a vertically-distributed stack of lines.

    `border` (a colour) draws a 3px outline — used to mark an active/fired
    state independently of the background colour.
    """
    img = Image.new("RGB", SIZE, bg)
    d = ImageDraw.Draw(img)
    ys = {1: [48], 2: [34, 62], 3: [24, 48, 72], 4: [18, 40, 62, 82]}.get(len(lines), [48])
    for (text, fs, color), y in zip(lines, ys):
        f = pick_font(text, fs)
        d.text((48, y), fit(d, text, f, 90), font=f, fill=color, anchor="mm")
    if border:
        d.rectangle([0, 0, SIZE[0] - 1, SIZE[1] - 1], outline=border, width=3)
    return img


def rgb(color_int: int) -> tuple[int, int, int]:
    """Split a 0xRRGGBB int (e.g. an Ableton track colour) into an RGB tuple."""
    return ((color_int >> 16) & 0xFF, (color_int >> 8) & 0xFF, color_int & 0xFF)


# Stable identifying palette for hosts that don't expose real track colours
# over their control protocol (REAPER's stock OSC has no track-colour
# message). Indexed by track position so each track keeps one colour.
PALETTE = [
    0xE0567A, 0xE0894A, 0xE0C84A, 0x7AC84A,
    0x4AC8A0, 0x4A9AE0, 0x8A6AE0, 0xE06AC8,
]


def palette_color(index: int) -> int:
    """A stable 0xRRGGBB identifying colour for the given track index."""
    return PALETTE[index % len(PALETTE)]


def _lum(r: float, g: float, b: float) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def dim_hex(color_int: int, factor: float = 1.0) -> str:
    """A 0xRRGGBB int scaled by `factor` and returned as a #rrggbb string."""
    r, g, b = rgb(color_int)
    return "#%02x%02x%02x" % (min(255, int(r * factor)), min(255, int(g * factor)), min(255, int(b * factor)))


def text_for(color_int: int, factor: float = 1.0) -> str:
    """Black or white text colour for legibility over the given background."""
    r, g, b = rgb(color_int)
    return "#0a0a0a" if _lum(r * factor, g * factor, b * factor) > 140 else "#ffffff"


def mix(c1: tuple[int, int, int], c2: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """Linear blend of two RGB tuples; t=0 → c1, t=1 → c2."""
    t = min(max(t, 0.0), 1.0)
    return tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))


def hexstr(rgb_tuple: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % rgb_tuple


def home_img() -> Image.Image:
    """The standard back-to-dashboard key, shared by all surfaces."""
    return btn("#4c1d95", [("⌂", 24, "#ffffff"), ("HOME", 12, "#ddd6fe")])


def vu_bar(draw: ImageDraw.ImageDraw, level: float, x0: int = 86, x1: int = 93,
           top: int = 10, bottom: int = 90) -> None:
    """Draw a vertical peak meter up the right edge of a key.

    `level` is expected already-scaled 0..1 (the caller applies any gamma /
    peak-decay). Drawn linearly so the caller's curve is what you see.
    """
    draw.rectangle([x0, top, x1, bottom], outline="#334155", width=1)
    lvl = min(max(level, 0.0), 1.0)
    h = int(lvl * (bottom - top - 2))
    if h > 0:
        color = "#ef4444" if lvl > 0.85 else "#facc15" if lvl > 0.6 else "#22c55e"
        draw.rectangle([x0, bottom - h, x1, bottom], fill=color)
