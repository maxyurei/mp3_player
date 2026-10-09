// What plays next, and what played before.
//
// Two separate structures, because they answer different questions:
//   - the Bag decides what has not been heard yet this pass (forwards)
//   - the history is what actually came out of the speakers (backwards)
// Deriving "previous" from an index walk instead of real history is the bug
// the desktop app has: under shuffle it rewinds to index-1, a track you never
// heard. Here, back goes back through what you actually listened to.
//
// Navigation behaves like browser back/forward: going back then forward
// retraces history rather than drawing new tracks.
(function (MP) {
  'use strict';

  // Long enough that going back is never the limit in practice, bounded so a
  // session left running for days cannot grow without end.
  const HISTORY_MAX = 500;

  class Queue {
    constructor() {
      this._tracks = [];
      this._bag = new MP.Bag(0);
      this._history = [];
      this._pos = -1;
      this.shuffle = true;
    }

    get tracks() {
      return this._tracks;
    }

    get length() {
      return this._tracks.length;
    }

    /** Draws left before every track has had a turn. */
    get remaining() {
      return this._bag.remaining;
    }

    get currentIndex() {
      return this._pos >= 0 ? this._history[this._pos] : -1;
    }

    get current() {
      const i = this.currentIndex;
      return i >= 0 && i < this._tracks.length ? this._tracks[i] : null;
    }

    setTracks(tracks) {
      this._tracks = tracks || [];
      this._bag.reset(this._tracks.length);
      this._history = [];
      this._pos = -1;
    }

    setShuffle(on) {
      if (this.shuffle === on) return;
      this.shuffle = on;
      if (!on) return;
      // Start a fresh pass from here, counting the current song as dealt.
      this._bag.reset(this._tracks.length);
      const i = this.currentIndex;
      if (i >= 0) this._bag.seed(i);
    }

    /** Next track index, or -1 if there is nothing to play. */
    next() {
      if (!this._tracks.length) return -1;
      // Retrace if the user had gone back.
      if (this._pos >= 0 && this._pos < this._history.length - 1) {
        this._pos += 1;
        return this._history[this._pos];
      }
      const index = this.shuffle
        ? this._bag.draw()
        : (this.currentIndex + 1) % this._tracks.length;
      return this._push(index);
    }

    /** Previous track index, or -1 if there is nothing behind us. */
    previous() {
      if (!this._tracks.length) return -1;
      if (this._pos > 0) {
        this._pos -= 1;
        return this._history[this._pos];
      }
      return this.currentIndex;
    }

    get hasPrevious() {
      return this._pos > 0;
    }

    /** Jump straight to a track; forward history is discarded, as in a browser. */
    jumpTo(index) {
      if (!(index >= 0 && index < this._tracks.length)) return -1;
      if (this._pos < this._history.length - 1) {
        this._history.length = this._pos + 1;
      }
      if (this.shuffle) this._bag.take(index);
      return this._push(index);
    }

    _push(index) {
      if (index < 0) return -1;
      this._history.push(index);
      if (this._history.length > HISTORY_MAX) {
        this._history.splice(0, this._history.length - HISTORY_MAX);
      }
      this._pos = this._history.length - 1;
      return index;
    }
  }

  MP.Queue = Queue;
})((window.MP = window.MP || {}));
