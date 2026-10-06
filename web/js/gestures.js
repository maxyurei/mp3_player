// Tap anywhere to play/pause, swipe left/right to skip.
//
// Pointer events rather than touch events, so the same code serves a finger on
// the phone and a mouse on the desktop. The thresholds below are what separate
// a tap from a swipe: a finger never lands perfectly still, so an exact-zero
// movement test would make tapping feel broken.
(function (MP) {
  'use strict';

  const SWIPE_MIN_PX = 50; // shorter than this is a twitch, not a swipe
  const SWIPE_RATIO = 1.5; // must be this much more horizontal than vertical
  const TAP_MAX_PX = 12; // allowed finger drift during a tap
  const TAP_MAX_MS = 600; // longer than this is a hold, not a tap

  /**
   * @param {Element} el
   * @param {{onTap?:Function, onSwipeLeft?:Function, onSwipeRight?:Function,
   *          ignore?:string}} options
   */
  function attachGestures(el, options) {
    const opts = options || {};
    let start = null;

    el.addEventListener('pointerdown', (event) => {
      // Let controls be controls: a tap on the progress bar seeks, it does not
      // also toggle playback.
      if (opts.ignore && event.target.closest(opts.ignore)) {
        start = null;
        return;
      }
      start = { x: event.clientX, y: event.clientY, t: Date.now() };
    });

    el.addEventListener('pointercancel', () => {
      start = null;
    });

    el.addEventListener('pointerup', (event) => {
      if (!start) return;
      const dx = event.clientX - start.x;
      const dy = event.clientY - start.y;
      const dt = Date.now() - start.t;
      start = null;

      if (
        Math.abs(dx) >= SWIPE_MIN_PX &&
        Math.abs(dx) > Math.abs(dy) * SWIPE_RATIO
      ) {
        if (dx < 0) opts.onSwipeLeft && opts.onSwipeLeft();
        else opts.onSwipeRight && opts.onSwipeRight();
        return;
      }

      if (Math.abs(dx) < TAP_MAX_PX && Math.abs(dy) < TAP_MAX_PX && dt < TAP_MAX_MS) {
        opts.onTap && opts.onTap();
      }
    });
  }

  MP.attachGestures = attachGestures;
})((window.MP = window.MP || {}));
