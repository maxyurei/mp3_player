# mp3_player

A lightweight desktop music player for coding — a thin always-visible ribbon,
an optional visual stage that can sink to the desktop layer as live wallpaper,
and in-app YouTube search/download. Windows-first (PySide6 + Qt Multimedia).

## Surfaces

| Surface | What it is |
| --- | --- |
| **Ribbon** | The app itself. A slim bar with transport, track list and the audio-reactive glow. Closing it quits. |
| **Stage** | An opened visual panel. Can be reparented onto the desktop layer, behind every window, as an animated wallpaper. |
| **Dock** | Registers a Windows appbar so a maximised editor stops short of the ribbon instead of hiding behind it. |

## Requirements

- **Python 3.12+**
- **ffmpeg on PATH** — not a pip package, needed for downloads:
  `winget install Gyan.FFmpeg`
- PySide6 **6.8+** is a hard floor: the audio-reactive glow taps decoded PCM
  through `QAudioBufferOutput`, which did not exist before 6.8.

## Setup

```bash
git clone https://github.com/maxyurei/mp3_player.git
cd mp3_player
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Run it:

```bash
python main.py
```

or double-click `MP3Player.bat`, which launches under `pythonw` so no console
window hangs around.

Keep yt-dlp current — it breaks whenever YouTube changes something:

```bash
pip install -U yt-dlp
```

## Your music and visuals are not in this repo

Both are intentionally local to each machine, so a fresh clone starts empty:

- **Music** — the player scans `~/Music` by default; point it anywhere from the
  ribbon. Downloads land as `Title [videoid].mp3`, where the id doubles as a
  cache key. No audio is tracked in git.
- **Visuals** — drop any `.gif` or `.webp` into `assets/visuals/` and it joins
  the rotation automatically; subfolders are one shared pool. Each track keeps
  whichever visual it was dealt, so a song looks the same every time. See
  `assets/visuals/README.txt`. The frames themselves are gitignored (third-party
  art, and ~78 MB of it), so this folder arrives holding only its README.

Settings and listening history live in `AppData` via `QSettings`, not in the
working tree.

## Layout

```
main.py          entry point — app identity, icon, stylesheet, shutdown wiring
player.py        playback engine over QMediaPlayer
library.py       folder scan, ID3 tags, album art
dsp.py           FFT / spectrum analysis feeding the reactive glow
visuals.py       visual pool: discovery and per-track assignment
history.py       play counts and listening history (AppData)
radio.py         recommendations built from your own listening — no account, no cookies
downloader.py    YouTube search + download, yt-dlp as a subprocess
ui/              ribbon, stage, dock, wallpaper, palette, theme, hotkeys
tools/           spectrum_lab.py — offline bench for tuning the analyser
attic/           retired code kept for reference; not imported by the app
```

## A note on `attic/`

Dead code from earlier iterations, including a Discover panel that read your
real YouTube feeds and therefore needed exported browser cookies that expired
every few days. `radio.py` replaced it precisely to avoid that — the live app
never touches cookies or an account. Any `cookies.txt` is a live Google session
rather than a config file, and `.gitignore` refuses to track one.
