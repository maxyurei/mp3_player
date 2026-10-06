import array

from PySide6.QtCore import QElapsedTimer, QObject, QUrl, Signal

import dsp
from PySide6.QtMultimedia import (
    QAudio,
    QAudioBuffer,
    QAudioBufferOutput,
    QAudioFormat,
    QAudioOutput,
    QMediaDevices,
    QMediaPlayer,
)

# How many samples of each decoded buffer to actually look at. A buffer can
# hold thousands; 256 is plenty to get the loudness right, and keeps this loop
# cheap enough to run in pure Python on the audio path.
_LEVEL_SAMPLE_CAP = 256

# Cap on frames consumed per buffer for the spectrum. Only the most recent
# dsp.N decimated samples survive into the transform anyway, so reading more
# than this is work whose result is immediately discarded.
_SPECTRUM_FRAME_CAP = dsp.N * dsp.DECIMATE

# array typecode and full-scale divisor per sample format. Anything not listed
# (unusual float widths, etc.) simply yields no level, and the UI stays static.
_SAMPLE_CODECS = {
    QAudioFormat.SampleFormat.UInt8: ("B", 128.0, 128.0),
    QAudioFormat.SampleFormat.Int16: ("h", 32768.0, 0.0),
    QAudioFormat.SampleFormat.Int32: ("i", 2147483648.0, 0.0),
    QAudioFormat.SampleFormat.Float: ("f", 1.0, 0.0),
}


def _buffer_level(buffer: QAudioBuffer) -> float | None:
    """RMS of a decoded buffer as 0..1. No numpy, no FFT — just a mean square.

    A spectrum analyser would need an FFT, and an FFT would need numpy (~15 MB
    of dependency for decoration). Amplitude alone is enough to drive a glow,
    and costs a couple of hundred multiplications.
    """
    codec = _SAMPLE_CODECS.get(buffer.format().sampleFormat())
    if codec is None:
        return None
    typecode, full_scale, bias = codec

    try:
        samples = array.array(typecode)
        samples.frombytes(bytes(buffer.constData()))
    except (TypeError, ValueError):
        return None
    if not samples:
        return None

    step = max(1, len(samples) // _LEVEL_SAMPLE_CAP)
    total = 0.0
    count = 0
    for i in range(0, len(samples), step):
        value = (samples[i] - bias) / full_scale
        total += value * value
        count += 1
    if not count:
        return None
    return (total / count) ** 0.5


def _buffer_mono(buffer: QAudioBuffer) -> list[float] | None:
    """Decoded buffer -> mono samples decimated by dsp.DECIMATE.

    Averaging each group of DECIMATE consecutive frames rather than picking one
    of them is deliberate: the average is a crude low-pass, which is what keeps
    content above the new Nyquist from aliasing back down into the bass bars as
    phantom energy.
    """
    fmt = buffer.format()
    codec = _SAMPLE_CODECS.get(fmt.sampleFormat())
    if codec is None:
        return None
    typecode, full_scale, bias = codec

    try:
        samples = array.array(typecode)
        samples.frombytes(bytes(buffer.constData()))
    except (TypeError, ValueError):
        return None
    if not samples:
        return None

    channels = max(1, fmt.channelCount())
    frames = len(samples) // channels
    if frames < dsp.DECIMATE:
        return None
    if frames > _SPECTRUM_FRAME_CAP:
        # Keep the most recent frames; the ring only retains dsp.N anyway.
        frames = _SPECTRUM_FRAME_CAP
    start = len(samples) // channels - frames

    step = dsp.DECIMATE
    scale = 1.0 / (full_scale * step * channels)
    out = []
    append = out.append
    for group in range(start, start + frames - step + 1, step):
        total = 0.0
        base = group * channels
        for offset in range(step * channels):
            total += samples[base + offset] - bias
        append(total * scale)
    return out


class Player(QObject):
    source_changed = Signal(str)
    playback_state_changed = Signal(QMediaPlayer.PlaybackState)
    media_ended = Signal()
    position_changed = Signal(int)
    duration_changed = Signal(int)
    level_changed = Signal(float)
    spectrum_changed = Signal(list)  # dsp.BANDS values in 0..1, low to high

    # Envelope follower: snap up on a transient, ease down afterwards. Equal
    # coefficients would make the glow strobe on every kick drum.
    _ATTACK = 0.55
    _RELEASE = 0.10
    # Buffers arrive far faster than the display refreshes.
    _EMIT_INTERVAL_MS = 40
    # The spectrum runs slower than the level. Measured at 0.24 ms per
    # transform, 20 fps costs about 0.5% of one core — cheap, but there is no
    # reason to pay it 25 times a second for a strip nobody is studying.
    _SPECTRUM_INTERVAL_MS = 50
    # RMS treated as "meter pinned". Measured against real tracks: a loud
    # master runs ~0.3, so this leaves a little headroom above it.
    _FULL_SCALE_RMS = 0.40

    def __init__(self) -> None:
        super().__init__()
        # QAudioOutput must outlive QMediaPlayer; keep it as an attribute so it
        # is not garbage-collected while the player is still using it.
        self._audio_output = QAudioOutput()
        self._player = QMediaPlayer()
        self._player.setAudioOutput(self._audio_output)

        # Tap the decoded PCM so the UI can react to the actual audio. Like
        # QAudioOutput this must outlive the constructor, so keep a reference.
        self._buffer_output = QAudioBufferOutput()
        self._player.setAudioBufferOutput(self._buffer_output)
        self._buffer_output.audioBufferReceived.connect(self._on_audio_buffer)
        self._level = 0.0
        self._level_clock = QElapsedTimer()
        self._level_clock.start()

        self._analyser = dsp.SpectrumAnalyser()
        self._spectrum_clock = QElapsedTimer()
        self._spectrum_clock.start()
        # Set false while no surface is showing a spectrum, so the transform
        # isn't run for nobody. This is the same pausability contract the
        # visuals obey: hidden means stopped, not merely ignored.
        self._spectrum_wanted = True

        # QAudioOutput binds to whatever the default output device was when it
        # was created, and does NOT follow the OS afterwards. So plugging in
        # headphones or connecting Bluetooth would keep audio on the old device.
        # Watch for device changes and re-point the output at the current
        # default. QMediaDevices must be kept as an attribute so it stays alive
        # to keep emitting the signal.
        self._media_devices = QMediaDevices()
        self._media_devices.audioOutputsChanged.connect(self._sync_output_device)

        self._player.playbackStateChanged.connect(self.playback_state_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        # Route position/duration through slots: Qt emits qlonglong, our
        # Signal(int) is 32-bit, so a direct .connect() refuses to wire them.
        self._player.positionChanged.connect(self._on_position_changed)
        self._player.durationChanged.connect(self._on_duration_changed)

    def _sync_output_device(self) -> None:
        # Fires when audio output devices are added/removed (e.g. headphones
        # plugged in, Bluetooth connected/disconnected). Follow the new system
        # default; playback continues uninterrupted on the new device.
        self._audio_output.setDevice(QMediaDevices.defaultAudioOutput())

    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.media_ended.emit()

    def set_spectrum_wanted(self, wanted: bool) -> None:
        self._spectrum_wanted = wanted
        if not wanted:
            self._analyser.reset()

    def _on_audio_buffer(self, buffer: QAudioBuffer) -> None:
        if self._spectrum_wanted:
            self._update_spectrum(buffer)

        rms = _buffer_level(buffer)
        if rms is None:
            return
        coefficient = self._ATTACK if rms > self._level else self._RELEASE
        self._level += (rms - self._level) * coefficient
        if self._level_clock.elapsed() < self._EMIT_INTERVAL_MS:
            return
        self._level_clock.restart()
        # Map the band real music actually occupies (RMS ~0..0.4) onto the full
        # range, then expand slightly. Raw RMS parks the meter near the bottom;
        # a plain square root parks it near the top. Loud masters legitimately
        # sit high — this is an output meter, not an excitement meter.
        self.level_changed.emit(min(1.0, self._level / self._FULL_SCALE_RMS) ** 0.7)

    def _update_spectrum(self, buffer: QAudioBuffer) -> None:
        mono = _buffer_mono(buffer)
        if mono:
            self._analyser.push(mono)
        if not self._analyser.ready:
            return
        if self._spectrum_clock.elapsed() < self._SPECTRUM_INTERVAL_MS:
            return
        self._spectrum_clock.restart()
        self.spectrum_changed.emit(self._analyser.compute())

    def _reset_level(self) -> None:
        self._level = 0.0
        self._analyser.reset()
        self.level_changed.emit(0.0)
        # Flat bars rather than a frozen snapshot: leaving the last frame up
        # makes a paused player look like a stalled one.
        self.spectrum_changed.emit([0.0] * self._analyser.bands)

    def _on_position_changed(self, ms: int) -> None:
        self.position_changed.emit(ms)

    def _on_duration_changed(self, ms: int) -> None:
        self.duration_changed.emit(ms)

    def load(self, path: str) -> None:
        self._player.setSource(QUrl.fromLocalFile(path))
        self.source_changed.emit(path)

    def clear(self) -> None:
        # Stop and detach the source so the OS releases its lock on the file.
        # On Windows a file that's still set as the source can't be moved or
        # deleted, so callers must clear() before removing the current track.
        self._player.stop()
        self._player.setSource(QUrl())
        self._reset_level()

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        # Buffers stop arriving while paused, so the envelope would otherwise
        # freeze at whatever it was when the music stopped.
        self._player.pause()
        self._reset_level()

    def toggle_play_pause(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.pause()
        else:
            self.play()

    def set_position(self, ms: int) -> None:
        self._player.setPosition(ms)

    def set_volume(self, percent: int) -> None:
        # QAudioOutput.setVolume is linear amplitude, but perceived loudness is
        # logarithmic — a linear slider spends half its travel nearly silent.
        # Convert so the slider tracks perceived loudness.
        linear = QAudio.convertVolume(
            max(0.0, min(1.0, percent / 100.0)),
            QAudio.VolumeScale.LogarithmicVolumeScale,
            QAudio.VolumeScale.LinearVolumeScale,
        )
        self._audio_output.setVolume(linear)

    @property
    def position(self) -> int:
        return self._player.position()

    @property
    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
