"""A spectrum analyser small enough to justify itself.

The old visualiser drew one amplitude number per frame, which is why it never
looked like anything: with loudness alone every part of the drawing moves
together, so there is no internal structure for the eye to lock onto. Real
frequency data is what makes bass punch on the kick while the highs shimmer
independently.

Doing that normally means an FFT, and an FFT normally means numpy — about 15 MB
of dependency for decoration, which this project has always refused. So this is
a hand-rolled radix-2 FFT, and every decision in it is about making that
affordable in pure Python:

  * N = 256, not 1024. Eight stages of 128 butterflies is 1024 butterflies per
    frame; 1024-point would be five times that for detail nobody can see in a
    96px strip.
  * The signal is decimated 4x first. Music visualisation lives below ~5 kHz,
    and decimating drops the sample rate to ~11 kHz so a 256-point transform
    covers 0-5.5 kHz at 43 Hz resolution — better bass detail than a
    full-rate 256-point FFT would give, for a quarter of the input.
  * Twiddle factors, the Hann window and the bit-reversal permutation are all
    precomputed once at import.
  * The inner loop keeps everything in locals. Attribute lookups dominate
    otherwise.
  * It runs at ~20 fps, not at buffer rate. Buffers arrive far faster than
    anything needs to be redrawn.

Cost is zero when playback stops, because nothing calls in.
"""

from __future__ import annotations

import math

# --- transform size ---------------------------------------------------------

N = 256
_HALF = N // 2

# Input decimation. 4x puts Nyquist at ~5.5 kHz for 44.1 kHz source audio.
DECIMATE = 4

# Visible band count. 32 reads well across a docked 1920px bar; narrow surfaces
# merge them down rather than re-running the transform at another size.
BANDS = 32

# Band energies are mapped from this window onto 0..1. These are ABSOLUTE
# levels — 10*log10 of the windowed power of samples in [-1, 1] — not values
# relative to the loudest band in the frame.
#
# That distinction is the whole point. The first version normalised each band
# against the frame's own peak, which yields the spectrum's *shape* and discards
# its *loudness*. When a beat lands, every band rises together, the ratios are
# unchanged, and nothing moves — the display coasts, and it does so worst
# exactly when the music is most rhythmic.
#
# The test that settles it is correlating displayed bar height against the
# frame's true RMS loudness, because frame-relative math is algebraically
# independent of loudness and so must score ~0 however it is tuned. Measured
# over three captured tracks: this code scores r = +0.75 / +0.57 / +0.70,
# against +0.25 / -0.13 / -0.50 for the frame-relative approach — which on one
# track was moving *opposite* to the music.
#
# Window chosen from the measured distribution of this library — 3 real tracks,
# ~19 s each, pooled over every band of every frame: p10 sits at 0 dB, p50 at
# 12 dB, p97 at 25 dB. A band's own swing over time is therefore about 25 dB
# wide, so the window has to be roughly that or both ends spend their time
# clamped, and a clamped bar is a bar that cannot move.
#
# Worth knowing before tuning these: while the AGC is engaged, only the SPAN
# between them affects the picture. The offset re-centres every frame against
# DB_CEIL, so sliding both by the same amount changes nothing — a sweep over
# the captures confirmed identical scores for every pair sharing a span. The
# span was chosen at 24 dB, where movement peaks (0.092) with saturation still
# at 0.001; the absolute placement was then chosen so the offset rests near
# 0 dB for typical material, which keeps the clamps far out of the way.
DB_FLOOR = 6.0
DB_CEIL = 30.0

# Slow loudness alignment, so a quiet master still fills the strip and a loud
# one doesn't peg it. Deliberately sluggish (time constant of several seconds):
# anything faster starts tracking the beat itself and reintroduces the coasting
# this replaced.
AGC_RATE = 0.010
AGC_HEADROOM = 6.0

# Silence, gaps and fades must not steer the AGC. Letting them through is what
# broke the first version of this: the offset was seeded from one frame that
# happened to be near-silent (-281 dB), which asked for +300 dB of correction
# and pegged every bar at full height for ten seconds while it decayed back. It
# also explains the other symptom, silence rendering at 0.600 instead of black —
# 0.600 is exactly where the AGC's own target lands once the offset is that far
# out. Gate the AGC and both symptoms go together.
#
# The gate is relative to the loudest the analyser has actually heard, not a
# fixed dB value. A fixed value silently encodes "every file is a loud YouTube
# rip", which this library happens to be but nothing enforces: measured against
# the same tracks attenuated 30 dB, an absolute 0 dB gate left the display
# completely dead. Relative, the same material works at any level.
AGC_GATE_BELOW_PEAK = 25.0

# The one thing the relative gate cannot do is judge the very first frame, when
# there is no peak yet. This absolute floor covers only that: it sits far below
# any real music (the quietest test frames land near +11 dB, and 30 dB of
# attenuation only takes them to -19) and far above digital silence at -281.
AGC_GATE_FLOOR_DB = -40.0

# How fast the reference peak forgets. It rises instantly and falls slowly, so a
# track that gets quieter drags the gate down with it rather than gating itself
# out entirely.
AGC_PEAK_RATE = 0.001

# Hard bounds on the offset. With the gate doing the real work these are only a
# backstop against pathology — wide enough not to clip genuinely quiet material
# (30 dB of attenuation needs about 32 dB of correction), narrow enough that the
# +300 dB excursion that caused the original bug could never be honoured.
AGC_MIN_OFFSET = -60.0
AGC_MAX_OFFSET = 60.0

# The offset starts at 0 dB and converges. That default is not arbitrary: the
# window above is placed so a normal track asks for an offset near zero, which
# means the display opens correct rather than opening wrong and correcting.
#
# The rate starts high and decays as evidence accumulates (a running-mean
# schedule), reaching the slow floor after ~200 frames — fast while it knows
# little, sluggish once it doesn't. A fixed slow rate instead needed 13.6 s to
# walk into range on one test track, which is most of the time anyone spends
# looking at it.
AGC_CONVERGE = 2.0

# ...but never let one frame carry the offset more than a quarter of the way.
# Every version of this bug has been a single frame being believed outright: it
# is what pegged the display at +300 dB, and with that fixed it still flashed
# the first frame of one track to 83% brightness before settling. A cap costs
# nothing and makes that whole class of mistake unrepresentable.
AGC_MAX_RATE = 0.25

# Spectral tilt, in dB per octave, applied before the dB mapping. Music is
# heavily bass-weighted, so a flat display leaves the top third permanently
# dark. +3 dB/octave is the usual pink-noise compensation and makes the treble
# bars earn their space.
TILT_DB_PER_OCTAVE = 3.0

# Per-band envelope. Snap up on a transient, fall fast enough to be back down
# before the next one — a slow release smears consecutive hits into a plateau,
# which is another way to make a lively track look static.
ATTACK = 0.80
RELEASE = 0.40

# --- precomputed tables -----------------------------------------------------


def _bit_reverse_pairs(n: int) -> list[tuple[int, int]]:
    """Index swaps for the bit-reversal permutation, computed once.

    Storing only i<j pairs means the permutation is a straight list of swaps
    with no per-element bit twiddling at runtime.
    """
    bits = n.bit_length() - 1
    pairs = []
    for i in range(n):
        j = 0
        x = i
        for _ in range(bits):
            j = (j << 1) | (x & 1)
            x >>= 1
        if i < j:
            pairs.append((i, j))
    return pairs


_REV = _bit_reverse_pairs(N)
_COS = [math.cos(-2.0 * math.pi * k / N) for k in range(_HALF)]
_SIN = [math.sin(-2.0 * math.pi * k / N) for k in range(_HALF)]
_WINDOW = [0.5 - 0.5 * math.cos(2.0 * math.pi * i / (N - 1)) for i in range(N)]


def _band_edges(count: int, lo_bin: int = 1, hi_bin: int = _HALF - 1) -> list[tuple[int, int]]:
    """Log-spaced bin ranges. Linear spacing would spend most bars on treble.

    Every band is forced to be at least one bin wide, so the low end doesn't
    collapse into duplicates at high band counts.
    """
    edges = []
    previous = lo_bin
    for index in range(1, count + 1):
        ratio = index / count
        top = int(round(lo_bin * (hi_bin / lo_bin) ** ratio))
        top = max(top, previous + 1)
        edges.append((previous, min(top, hi_bin)))
        previous = top
        if previous >= hi_bin:
            # Ran out of spectrum; pad the rest against the top bin so the
            # returned list always has `count` entries.
            while len(edges) < count:
                edges.append((hi_bin - 1, hi_bin))
            break
    return edges[:count]


_EDGES = _band_edges(BANDS)


def _tilt_table(edges: list[tuple[int, int]]) -> list[float]:
    """Per-band dB boost rising with frequency."""
    reference = (edges[0][0] + edges[0][1]) / 2.0 or 1.0
    table = []
    for lo, hi in edges:
        centre = (lo + hi) / 2.0 or 1.0
        octaves = math.log(centre / reference, 2) if centre > 0 else 0.0
        table.append(TILT_DB_PER_OCTAVE * octaves)
    return table


_TILT = _tilt_table(_EDGES)


def _fft(re: list[float], im: list[float]) -> None:
    """In-place iterative radix-2 decimation-in-time FFT. N is a power of two."""
    for i, j in _REV:
        re[i], re[j] = re[j], re[i]
        im[i], im[j] = im[j], im[i]

    cos_table = _COS
    sin_table = _SIN
    size = 2
    while size <= N:
        half = size >> 1
        step = N // size
        for start in range(0, N, size):
            k = 0
            for a in range(start, start + half):
                b = a + half
                wr = cos_table[k]
                wi = sin_table[k]
                rb = re[b]
                ib = im[b]
                tr = rb * wr - ib * wi
                ti = rb * wi + ib * wr
                re[b] = re[a] - tr
                im[b] = im[a] - ti
                re[a] += tr
                im[a] += ti
                k += step
        size <<= 1


class SpectrumAnalyser:
    """Accumulates decimated mono samples and produces smoothed band levels."""

    def __init__(self, bands: int = BANDS) -> None:
        self.bands = bands
        self._edges = _EDGES if bands == BANDS else _band_edges(bands)
        self._tilt = _TILT if bands == BANDS else _tilt_table(self._edges)
        self._buffer: list[float] = [0.0] * N
        self._filled = 0
        self._levels = [0.0] * bands
        # Slow loudness offset in dB, applied on top of the absolute window.
        self._offset = 0.0
        # Non-silent frames seen since the last reset; drives the AGC's
        # decaying convergence rate.
        self._observed = 0
        # Loudest band level heard recently, in dB. The gate is measured
        # against this rather than against a fixed threshold.
        self._peak = -1e9
        self.level = 0.0

    def reset(self) -> None:
        self._buffer = [0.0] * N
        self._filled = 0
        self._levels = [0.0] * self.bands
        # Re-converge on the next track rather than carrying the previous one's
        # offset across, which would leave the first seconds of a quieter song
        # flat.
        self._observed = 0
        self._peak = -1e9
        self._offset = 0.0
        self.level = 0.0

    def push(self, mono: list[float]) -> None:
        """Add already-mono, already-decimated samples to the ring."""
        if not mono:
            return
        buffer = self._buffer
        count = len(mono)
        if count >= N:
            buffer[:] = mono[-N:]
            self._filled = N
            return
        # Shift left by `count` and append. A deque would avoid the copy, but
        # the FFT needs random access, and N is only 256.
        del buffer[:count]
        buffer.extend(mono)
        self._filled = min(N, self._filled + count)

    @property
    def ready(self) -> bool:
        return self._filled >= N

    def compute(self) -> list[float]:
        """Run one transform and return `bands` values in 0..1."""
        window = _WINDOW
        source = self._buffer
        re = [source[i] * window[i] for i in range(N)]
        im = [0.0] * N

        _fft(re, im)

        magnitudes = [0.0] * _HALF
        for k in range(1, _HALF):
            magnitudes[k] = re[k] * re[k] + im[k] * im[k]

        edges = self._edges
        tilt = self._tilt
        bands = self.bands

        # Absolute band levels in dB, tilted. No frame-relative reference here:
        # see DB_FLOOR for why that was the bug.
        raw = [0.0] * bands
        loudest = -300.0
        for index in range(bands):
            lo, hi = edges[index]
            if hi - lo <= 0:  # degenerate band: one bin wide
                energy = magnitudes[lo]
            else:
                energy = 0.0
                for k in range(lo, hi):
                    if magnitudes[k] > energy:
                        energy = magnitudes[k]
            # 10*log10, not 20: these values are already squared magnitudes.
            db = 10.0 * math.log10(energy) if energy > 1e-20 else -300.0
            db += tilt[index]
            raw[index] = db
            if db > loudest:
                loudest = db

        # Slow loudness alignment. Only frames that actually contain audio are
        # allowed to steer it — see AGC_GATE_DB for what happens otherwise.
        # Reference peak: instant attack, very slow release.
        if loudest > self._peak:
            self._peak = loudest
        else:
            self._peak += (loudest - self._peak) * AGC_PEAK_RATE

        gate = self._peak - AGC_GATE_BELOW_PEAK
        if gate < AGC_GATE_FLOOR_DB:
            gate = AGC_GATE_FLOOR_DB
        if loudest >= gate:
            self._observed += 1
            wanted = (DB_CEIL - AGC_HEADROOM) - loudest
            # Decaying rate: converge while the evidence is thin, then lock into
            # AGC_RATE so the offset stops reacting to the beat.
            rate = AGC_CONVERGE / (self._observed + 1.0)
            if rate > AGC_MAX_RATE:
                rate = AGC_MAX_RATE
            elif rate < AGC_RATE:
                rate = AGC_RATE
            self._offset += (wanted - self._offset) * rate
            if self._offset < AGC_MIN_OFFSET:
                self._offset = AGC_MIN_OFFSET
            elif self._offset > AGC_MAX_OFFSET:
                self._offset = AGC_MAX_OFFSET

        levels = self._levels
        offset = self._offset
        span = DB_CEIL - DB_FLOOR
        total = 0.0
        for index in range(bands):
            target = (raw[index] + offset - DB_FLOOR) / span
            if target < 0.0:
                target = 0.0
            elif target > 1.0:
                target = 1.0
            current = levels[index]
            current += (target - current) * (ATTACK if target > current else RELEASE)
            levels[index] = current
            total += current

        self.level = total / bands if bands else 0.0
        return list(levels)
