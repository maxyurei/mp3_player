"""Offline bench for the spectrum analyser. Not imported by the app.

Tuning a visualiser against live playback is slow, unrepeatable and mostly
guesswork: you change a constant, play a song, squint at it, and cannot tell
whether what you saw was the change or the track. This replaces that loop.

Capture the decimated mono stream from real files once, then replay it through
dsp.py entirely offline — instant, deterministic and scoreable. Every number in
dsp.py's comments came from here.

    python tools/spectrum_lab.py capture         # once; writes captures.json
    python tools/spectrum_lab.py characterize    # what the raw dB range IS
    python tools/spectrum_lab.py validate        # the scorecard
    python tools/spectrum_lab.py sweep           # DB_FLOOR/DB_CEIL search
    python tools/spectrum_lab.py robustness      # quiet-material safety

The metric that matters is `beat` — the correlation between displayed bar
height and the frame's true loudness. The bug this bench was built to kill was
an analyser that normalised each band against its own frame's peak, making the
display algebraically independent of loudness: bars that never move with the
music, however lively the audio. Correlation catches that; nothing else did.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The ASCII bar strip uses block characters, and the Windows console still
# defaults to cp1252, which cannot encode them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import dsp  # noqa: E402

CAPTURES = Path(__file__).resolve().parent / "captures.json"

# Decimated rate is 48 kHz / DECIMATE; the analyser runs at ~20 fps.
HOP = 600
CAPTURE_MS = 22000
SEEK_MS = 8000  # start inside the track, not in its intro
BLOCKS = " ▁▂▃▄▅▆▇█"


# --- capture -----------------------------------------------------------------


def find_tracks(limit: int = 3) -> list[Path]:
    """A few mp3s from different playlist folders, for variety."""
    music = Path.home() / "Music"
    by_folder: dict[Path, Path] = {}
    for path in sorted(music.rglob("*.mp3")):
        by_folder.setdefault(path.parent, path)
    return list(by_folder.values())[:limit]


def capture(paths: list[Path]) -> None:
    from PySide6.QtCore import QCoreApplication, QTimer, QUrl
    from PySide6.QtMultimedia import QAudioBufferOutput, QAudioOutput, QMediaPlayer

    from player import _buffer_mono

    app = QCoreApplication(sys.argv)
    captures: dict[str, list[float]] = {}
    state: dict = {"index": 0}

    def next_track():
        if state["index"] >= len(paths):
            CAPTURES.write_text(json.dumps(captures))
            print(f"wrote {CAPTURES}")
            app.quit()
            return

        path = paths[state["index"]]
        print(f"capturing: {path.stem[:60]}")
        samples: list[float] = []

        # Volume 0: this decodes the file without making a sound.
        audio = QAudioOutput()
        audio.setVolume(0.0)
        player = QMediaPlayer()
        player.setAudioOutput(audio)
        sink = QAudioBufferOutput()
        player.setAudioBufferOutput(sink)
        sink.audioBufferReceived.connect(
            lambda buf: samples.extend(_buffer_mono(buf) or [])
        )

        def on_status(status):
            if status == QMediaPlayer.MediaStatus.LoadedMedia:
                player.setPosition(SEEK_MS)
                player.play()

        player.mediaStatusChanged.connect(on_status)
        player.setSource(QUrl.fromLocalFile(str(path)))

        def done():
            player.stop()
            captures[path.stem] = samples
            print(f"  {len(samples)} decimated samples")
            state["index"] += 1
            state.update(player=None, sink=None, audio=None)
            QTimer.singleShot(200, next_track)

        state.update(player=player, sink=sink, audio=audio)
        QTimer.singleShot(CAPTURE_MS, done)

    QTimer.singleShot(0, next_track)
    app.exec()


def load() -> dict[str, list[float]]:
    if not CAPTURES.exists():
        sys.exit("no captures.json — run `python tools/spectrum_lab.py capture` first")
    return json.loads(CAPTURES.read_text())


# --- analysis ----------------------------------------------------------------


def frames(samples: list[float]):
    for start in range(dsp.N, len(samples), HOP):
        yield samples[start - dsp.N : start]


def raw_bands(chunk: list[float]) -> list[float]:
    """Tilted absolute dB per band — dsp.compute's input, before mapping."""
    window = dsp._WINDOW
    re = [chunk[i] * window[i] for i in range(dsp.N)]
    im = [0.0] * dsp.N
    dsp._fft(re, im)
    mags = [0.0] * dsp._HALF
    for k in range(1, dsp._HALF):
        mags[k] = re[k] * re[k] + im[k] * im[k]
    out = []
    for index, (lo, hi) in enumerate(dsp._EDGES):
        energy = mags[lo] if hi - lo <= 0 else max(mags[lo:hi])
        db = 10.0 * math.log10(energy) if energy > 1e-20 else -300.0
        out.append(db + dsp._TILT[index])
    return out


def run(samples, floor=None, ceil=None, gain=1.0):
    """Replay through a fresh analyser. Returns (frames, offsets)."""
    if floor is not None:
        dsp.DB_FLOOR = floor
    if ceil is not None:
        dsp.DB_CEIL = ceil
    analyser = dsp.SpectrumAnalyser()
    out, offsets = [], []
    for chunk in frames(samples):
        analyser._buffer = [v * gain for v in chunk] if gain != 1.0 else list(chunk)
        analyser._filled = dsp.N
        out.append(analyser.compute())
        offsets.append(analyser._offset)
    return out, offsets


def score(rendered) -> dict:
    bands = len(rendered[0])
    total = sat = dead = count = 0.0
    delta = dcount = 0.0
    for i, frame in enumerate(rendered):
        for b in range(bands):
            v = frame[b]
            total += v
            count += 1
            if v >= 0.99:
                sat += 1
            elif v <= 0.01:
                dead += 1
            if i:
                delta += abs(v - rendered[i - 1][b])
                dcount += 1
    return {
        "level": total / count,
        "sat": sat / count,
        "dead": dead / count,
        "delta": delta / dcount if dcount else 0.0,
    }


def loudness(samples) -> list[float]:
    out = []
    for chunk in frames(samples):
        rms = math.sqrt(sum(v * v for v in chunk) / len(chunk))
        out.append(20.0 * math.log10(rms) if rms > 1e-12 else -240.0)
    return out


def correlation(xs, ys) -> float:
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else float("nan")


def pct(values, fraction):
    return values[min(len(values) - 1, int(len(values) * fraction))]


# --- commands ----------------------------------------------------------------


def cmd_characterize():
    pooled = []
    for name, samples in load().items():
        values = []
        for chunk in frames(samples):
            values.extend(raw_bands(chunk))
        values.sort()
        pooled.extend(values)
        print(f"{name[:40]:<41} p10={pct(values,0.10):>6.1f} "
              f"p50={pct(values,0.50):>6.1f} p97={pct(values,0.97):>6.1f}")
    pooled.sort()
    print("\npooled percentiles (dB):")
    for f in (0.01, 0.10, 0.25, 0.50, 0.75, 0.90, 0.97, 0.99):
        print(f"  p{f*100:>5.1f}  {pct(pooled, f):>8.2f}")
    silent = raw_bands([0.0] * dsp.N)
    print(f"\ndigital silence -> {min(silent):.0f}..{max(silent):.0f} dB "
          "(this is why the AGC must gate it)")


def cmd_validate():
    captures = load()
    print(f"DB_FLOOR={dsp.DB_FLOOR}  DB_CEIL={dsp.DB_CEIL}  "
          f"span={dsp.DB_CEIL - dsp.DB_FLOOR}\n")
    print(f"{'track':<41} {'level':>6} {'delta':>6} {'sat':>6} {'dead':>6} {'beat r':>7}")
    for name, samples in captures.items():
        rendered, _ = run(samples)
        s = score(rendered)
        loud = loudness(samples)
        n = min(len(loud), len(rendered))
        means = [sum(f) / len(f) for f in rendered[20:n]]
        r = correlation(loud[20:n], means)
        print(f"{name[:40]:<41} {s['level']:>6.3f} {s['delta']:>6.3f} "
              f"{s['sat']:>6.3f} {s['dead']:>6.3f} {r:>7.3f}")

    print("\nsilence must read ~0.000 (a broken AGC renders it at 0.600):")
    for name, samples in captures.items():
        analyser = dsp.SpectrumAnalyser()
        for chunk in frames(samples):
            analyser._buffer = list(chunk)
            analyser._filled = dsp.N
            analyser.compute()
        analyser._buffer = [0.0] * dsp.N
        tail = [analyser.compute() for _ in range(40)][-1]
        print(f"  {name[:40]:<41} {sum(tail)/len(tail):.4f}  "
              f"offset {analyser._offset:>6.2f} dB")

    name, samples = next(iter(captures.items()))
    rendered, _ = run(samples)
    print(f"\n{name[:60]} — 16 frames:")
    for frame in rendered[120:136]:
        print("  " + "".join(BLOCKS[min(8, int(v * 8.999))] for v in frame))


def cmd_sweep():
    captures = load()
    print(f"{'floor':>6} {'ceil':>6} {'span':>5} | {'level':>6} {'delta':>6} {'sat':>6}")
    print("(only the SPAN matters while the AGC is engaged — equal spans score "
          "identically)")
    for floor in (0.0, 2.0, 4.0, 6.0):
        for ceil in (22.0, 25.0, 28.0, 30.0, 34.0):
            if ceil - floor < 18:
                continue
            agg = {"level": 0.0, "delta": 0.0, "sat": 0.0}
            for samples in captures.values():
                s = score(run(samples, floor, ceil)[0])
                for k in agg:
                    agg[k] += s[k] / len(captures)
            print(f"{floor:>6.1f} {ceil:>6.1f} {ceil-floor:>5.1f} | "
                  f"{agg['level']:>6.3f} {agg['delta']:>6.3f} {agg['sat']:>6.3f}")


def cmd_robustness():
    captures = load()
    print("The library is loud YouTube rips; nothing enforces that. A display "
          "that\ndies on quiet material has a threshold hard-coded to this "
          "library.\n")
    print(f"{'gain':>6} {'track':<41} {'level':>7} {'delta':>7} {'offset':>8}")
    for db in (0, -10, -20, -30, -40):
        gain = 10.0 ** (db / 20.0)
        for name, samples in captures.items():
            rendered, offsets = run(samples, gain=gain)
            s = score(rendered)
            flag = "" if s["level"] > 0.10 else "  <-- DEAD"
            print(f"{db:>4} dB {name[:40]:<41} {s['level']:>7.3f} "
                  f"{s['delta']:>7.3f} {offsets[-1]:>8.2f}{flag}")
        print()


COMMANDS = {
    "characterize": cmd_characterize,
    "validate": cmd_validate,
    "sweep": cmd_sweep,
    "robustness": cmd_robustness,
}

if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "validate"
    if command == "capture":
        tracks = [Path(a) for a in sys.argv[2:]] or find_tracks()
        if not tracks:
            sys.exit("no mp3s found under ~/Music — pass paths explicitly")
        capture(tracks)
    elif command in COMMANDS:
        COMMANDS[command]()
    else:
        sys.exit(f"unknown command {command!r}; try: capture, "
                 f"{', '.join(COMMANDS)}")
