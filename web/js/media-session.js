// Media Session: what the lock screen, the notification shade and the headphone
// buttons talk to.
//
// This is the whole reason the app can be used without looking at it. The
// handlers registered here are also how desktop keyboard media keys arrive —
// the browser routes them to these actions, not to keydown.
(function (MP) {
  'use strict';

  const session = () =>
    typeof navigator !== 'undefined' ? navigator.mediaSession : null;

  const MediaSession = {
    get supported() {
      return !!session();
    },

    /** Wire the transport actions once, at startup. */
    bind(actions) {
      const ms = session();
      if (!ms) return;
      const map = {
        play: actions.play,
        pause: actions.pause,
        nexttrack: actions.next,
        previoustrack: actions.previous,
        stop: actions.pause,
        seekto: (details) => {
          if (details && typeof details.seekTime === 'number') {
            actions.seekTo(details.seekTime);
          }
        },
        seekforward: (d) => actions.nudge((d && d.seekOffset) || 10),
        seekbackward: (d) => actions.nudge(-((d && d.seekOffset) || 10)),
      };
      for (const [action, handler] of Object.entries(map)) {
        if (!handler) continue;
        try {
          ms.setActionHandler(action, handler);
        } catch (error) {
          // Browsers reject actions they do not implement; the rest still bind.
        }
      }
    },

    setTrack(track) {
      const ms = session();
      if (!ms || typeof window.MediaMetadata !== 'function') return;
      if (!track) {
        ms.metadata = null;
        return;
      }
      // Artwork comes from the manifest, so it is present for bucket tracks
      // and absent for locally picked files — the lock screen falls back to
      // its own placeholder when the list is empty. Sizes are declared rather
      // than measured: iOS picks one before the image has loaded.
      const artwork = track.cover
        ? [
            { src: track.cover, sizes: '512x512', type: 'image/jpeg' },
            { src: track.cover, sizes: '256x256', type: 'image/jpeg' },
          ]
        : [];

      ms.metadata = new window.MediaMetadata({
        title: track.title || '',
        artist: track.artist || '',
        album: track.album || '',
        artwork: artwork,
      });
    },

    setPlaying(isPlaying) {
      const ms = session();
      if (!ms) return;
      ms.playbackState = isPlaying ? 'playing' : 'paused';
    },

    /** Drives the lock-screen scrubber. */
    setPosition(position, duration) {
      const ms = session();
      if (!ms || typeof ms.setPositionState !== 'function') return;
      if (!Number.isFinite(duration) || duration <= 0) return;
      try {
        ms.setPositionState({
          duration: duration,
          position: Math.max(0, Math.min(duration, position || 0)),
          playbackRate: 1,
        });
      } catch (error) {
        // Thrown when position/duration disagree mid-seek. Next tick fixes it.
      }
    },
  };

  MP.MediaSession = MediaSession;
})((window.MP = window.MP || {}));
