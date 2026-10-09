// The one and only <audio> element, reused for every track.
//
// Single and long-lived on purpose, because of iOS:
//   - background playback after the screen locks only continues for a media
//     element that is already playing, so the element must outlive track
//     changes rather than being created per song;
//   - only one element really plays at a time, so a pool of them would fight;
//   - a Web Audio graph gets suspended on lock, which is why none of the
//     decoded-PCM analysis the desktop app does can live here.
// Swapping .src on one element is the only shape that survives all three.
(function (MP) {
  'use strict';

  class AudioEngine {
    constructor(el) {
      this.el = el;
      // Callbacks, assigned by app.js. One consumer, so plain slots rather
      // than an event emitter.
      this.onStateChange = null;
      this.onEnded = null;
      this.onTime = null;
      this.onError = null;

      this._source = null;
      this._url = null;
      // Bumped on every load so a slow url resolution from a track the user
      // has already skipped past cannot overwrite the current one.
      this._generation = 0;

      // 'playing' as well as 'play': after a track change, 'play' can arrive
      // before the new file has buffered, and 'playing' is the one that
      // follows once it actually starts.
      el.addEventListener('play', () => this._state());
      el.addEventListener('playing', () => this._state());
      el.addEventListener('pause', () => this._state());
      el.addEventListener('ended', () => this.onEnded && this.onEnded());
      el.addEventListener('timeupdate', () => this.onTime && this.onTime());
      el.addEventListener('loadedmetadata', () => this.onTime && this.onTime());
      el.addEventListener('error', () => {
        // A codec the browser will not decode, or a file that vanished. Report
        // it; app.js decides whether to skip on.
        if (this.onError) this.onError(el.error);
      });
    }

    setSource(source) {
      this._source = source;
    }

    /**
     * Whether playback is wanted, not whether sound is coming out yet. A
     * freshly swapped src sits at readyState 0 while it buffers, and counting
     * that as paused left the screen dimmed after every skip.
     */
    get isPlaying() {
      return !this.el.paused && !this.el.ended;
    }

    get position() {
      return this.el.currentTime || 0;
    }

    get duration() {
      return Number.isFinite(this.el.duration) ? this.el.duration : 0;
    }

    /**
     * Point the element at a track and (optionally) start it.
     * Resolves false if a newer load superseded this one.
     */
    async load(track, { autoplay = true } = {}) {
      if (!track || !this._source) return false;
      const generation = ++this._generation;

      let url;
      try {
        url = await this._source.urlFor(track);
      } catch (error) {
        if (this.onError) this.onError(error);
        return false;
      }
      if (generation !== this._generation) {
        // Superseded while we were resolving — drop what we just acquired.
        this._source.releaseUrl(url);
        return false;
      }

      const previous = this._url;
      this._url = url;
      this.el.src = url;
      if (previous) this._source.releaseUrl(previous);

      if (autoplay) await this.play();
      return true;
    }

    async play() {
      try {
        await this.el.play();
      } catch (error) {
        // Rejects when iOS has not yet seen a user gesture, which is a normal
        // state to be in rather than a failure. app.js shows the prompt.
        if (this.onError) this.onError(error, { blocked: true });
      }
    }

    pause() {
      this.el.pause();
    }

    toggle() {
      if (this.isPlaying) this.pause();
      else this.play();
    }

    seekTo(seconds) {
      if (!Number.isFinite(this.duration) || this.duration <= 0) return;
      this.el.currentTime = Math.max(0, Math.min(this.duration, seconds));
    }

    seekRatio(ratio) {
      this.seekTo(this.duration * Math.max(0, Math.min(1, ratio)));
    }

    _state() {
      if (this.onStateChange) this.onStateChange(this.isPlaying);
    }
  }

  MP.AudioEngine = AudioEngine;
})((window.MP = window.MP || {}));
