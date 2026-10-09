// The view. Reads nothing, decides nothing — app.js pushes state in.
(function (MP) {
  'use strict';

  class UI {
    constructor(root) {
      const find = (id) => root.getElementById(id);
      this.stage = find('stage');
      this.empty = find('empty');
      this.now = find('now');
      this.title = find('title');
      this.artist = find('artist');
      this.progress = find('progress');
      this.progressFill = find('progressFill');
      this.elapsed = find('elapsed');
      this.remaining = find('remaining');
      this.status = find('status');
      this.statusText = find('statusText');
      this.hint = find('hint');
      this.emptyCopy = find('emptyCopy');
      this.picker = find('picker');
      this.pickerFiles = find('pickerFiles');
      this.pickFolder = find('pickFolder');
      this.pickFiles = find('pickFiles');
      this._flashTimer = 0;
    }

    /**
     * iOS cannot pick folders, so there it gets the file picker as the only
     * option and copy that matches. Elsewhere folders lead and picking loose
     * songs stays available as the quieter second choice.
     */
    configurePickers(supportsFolders) {
      this.pickFolder.hidden = !supportsFolders;
      this.pickFiles.classList.toggle('picker-alt', supportsFolders);
      this.pickFiles.querySelector('span').textContent = supportsFolders
        ? 'Pick songs instead'
        : 'Choose songs';
      this.emptyCopy.textContent = supportsFolders
        ? 'Point it at a folder of music. Nothing is uploaded — the files play straight off this device.'
        : 'Pick songs from the Files app. Nothing is uploaded — they play straight off this device.';
    }

    showLibrary() {
      this.empty.hidden = true;
      this.now.hidden = false;
    }

    showPicker() {
      this.empty.hidden = false;
      this.now.hidden = true;
    }

    setTrack(track) {
      this.title.textContent = track ? track.title : '';
      this.artist.textContent = track ? track.artist : '';
      // The tab/standalone title doubles as the app-switcher label on iOS.
      document.title = track ? track.title + ' — ' + track.artist : 'Player';
    }

    setPlaying(isPlaying) {
      this.stage.classList.toggle('is-paused', !isPlaying);
    }

    setProgress(position, duration) {
      const ratio = duration > 0 ? Math.min(1, position / duration) : 0;
      this.progressFill.style.transform = 'scaleX(' + ratio + ')';
      this.elapsed.textContent = MP.formatTime(position);
      // Counting down rather than showing total length: the useful question
      // mid-track is how much is left.
      this.remaining.textContent =
        duration > 0 ? '-' + MP.formatTime(duration - position) : '0:00';
    }

    setStatus(text) {
      this.statusText.textContent = text;
      // No library, nothing to open.
      this.status.hidden = !text;
    }

    setHint(text) {
      this.hint.textContent = text || '';
    }

    /** Acknowledge a skip, so a swipe is felt without looking at the screen. */
    flash(direction) {
      const className = direction === 'back' ? 'flash-back' : 'flash-next';
      this.stage.classList.remove('flash-back', 'flash-next');
      // Force a reflow so re-adding the class restarts the animation even when
      // the same direction is swiped twice quickly.
      void this.stage.offsetWidth;
      this.stage.classList.add(className);
      clearTimeout(this._flashTimer);
      this._flashTimer = setTimeout(
        () => this.stage.classList.remove(className),
        220
      );
    }
  }

  MP.UI = UI;
})((window.MP = window.MP || {}));
