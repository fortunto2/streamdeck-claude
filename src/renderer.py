"""PIL-based icon renderer for Stream Deck buttons."""

from PIL import Image, ImageDraw, ImageFont

STATUS_COLORS = {
    "clean": "#22c55e",
    "pass": "#22c55e",
    "done": "#22c55e",
    "active": "#22c55e",
    "dirty": "#ef4444",
    "fail": "#ef4444",
    "failed": "#ef4444",
    "error": "#ef4444",
    "untracked": "#eab308",
    "warning": "#eab308",
    "running": "#3b82f6",
    "in_progress": "#3b82f6",
    "idle": "#6b7280",
    "none": "#6b7280",
    "unknown": "#6b7280",
    "action": "#1e3a5f",
}

FONT_PATH = "/System/Library/Fonts/Helvetica.ttc"


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except OSError:
        return ImageFont.load_default()


def status_to_color(status: str) -> str:
    """Map a status string to a hex color."""
    return STATUS_COLORS.get(status, "#6b7280")


def render_button(
    size: tuple[int, int] = (96, 96),
    label: str | None = None,
    bg_color: str = "#1e3a5f",
    icon_path: str | None = None,
) -> Image.Image:
    """Render a button image with background color, optional icon, and label."""
    img = Image.new("RGB", size, bg_color)
    draw = ImageDraw.Draw(img)

    if icon_path:
        try:
            icon = Image.open(icon_path).convert("RGBA")
            icon_size = (48, 48)
            icon = icon.resize(icon_size, Image.LANCZOS)
            x = (size[0] - icon_size[0]) // 2
            y = 8
            img.paste(icon, (x, y), icon)
        except (FileNotFoundError, OSError):
            pass

    if label:
        lines = label.split("\n")
        if len(lines) == 1:
            font = _font(14)
            draw.text(
                (size[0] // 2, size[1] - 8),
                label, font=font, fill="white", anchor="ms",
            )
        else:
            # Multi-line: first line = name (14pt), second = detail (11pt)
            draw.text(
                (size[0] // 2, size[1] - 22),
                lines[0], font=_font(14), fill="white", anchor="ms",
            )
            draw.text(
                (size[0] // 2, size[1] - 6),
                lines[1][:12], font=_font(11), fill="#aaaaaa", anchor="ms",
            )

    return img


def render_text_button(
    size: tuple[int, int] = (96, 96),
    lines: list[str] | None = None,
    bg_color: str = "#1e3a5f",
    font_sizes: list[int] | None = None,
    colors: list[str] | None = None,
) -> Image.Image:
    """Render a text-only button — big readable text, no icon.

    lines: up to 4 lines of text, centered vertically
    font_sizes: per-line font sizes (default: [18] for 1 line, [16,14] for 2, etc.)
    colors: per-line colors (default: white, then progressively dimmer)
    """
    img = Image.new("RGB", size, bg_color)
    if not lines:
        return img

    draw = ImageDraw.Draw(img)
    n = len(lines)

    # Default font sizes: big for few lines, smaller for more
    if not font_sizes:
        if n == 1:
            font_sizes = [22]
        elif n == 2:
            font_sizes = [18, 14]
        elif n == 3:
            font_sizes = [16, 13, 11]
        else:
            font_sizes = [14, 12, 10, 9]

    # Default colors: white → gray gradient
    if not colors:
        palette = ["#ffffff", "#dddddd", "#aaaaaa", "#888888"]
        colors = palette[:n]

    # Pad to match lines count
    while len(font_sizes) < n:
        font_sizes.append(font_sizes[-1])
    while len(colors) < n:
        colors.append(colors[-1])

    # Calculate total height for vertical centering
    fonts = [_font(s) for s in font_sizes]
    line_heights = [f.getbbox("Ag")[3] - f.getbbox("Ag")[1] for f in fonts]
    spacing = 4
    total_h = sum(line_heights) + spacing * (n - 1)
    y = (size[1] - total_h) // 2

    for i, text in enumerate(lines):
        draw.text(
            (size[0] // 2, y),
            text, font=fonts[i], fill=colors[i], anchor="mt",
        )
        y += line_heights[i] + spacing

    return img
