#!/usr/bin/env python3
"""Regenerate web/icons/*.png.

A build-time tool, not part of the app. Run it only when the icon design
changes:  python3 web/tools/make-icons.py

The icon is a record: a large white disc on near-black, so the tile reads as
mostly white and stays visible on a light or dark iPhone wallpaper. Everything
is drawn at 4x and downsampled, which is cheaper than finding an SVG
rasteriser and gives clean edges at 32 px.
"""

from pathlib import Path

from PIL import Image, ImageDraw

BG = (10, 10, 11, 255)  # --bg from css/app.css
DISC = (244, 244, 245, 255)  # --text
SUPERSAMPLE = 4

ICONS = Path(__file__).resolve().parent.parent / "icons"


def draw(size: int, glyph_scale: float) -> Image.Image:
    """Render one icon. glyph_scale shrinks the record for maskable icons,
    whose outer ~20% can be cropped to any shape by the launcher."""
    px = size * SUPERSAMPLE
    image = Image.new("RGBA", (px, px), BG)
    pen = ImageDraw.Draw(image)
    centre = px / 2

    def circle(radius_fraction: float, fill):
        r = px * radius_fraction * glyph_scale
        pen.ellipse(
            [centre - r, centre - r, centre + r, centre + r], fill=fill
        )

    circle(0.33, DISC)  # the record
    circle(0.235, BG)  # a groove, so it reads as vinyl and not a dot
    circle(0.215, DISC)
    circle(0.075, BG)  # spindle hole

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    # 192/512 are the PWA manifest sizes; 180 is what iOS uses for the home
    # screen; 32 is the browser tab. Maskable gets its own file because the
    # glyph has to sit inside the safe zone.
    for name, size, scale in [
        ("icon-192.png", 192, 1.0),
        ("icon-512.png", 512, 1.0),
        ("icon-maskable-512.png", 512, 0.78),
        ("apple-touch-icon-180.png", 180, 1.0),
        ("favicon-32.png", 32, 1.0),
    ]:
        draw(size, scale).save(ICONS / name, "PNG", optimize=True)
        print(f"wrote {name}")


if __name__ == "__main__":
    main()
