// Wiring. Every decision about what plays next lives in queue.js; everything
// about how it plays lives in audio.js. This file only connects them to the
// screen, the gestures and the OS.
(function (MP) {
  'use strict';

  // Past this point into a track, "previous" restarts it instead of going
  // back — what every other player does, and what the thumb expects.
  const PREV_RESTART_SECONDS = 3;
  // Give up skipping after this many unplayable files in a row, rather than
  // racing through a whole folder of them.
  const MAX_CONSECUTIVE_ERRORS = 5;
  const POSITION_PUSH_MS = 1000;

  function start() {
    const ui = new MP.UI(document);
    const audio = new MP.AudioEngine(document.getElementById('audio'));
    const queue = new MP.Queue();
    const source = MP.LocalSource;

    audio.setSource(source);

    let consecutiveErrors = 0;
    let lastPositionPush = 0;

    function updateStatus() {
      if (!queue.length) {
        ui.setStatus('');
        return;
      }
      const songs = queue.length === 1 ? '1 song' : queue.length + ' songs';
      const mode = queue.shuffle
        ? 'shuffle · ' + queue.remaining + ' left this pass'
        : 'in order';
      ui.setStatus(songs + ' · ' + mode);
    }

    async function playIndex(index) {
      if (index < 0) return;
      const track = queue.tracks[index];
      if (!track) return;
      ui.setTrack(track);
      ui.setProgress(0, 0);
      MP.MediaSession.setTrack(track);
      updateStatus();
      await audio.load(track);
    }

    function next() {
      if (!queue.length) return;
      ui.flash('next');
      playIndex(queue.next());
    }

    function previous() {
      if (!queue.length) return;
      ui.flash('back');
      // Deep into a track, or nothing behind us: restart rather than rewind.
      if (audio.position > PREV_RESTART_SECONDS || !queue.hasPrevious) {
        audio.seekTo(0);
        return;
      }
      playIndex(queue.previous());
    }

    function toggle() {
      if (!queue.length) return;
      if (queue.currentIndex < 0) {
        playIndex(queue.next());
        return;
      }
      audio.toggle();
    }

    function toggleShuffle() {
      queue.setShuffle(!queue.shuffle);
      updateStatus();
      ui.setHint(queue.shuffle ? 'Shuffle on' : 'Playing in order');
    }

    // --- playback events ---

    audio.onStateChange = (isPlaying) => {
      ui.setPlaying(isPlaying);
      MP.MediaSession.setPlaying(isPlaying);
      if (isPlaying) {
        consecutiveErrors = 0;
        ui.setHint('');
      }
    };

    audio.onTime = () => {
      ui.setProgress(audio.position, audio.duration);
      const now = Date.now();
      if (now - lastPositionPush > POSITION_PUSH_MS) {
        lastPositionPush = now;
        MP.MediaSession.setPosition(audio.position, audio.duration);
      }
    };

    audio.onEnded = () => {
      playIndex(queue.next());
    };

    audio.onError = (error, info) => {
      if (info && info.blocked) {
        // iOS has not seen a gesture yet. Not a failure — just waiting.
        ui.setHint('Tap anywhere to play');
        return;
      }
      consecutiveErrors += 1;
      if (consecutiveErrors > MAX_CONSECUTIVE_ERRORS || queue.length <= 1) {
        ui.setHint('Could not play that file.');
        return;
      }
      ui.setHint('Skipped a file this browser cannot play');
      playIndex(queue.next());
    };

    // --- input ---

    MP.attachGestures(ui.stage, {
      onTap: toggle,
      onSwipeLeft: next,
      onSwipeRight: previous,
      ignore: '.progress, .picker, input, button, a',
    });

    ui.progress.addEventListener('pointerdown', (event) => {
      const rect = ui.progress.getBoundingClientRect();
      if (rect.width <= 0) return;
      audio.seekRatio((event.clientX - rect.left) / rect.width);
    });

    document.addEventListener('keydown', (event) => {
      // Never swallow a key that belongs to a control the user is focused on.
      // event.target is not always an Element (it can be the document), so
      // closest() has to be checked for rather than assumed.
      const target = event.target;
      if (target && typeof target.closest === 'function') {
        if (target.closest('input, button, a, [contenteditable]')) return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;

      const handlers = {
        ' ': toggle,
        k: toggle,
        ArrowRight: next,
        ArrowLeft: previous,
        j: previous,
        l: next,
        s: toggleShuffle,
      };
      const handler = handlers[event.key] || handlers[event.key.toLowerCase()];
      if (!handler) return;
      event.preventDefault();
      handler();
    });

    // Lock screen, notification shade, headphone buttons and desktop media
    // keys all arrive here rather than as key events.
    MP.MediaSession.bind({
      play: () => audio.play(),
      pause: () => audio.pause(),
      next: next,
      previous: previous,
      seekTo: (seconds) => audio.seekTo(seconds),
      nudge: (delta) => audio.seekTo(audio.position + delta),
    });

    // --- library ---

    function loadFiles(fileList) {
      const tracks = source.fromFiles(fileList);
      if (!tracks.length) {
        ui.setHint('Nothing playable in that selection.');
        return;
      }
      queue.setTracks(tracks);
      ui.showLibrary();
      ui.setHint('');
      // The picker change is itself a user gesture, so starting here is
      // allowed even under the iOS autoplay rules.
      playIndex(queue.next());
    }

    // Two routes to the same place: a folder on desktop, loose files on iOS.
    ui.configurePickers(MP.platform.folderPicking);
    ui.picker.addEventListener('change', (e) => loadFiles(e.target.files));
    ui.pickerFiles.addEventListener('change', (e) => loadFiles(e.target.files));

    // Lock-screen controls only appear for an installed app, so say so once,
    // in the one place it is actionable.
    if (MP.platform.iOS && !MP.platform.standalone && MP.platform.secure) {
      ui.setHint('Tip: Share → Add to Home Screen for lock-screen controls');
    }

    updateStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})((window.MP = window.MP || {}));
