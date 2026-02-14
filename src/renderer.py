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
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
        except OSError:
            font = ImageFont.load_default()
        draw.text(
            (size[0] // 2, size[1] - 8),
            label,
            font=font,
            fill="white",
            anchor="ms",
        )

    return img
