"""One shared workaround for how QMovie behaves at a loop boundary.

Both the ribbon and the stage decode frames at panel size via
`QMovie.setScaledSize()` — that is the optimisation that made a docked 1920px
strip affordable, since it stops a 500px GIF being decoded at 1920x1920 to show
a 96px band of it. Both then draw the frame at its natural size, because it is
supposed to arrive pre-scaled.

It does, except for the first frame of every loop. There, QMovie hands back the
frame at the *source's* own size and ignores the scaled size entirely, for
exactly one frame, before returning to normal:

    frame 38: 171x137   frame 39: 171x137   frame 0: 620x496   frame 1: 171x137

Drawn by code that assumes the frame is already the right size, that one frame
lands with the wrong geometry and the picture visibly jumps and snaps back —
once per loop, on every GIF. It is most obvious on the stage, where the frame is
biggest.

So nothing here trusts the incoming size: it is checked against what was asked
for, and corrected on the rare frame that disagrees. The cost is one scale on
one frame per loop, and zero on every other frame — the comparison is against a
QSize, so the common path is an integer compare.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QMovie, QPixmap


def honour_scaled_size(movie: QMovie, pixmap: QPixmap) -> QPixmap:
    """Return `pixmap` at the size the movie was told to produce."""
    wanted = movie.scaledSize()
    if wanted.isEmpty() or pixmap.size() == wanted:
        return pixmap
    return pixmap.scaled(
        wanted,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
