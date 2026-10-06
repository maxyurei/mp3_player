// True shuffle: a bag of tracks dealt one at a time, never repeating until
// every track has been dealt once.
//
// This is deliberately not the "pick a random track, avoid the last N" model.
// That model repeats songs long before the library is exhausted as soon as the
// library is bigger than its memory window. A permutation cannot: the cycle is
// the library.
(function (MP) {
  'use strict';

  // Fisher-Yates, so every permutation is equally likely.
  function shuffledIndices(n) {
    const order = Array.from({ length: n }, (_, i) => i);
    for (let i = order.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [order[i], order[j]] = [order[j], order[i]];
    }
    return order;
  }

  class Bag {
    constructor(size = 0) {
      this.reset(size);
    }

    reset(size) {
      this._size = Math.max(0, size | 0);
      this._order = this._size > 0 ? shuffledIndices(this._size) : [];
      this._cursor = -1;
    }

    get size() {
      return this._size;
    }

    /** Index the bag is sitting on, or -1 before the first draw. */
    get current() {
      return this._cursor >= 0 ? this._order[this._cursor] : -1;
    }

    /** Draws left in this pass, for the status line. */
    get remaining() {
      return Math.max(0, this._size - 1 - this._cursor);
    }

    draw() {
      if (this._size === 0) return -1;
      if (this._cursor + 1 >= this._order.length) this._refill();
      else this._cursor += 1;
      return this._order[this._cursor];
    }

    // Refilling is where "every song once" would otherwise still let a song
    // play twice in a row: the last track of one pass can be the first of the
    // next. Swap it away so the seam between passes sounds like shuffle too.
    _refill() {
      const justPlayed = this.current;
      this._order = shuffledIndices(this._size);
      if (this._size > 1 && this._order[0] === justPlayed) {
        const j = 1 + Math.floor(Math.random() * (this._size - 1));
        [this._order[0], this._order[j]] = [this._order[j], this._order[0]];
      }
      this._cursor = 0;
    }

    /**
     * Treat `index` as already dealt in this pass, with the bag sitting on it.
     * Used when shuffle is switched on mid-song, so the song already playing
     * is not dealt a second time before the pass is through.
     */
    seed(index) {
      if (this._size === 0) return;
      const at = this._order.indexOf(index);
      if (at < 0) return;
      [this._order[0], this._order[at]] = [this._order[at], this._order[0]];
      this._cursor = 0;
    }
  }

  MP.shuffledIndices = shuffledIndices;
  MP.Bag = Bag;
})((window.MP = window.MP || {}));
