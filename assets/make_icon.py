"""Generate assets/icon.ico — the app's custom neon play-triangle icon.

Rendered with QPainter (no extra dependencies) in the app's palette: a dark
violet-tinted rounded tile with a glowing gradient play triangle. Tweak the
colors/geometry below and re-run to regenerate:

    python assets/make_icon.py

Writes a multi-size .ico with PNG-compressed entries (supported since Vista).
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)

# Standard Windows icon sizes: taskbar, title bar, alt-tab, Explorer views.
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = float(size)
    rect = QRectF(0, 0, s, s)
    radius = s * 0.22

    # Dark rounded tile (matches the app's #131316/#1a1a20 base).
    tile = QPainterPath()
    tile.addRoundedRect(rect, radius, radius)
    bg = QLinearGradient(0, 0, 0, s)
    bg.setColorAt(0.0, QColor("#1d1d25"))
    bg.setColorAt(1.0, QColor("#111114"))
    p.fillPath(tile, bg)

    # Neon glow pooled behind the triangle.
    p.setClipPath(tile)
    glow = QRadialGradient(QPointF(s * 0.52, s * 0.50), s * 0.55)
    glow.setColorAt(0.0, QColor(160, 107, 255, 115))
    glow.setColorAt(1.0, QColor(160, 107, 255, 0))
    p.fillRect(rect, glow)

    # Play triangle, nudged right of center so it reads optically centered.
    left, right = s * 0.36, s * 0.74
    top, bottom = s * 0.27, s * 0.73
    tri = QPainterPath()
    tri.moveTo(left, top)
    tri.lineTo(right, s * 0.5)
    tri.lineTo(left, bottom)
    tri.closeSubpath()
    fill = QLinearGradient(left, top, right, bottom)
    fill.setColorAt(0.0, QColor("#d3b8ff"))
    fill.setColorAt(1.0, QColor("#8b54ea"))
    p.setPen(Qt.PenStyle.NoPen)
    p.fillPath(tri, fill)

    # Faint violet rim so the tile edge glows against dark taskbars.
    p.setClipping(False)
    pen = QPen(QColor(160, 107, 255, 90), max(1.0, s * 0.02))
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    inset = pen.widthF() / 2
    p.drawRoundedRect(
        rect.adjusted(inset, inset, -inset, -inset), radius - inset, radius - inset
    )

    p.end()
    return img


def write_ico(images: list[QImage], dest: Path) -> None:
    pngs: list[bytes] = []
    for img in images:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buffer, "PNG")
        buffer.close()
        pngs.append(bytes(data))

    # ICONDIR header, then one ICONDIRENTRY per image, then the PNG blobs.
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    for img, png in zip(images, pngs):
        width = img.width() if img.width() < 256 else 0  # 0 encodes 256
        height = img.height() if img.height() < 256 else 0
        entries += struct.pack("<BBBBHHII", width, height, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    dest.write_bytes(header + entries + b"".join(pngs))


def main() -> None:
    # QPainter needs a Gui application to exist (fonts/paint engine init).
    QGuiApplication(sys.argv)
    dest = Path(__file__).parent / "icon.ico"
    write_ico([render(size) for size in SIZES], dest)
    print(f"Wrote {dest}")


if __name__ == "__main__":
    main()
