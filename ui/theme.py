"""Single source of truth for the palette.

Colours live here rather than only in styles.qss because the custom-painted
widgets (segmented sliders, art panel, level meter) need them as Python values.
styles.qss mirrors these hexes — if you change one, change both.

Accent discipline: VIOLET is the interactive accent (focus, fills, the play
button). CYAN means one thing only — *live audio* — so it is reserved for the
playhead, the playing row, the level meter and the PLAYING readout. Using it
anywhere else dilutes the signal.
"""

BG = "#0b0b0e"
SCREEN = "#08080a"  # inside the art bezel — darker than the app background
PANEL = "#121218"
RAISED = "#1c1c24"
RAISED_HI = "#262630"
BORDER = "#23232d"
BORDER_HI = "#383846"

TEXT = "#ecedf3"
MUTED = "#8b8b9a"
SUBDUED = "#55555f"

VIOLET = "#a06bff"
VIOLET_HI = "#c4a0ff"
VIOLET_DIM = "#6f3fd4"

CYAN = "#2ee8d5"
CYAN_DIM = "#17a99c"

DANGER = "#ff5c6c"

# Spectrum bars. Fixed, desaturated, and identical for every track.
#
# These were previously tinted from a colour sampled out of the current GIF,
# which meant the strip changed character track to track and occasionally
# landed on something garish. A spectrum is chrome — it should read as part of
# the app, not as part of the artwork sitting behind it. The gradient runs from
# a dim slate at the baseline to a lifted grey-violet at the top, so bar height
# reads as brightness as well as length.
#
# Note these are deliberately NOT cyan, including the peak caps, which used to
# be. Cyan means live audio and belongs to the playhead alone; a strip of 96
# cyan bars is the loudest possible way to dilute that.
SPECTRUM_LO = "#3b3b4d"
SPECTRUM_HI = "#8f86b8"
SPECTRUM_CAP = "#b9b2d6"
