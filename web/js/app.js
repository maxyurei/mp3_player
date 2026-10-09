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
    const sheet = new MP.Sheet(document);

    // The queue holds one playlist at a time; the library holds them all.
    let library = new MP.Library([]);
    let playlistId = MP.Library.ALL;

    // Swapped at startup for the bucket when one is configured. Everything
    // below this line is written against the source interface, not against
    // either implementation.
    let source = MP.LocalSource;
    audio.setSource(source);

    function useSource(next) {
      source = next;
      audio.setSource(next);
    }

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
      const name = playlistId === MP.Library.ALL ? '' : library.get(playlistId).name + ' · ';
      ui.setStatus(name + songs + ' · ' + mode);
    }

    async function playIndex(index, options) {
      if (index < 0) return;
      const track = queue.tracks[index];
      if (!track) return;
      const autoplay = !options || options.autoplay !== false;
      ui.setTrack(track);
      sheet.setCurrent(track.id, audio.isPlaying);
      ui.setProgress(0, 0);
      MP.MediaSession.setTrack(track);
      updateStatus();
      await audio.load(track, { autoplay: autoplay });
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

    /** Point the queue at a playlist. History starts over with it. */
    function usePlaylist(id) {
      if (id === playlistId && queue.length) return;
      playlistId = id;
      queue.setTracks(library.get(id).tracks);
    }

    /** Picked from the list: play that song, then carry on in its playlist. */
    function playFromList(id, track) {
      usePlaylist(id);
      playIndex(queue.jumpTo(queue.tracks.indexOf(track)));
    }

    function shufflePlaylist(id) {
      // Always a fresh pass, even for the playlist already playing: that is
      // what pressing Shuffle means.
      playlistId = null;
      usePlaylist(id);
      queue.setShuffle(true);
      playIndex(queue.next());
    }

    sheet.onPick = playFromList;
    sheet.onShuffle = shufflePlaylist;

    function openSheet(focusSearch) {
      if (!library.tracks.length) return;
      sheet.open(playlistId, { focusSearch: focusSearch });
    }

    function setLibrary(tracks) {
      library = new MP.Library(tracks);
      playlistId = MP.Library.ALL;
      queue.setTracks(library.tracks);
      sheet.setLibrary(library, playlistId);
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
      const current = queue.current;
      sheet.setCurrent(current ? current.id : null, isPlaying);
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
      onSwipeUp: () => openSheet(false),
      ignore: '.progress, .picker, input, button, a',
    });

    ui.status.addEventListener('click', () => openSheet(false));

    ui.progress.addEventListener('pointerdown', (event) => {
      const rect = ui.progress.getBoundingClientRect();
      if (rect.width <= 0) return;
      audio.seekRatio((event.clientX - rect.left) / rect.width);
    });

    document.addEventListener('keydown', (event) => {
      if (sheet.isOpen) {
        if (event.key === 'Escape') {
          event.preventDefault();
          sheet.close();
        }
        // Everything else belongs to the search box and the list while the
        // sheet is up; space should not pause behind it.
        return;
      }
      if (event.key === '/' && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        openSheet(true);
        return;
      }

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
      useSource(MP.LocalSource);
      const tracks = MP.LocalSource.fromFiles(fileList);
      if (!tracks.length) {
        ui.setHint('Nothing playable in that selection.');
        return;
      }
      setLibrary(tracks);
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

    function offerPicker(message) {
      ui.showPicker();
      ui.setHint(message || '');
    }

    /** Load the bucket if one is configured; fall back to the picker if not. */
    async function openLibrary() {
      const base = MP.config.libraryBase;
      if (!base) {
        // No bucket yet — the picker is the whole app, as in earlier steps.
        if (MP.platform.iOS && !MP.platform.standalone && MP.platform.secure) {
          offerPicker('Tip: Share → Add to Home Screen for lock-screen controls');
        }
        return;
      }

      ui.setHint('Loading library…');
      try {
        const remote = MP.RemoteSource(base);
        const tracks = await remote.load();
        if (!tracks.length) throw new Error('the library is empty');
        useSource(remote);
        setLibrary(tracks);
        ui.showLibrary();
        // Queued but not started: autoplay with no user gesture is blocked on
        // iOS, and starting unbidden on launch is wrong on the desktop too.
        await playIndex(queue.next(), { autoplay: false });
        ui.setHint('Tap to play');
      } catch (error) {
        offerPicker(
          'Could not load the library (' +
            (error && error.message ? error.message : 'unknown error') +
            '). You can still pick files from this device.'
        );
      }
    }

    updateStatus();
    openLibrary();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})((window.MP = window.MP || {}));
