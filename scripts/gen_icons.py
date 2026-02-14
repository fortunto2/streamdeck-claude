"""Generate 96x96 PNG icons for Stream Deck buttons using PIL.

No external assets needed — draws white shapes on transparent backgrounds.
Icons are resized to 48x48 by the renderer when composited onto button backgrounds.

Usage:
    uv run python scripts/gen_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS_DIR = Path(__file__).parent.parent / "assets"
SIZE = 96
CENTER = SIZE // 2


def _get_font(size: int = 48) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Try to load a system font, fall back to default."""
    for font_path in [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/SFNSMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]:
        try:
            return ImageFont.truetype(font_path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_icon(name: str, draw_func) -> None:
    """Create a 96x96 RGBA icon and save it."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw_func(draw, img)
    img.save(ASSETS_DIR / name)
    print(f"  {name}")


# ---------------------------------------------------------------------------
# Draw functions for each icon
# ---------------------------------------------------------------------------


def draw_git(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Git branch — Y-shaped fork with dots at endpoints."""
    # Trunk
    draw.line([(48, 18), (48, 50)], fill="white", width=3)
    # Left branch
    draw.line([(48, 50), (28, 76)], fill="white", width=3)
    # Right branch
    draw.line([(48, 50), (68, 76)], fill="white", width=3)
    # Dots at endpoints
    draw.ellipse([40, 10, 56, 26], fill="white")
    draw.ellipse([20, 68, 36, 84], fill="white")
    draw.ellipse([60, 68, 76, 84], fill="white")


def draw_pipeline(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Pipeline — three right arrows in a row."""
    y = 48
    for x_start in [14, 38, 62]:
        # Arrow shaft
        draw.line([(x_start, y), (x_start + 16, y)], fill="white", width=3)
        # Arrowhead
        draw.polygon(
            [(x_start + 16, y - 6), (x_start + 24, y), (x_start + 16, y + 6)],
            fill="white",
        )


def draw_claude(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Claude — sparkle/star symbol."""
    cx, cy = 48, 48
    # Four-point star (sparkle)
    draw.polygon(
        [
            (cx, cy - 30),
            (cx + 8, cy - 8),
            (cx + 30, cy),
            (cx + 8, cy + 8),
            (cx, cy + 30),
            (cx - 8, cy + 8),
            (cx - 30, cy),
            (cx - 8, cy - 8),
        ],
        fill="white",
    )


def draw_build(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Build — hammer shape."""
    # Handle
    draw.line([(30, 70), (58, 34)], fill="white", width=4)
    # Head (rectangle rotated — approximate with polygon)
    draw.polygon(
        [(50, 30), (74, 18), (80, 28), (56, 40)],
        fill="white",
    )


def draw_research(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Research — magnifying glass."""
    # Glass circle
    draw.ellipse([22, 16, 62, 56], outline="white", width=4)
    # Handle
    draw.line([(56, 52), (76, 78)], fill="white", width=5)


def draw_validate(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Validate — bold checkmark."""
    draw.line([(20, 50), (38, 70)], fill="white", width=5)
    draw.line([(38, 70), (76, 26)], fill="white", width=5)


def draw_scaffold(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Scaffold — building blocks (three stacked rectangles)."""
    # Bottom row: two blocks
    draw.rectangle([16, 56, 46, 80], outline="white", width=3)
    draw.rectangle([50, 56, 80, 80], outline="white", width=3)
    # Top row: one block centered
    draw.rectangle([33, 24, 63, 48], outline="white", width=3)


def draw_plan(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Plan — clipboard/list with lines."""
    # Clipboard outline
    draw.rectangle([22, 18, 74, 82], outline="white", width=3)
    # Clip at top
    draw.rectangle([36, 12, 60, 24], fill="white")
    # Lines
    for y in [36, 50, 64]:
        draw.line([(32, y), (64, y)], fill="white", width=2)


def draw_deploy(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Deploy — rocket shape."""
    # Body (elongated triangle pointing up)
    draw.polygon(
        [(48, 10), (36, 58), (60, 58)],
        fill="white",
    )
    # Fins
    draw.polygon([(36, 50), (22, 70), (36, 62)], fill="white")
    draw.polygon([(60, 50), (74, 70), (60, 62)], fill="white")
    # Exhaust
    draw.polygon([(40, 58), (48, 80), (56, 58)], fill="white")
    # Window (cutout circle — draw in transparent)
    draw.ellipse([42, 28, 54, 40], fill=(0, 0, 0, 0))


def draw_review(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Review — eye symbol."""
    # Eye outline (almond shape)
    draw.arc([10, 28, 86, 68], 0, 360, fill="white", width=3)
    # Upper and lower lids as arcs
    draw.arc([10, 20, 86, 76], 200, 340, fill="white", width=3)
    draw.arc([10, 20, 86, 76], 20, 160, fill="white", width=3)
    # Pupil
    draw.ellipse([36, 36, 60, 60], fill="white")
    # Inner pupil (dark)
    draw.ellipse([42, 42, 54, 54], fill=(0, 0, 0, 0))


def draw_tmux(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Tmux — terminal prompt >_."""
    # Terminal box
    draw.rectangle([14, 18, 82, 78], outline="white", width=3)
    # Top bar
    draw.line([(14, 30), (82, 30)], fill="white", width=2)
    # > prompt
    draw.line([(24, 44), (36, 52)], fill="white", width=3)
    draw.line([(36, 52), (24, 60)], fill="white", width=3)
    # _ cursor
    draw.line([(42, 60), (58, 60)], fill="white", width=3)


def draw_search(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Search — globe with latitude/longitude lines."""
    # Outer circle
    draw.ellipse([16, 16, 80, 80], outline="white", width=3)
    # Vertical center ellipse
    draw.ellipse([34, 16, 62, 80], outline="white", width=2)
    # Horizontal lines (latitudes)
    draw.line([(16, 48), (80, 48)], fill="white", width=2)
    draw.arc([16, 28, 80, 48], 0, 180, fill="white", width=2)
    draw.arc([16, 48, 80, 68], 180, 360, fill="white", width=2)


def draw_swarm(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Swarm — three connected dots (constellation pattern)."""
    # Three large dots in a triangle
    r = 10
    dots = [(48, 22), (26, 68), (70, 68)]
    # Lines connecting dots
    for i in range(len(dots)):
        for j in range(i + 1, len(dots)):
            draw.line([dots[i], dots[j]], fill="white", width=2)
    # Dots on top
    for x, y in dots:
        draw.ellipse([x - r, y - r, x + r, y + r], fill="white")


def draw_exit(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Exit — X symbol."""
    draw.line([(24, 24), (72, 72)], fill="white", width=5)
    draw.line([(72, 24), (24, 72)], fill="white", width=5)


def draw_brightness(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Brightness — sun with rays."""
    cx, cy = 48, 48
    # Center circle
    draw.ellipse([32, 32, 64, 64], fill="white")
    # Rays
    import math

    for angle_deg in range(0, 360, 45):
        angle = math.radians(angle_deg)
        x1 = cx + int(22 * math.cos(angle))
        y1 = cy + int(22 * math.sin(angle))
        x2 = cx + int(34 * math.cos(angle))
        y2 = cy + int(34 * math.sin(angle))
        draw.line([(x1, y1), (x2, y2)], fill="white", width=3)


def draw_ralph(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Ralph — loop/refresh arrows (circular)."""
    # Circular arrow — draw an arc with arrowheads
    draw.arc([18, 18, 78, 78], 30, 300, fill="white", width=4)
    # Arrowhead at end of arc (~300 degrees, which is roughly top-right)
    import math

    angle = math.radians(300)
    ex = 48 + int(30 * math.cos(angle))
    ey = 48 + int(30 * math.sin(angle))
    draw.polygon(
        [(ex - 2, ey - 12), (ex + 10, ey), (ex - 4, ey + 4)],
        fill="white",
    )
    # Arrowhead at start of arc (~30 degrees, bottom-right)
    angle2 = math.radians(30)
    sx = 48 + int(30 * math.cos(angle2))
    sy = 48 + int(30 * math.sin(angle2))
    draw.polygon(
        [(sx + 2, sy + 12), (sx - 10, sy), (sx + 4, sy - 4)],
        fill="white",
    )


def draw_test(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Test — flask/beaker."""
    # Neck
    draw.rectangle([38, 14, 58, 38], outline="white", width=3)
    # Body (trapezoid widening downward)
    draw.polygon(
        [(38, 38), (20, 78), (76, 78), (58, 38)],
        outline="white",
        fill=None,
    )
    draw.polygon(
        [(38, 38), (20, 78), (76, 78), (58, 38)],
        outline="white",
    )
    # Liquid level
    draw.polygon(
        [(30, 58), (24, 74), (72, 74), (66, 58)],
        fill="white",
    )
    # Rim at top
    draw.line([(32, 14), (64, 14)], fill="white", width=3)


def draw_cpu(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """CPU — processor chip with pins."""
    # Main chip body
    draw.rectangle([26, 26, 70, 70], outline="white", width=3)
    # Inner die
    draw.rectangle([36, 36, 60, 60], fill="white")
    # Pins on each side
    pin_len = 8
    for offset in [36, 48, 60]:
        # Top pins
        draw.line([(offset, 26), (offset, 26 - pin_len)], fill="white", width=2)
        # Bottom pins
        draw.line([(offset, 70), (offset, 70 + pin_len)], fill="white", width=2)
        # Left pins
        draw.line([(26, offset), (26 - pin_len, offset)], fill="white", width=2)
        # Right pins
        draw.line([(70, offset), (70 + pin_len, offset)], fill="white", width=2)


def draw_disk(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Disk — cylinder (database symbol)."""
    # Top ellipse
    draw.ellipse([18, 14, 78, 34], outline="white", width=3)
    draw.ellipse([18, 14, 78, 34], fill="white")
    # Body sides
    draw.line([(18, 24), (18, 68)], fill="white", width=3)
    draw.line([(78, 24), (78, 68)], fill="white", width=3)
    # Fill body
    draw.rectangle([19, 24, 77, 68], fill="white")
    # Bottom ellipse (visible part)
    draw.ellipse([18, 58, 78, 78], fill="white")
    draw.ellipse([18, 58, 78, 78], outline="white", width=3)
    # Make it look 3D — cut out the inside with darker shading
    # Actually keep it solid white for clean icon look at small sizes
    # Re-draw top cap slightly darker to show depth
    draw.ellipse([20, 16, 76, 32], fill=(220, 220, 220, 255))


def draw_seo(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """SEO — bar chart trending up."""
    # Bars
    bars = [(20, 68), (36, 54), (52, 40), (68, 26)]
    bar_w = 12
    for x, top in bars:
        draw.rectangle([x, top, x + bar_w, 80], fill="white")
    # Trend line
    draw.line(
        [(26, 64), (42, 50), (58, 36), (74, 22)],
        fill="white",
        width=3,
    )


def draw_content(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Content — document with pen."""
    # Document
    draw.rectangle([18, 14, 62, 82], outline="white", width=3)
    # Folded corner
    draw.polygon([(48, 14), (62, 14), (62, 28)], fill=(0, 0, 0, 0))
    draw.polygon([(48, 14), (48, 28), (62, 28)], outline="white", fill="white")
    # Text lines
    for y in [40, 52, 64]:
        draw.line([(26, y), (54, y)], fill="white", width=2)
    # Pen (diagonal, overlapping bottom-right)
    draw.line([(60, 82), (82, 50)], fill="white", width=4)
    draw.polygon([(58, 84), (60, 78), (64, 82)], fill="white")


def draw_kb(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Knowledge base — open book."""
    # Left page
    draw.polygon([(48, 22), (14, 30), (14, 76), (48, 70)], outline="white", fill=None)
    draw.line([(48, 22), (14, 30)], fill="white", width=2)
    draw.line([(14, 30), (14, 76)], fill="white", width=2)
    draw.line([(14, 76), (48, 70)], fill="white", width=2)
    # Right page
    draw.polygon([(48, 22), (82, 30), (82, 76), (48, 70)], outline="white", fill=None)
    draw.line([(48, 22), (82, 30)], fill="white", width=2)
    draw.line([(82, 30), (82, 76)], fill="white", width=2)
    draw.line([(82, 76), (48, 70)], fill="white", width=2)
    # Spine
    draw.line([(48, 22), (48, 70)], fill="white", width=2)
    # Left page lines
    for y in [40, 50, 60]:
        draw.line([(22, y + 2), (42, y)], fill="white", width=1)
    # Right page lines
    for y in [40, 50, 60]:
        draw.line([(54, y), (74, y + 2)], fill="white", width=1)


def draw_context7(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Context7 — documentation book with '7' badge."""
    # Book body
    draw.rectangle([20, 16, 68, 80], outline="white", width=3)
    # Spine
    draw.line([(28, 16), (28, 80)], fill="white", width=3)
    # Text lines on pages
    for y in [32, 44, 56, 68]:
        draw.line([(36, y), (60, y)], fill="white", width=2)
    # "7" badge in top-right corner
    draw.ellipse([58, 8, 84, 34], fill="white")
    font = _get_font(18)
    draw.text((71, 21), "7", fill=(0, 0, 0), font=font, anchor="mm")


def draw_commit(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Git commit — dot on a vertical line."""
    # Vertical line
    draw.line([(48, 10), (48, 86)], fill="white", width=3)
    # Commit dot
    draw.ellipse([34, 34, 62, 62], fill="white")
    # Inner dot (hollow effect)
    draw.ellipse([40, 40, 56, 56], fill=(0, 0, 0, 0))


def draw_push(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Push — bold up arrow."""
    # Arrow shaft
    draw.rectangle([40, 40, 56, 80], fill="white")
    # Arrowhead
    draw.polygon(
        [(48, 12), (22, 44), (74, 44)],
        fill="white",
    )


def draw_pull(draw: ImageDraw.ImageDraw, img: Image.Image) -> None:
    """Pull — bold down arrow."""
    # Arrow shaft
    draw.rectangle([40, 16, 56, 56], fill="white")
    # Arrowhead
    draw.polygon(
        [(48, 84), (22, 52), (74, 52)],
        fill="white",
    )


# ---------------------------------------------------------------------------
# Registry: filename → draw function
# ---------------------------------------------------------------------------

ICONS = {
    "git.png": draw_git,
    "pipeline.png": draw_pipeline,
    "claude.png": draw_claude,
    "build.png": draw_build,
    "research.png": draw_research,
    "validate.png": draw_validate,
    "scaffold.png": draw_scaffold,
    "plan.png": draw_plan,
    "deploy.png": draw_deploy,
    "review.png": draw_review,
    "tmux.png": draw_tmux,
    "search.png": draw_search,
    "swarm.png": draw_swarm,
    "exit.png": draw_exit,
    "brightness.png": draw_brightness,
    "ralph.png": draw_ralph,
    "test.png": draw_test,
    "cpu.png": draw_cpu,
    "disk.png": draw_disk,
    "seo.png": draw_seo,
    "content.png": draw_content,
    "kb.png": draw_kb,
    "context7.png": draw_context7,
    "commit.png": draw_commit,
    "push.png": draw_push,
    "pull.png": draw_pull,
}


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating {len(ICONS)} icons into {ASSETS_DIR}/")
    for name, draw_func in ICONS.items():
        make_icon(name, draw_func)
    print(f"Done — {len(ICONS)} icons generated.")


if __name__ == "__main__":
    main()
