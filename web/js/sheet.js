// The library sheet: search, playlists and the song list, sliding up over the
// player. Like ui.js it decides nothing — it reports what was picked and
// app.js does the playing.
(function (MP) {
  'use strict';

  // How far the sheet has to be dragged down, or how fast, to dismiss it.
  const DISMISS_PX = 90;
  const DISMISS_VELOCITY = 0.5; // px per ms

  class Sheet {
    constructor(root) {
      const find = (id) => root.getElementById(id);
      this.el = find('sheet');
      this.panel = find('sheetPanel');
      this.scrim = find('sheetScrim');
      this.head = find('sheetHead');
      this.search = find('search');
      this.chips = find('playlists');
      this.count = find('listCount');
      this.shuffleButton = find('shufflePlay');
      this.list = find('songs');
      this.emptyNote = find('songsEmpty');

      // Callbacks, assigned by app.js.
      this.onPick = null; // (playlistId, track)
      this.onShuffle = null; // (playlistId)

      this._library = null;
      this._playlistId = MP.Library.ALL;
      this._shown = [];
      this._currentId = null;
      this._playing = false;
      this._hideTimer = 0;

      this.scrim.addEventListener('click', () => this.close());
      this.search.addEventListener('input', () => this._renderSongs());
      this.search.addEventListener('keydown', (event) => {
        // Enter plays the top hit, so "/", a few letters, Enter is a whole
        // request without touching the mouse.
        if (event.key === 'Enter' && this._shown.length) {
          event.preventDefault();
          this._pick(this._shown[0]);
        }
      });

      this.chips.addEventListener('click', (event) => {
        const chip = event.target.closest('[data-playlist]');
        if (!chip) return;
        this._playlistId = chip.dataset.playlist;
        this._renderChips();
        this._renderSongs();
        this.list.scrollTop = 0;
      });

      this.list.addEventListener('click', (event) => {
        const row = event.target.closest('[data-index]');
        if (row) this._pick(this._shown[Number(row.dataset.index)]);
      });

      this.shuffleButton.addEventListener('click', () => {
        if (this.onShuffle) this.onShuffle(this._playlistId);
        this.close();
      });

      this._attachDrag();
    }

    get isOpen() {
      return this.el.classList.contains('is-open');
    }

    setLibrary(library, playlistId) {
      this._library = library;
      this._playlistId = playlistId;
      this.search.value = '';
      if (this.isOpen) this._render();
    }

    /** Mark the playing song in the list, without rebuilding it. */
    setCurrent(trackId, isPlaying) {
      this._currentId = trackId;
      this._playing = isPlaying;
      if (!this.isOpen) return;
      for (const row of this.list.children) {
        const track = this._shown[Number(row.dataset.index)];
        const current = !!track && track.id === trackId;
        row.classList.toggle('is-current', current);
        row.classList.toggle('is-playing', current && isPlaying);
      }
    }

    open(playlistId, { focusSearch = false } = {}) {
      if (!this._library) return;
      if (playlistId) this._playlistId = playlistId;
      clearTimeout(this._hideTimer);
      this._render();
      this.el.hidden = false;
      // Next frame, so the closed position is painted first and the slide
      // actually animates instead of appearing in place.
      requestAnimationFrame(() => {
        this.el.classList.add('is-open');
        this._revealCurrent();
        if (focusSearch) this.search.focus({ preventScroll: true });
      });
    }

    close() {
      if (!this.isOpen) return;
      this.el.classList.remove('is-open');
      this.panel.style.transform = '';
      this.search.blur();
      clearTimeout(this._hideTimer);
      // Hidden once the slide has finished, so it stays out of the tab order
      // and the accessibility tree while closed.
      this._hideTimer = setTimeout(() => {
        if (!this.isOpen) this.el.hidden = true;
      }, 360);
    }

    _pick(track) {
      if (!track) return;
      if (this.onPick) this.onPick(this._playlistId, track);
      this.search.value = '';
      this.close();
    }

    _render() {
      this._renderChips();
      this._renderSongs();
    }

    _renderChips() {
      const playlists = this._library.playlists;
      // One playlist is just "All songs": a lone chip would be a control that
      // does nothing, so the row disappears until there is a folder.
      this.chips.hidden = playlists.length < 2;
      this.chips.textContent = '';
      for (const playlist of playlists) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'chip';
        chip.dataset.playlist = playlist.id;
        chip.textContent = playlist.id === MP.Library.ALL ? 'All' : playlist.name;
        if (playlist.id === this._playlistId) chip.classList.add('is-active');
        this.chips.appendChild(chip);
      }
      const active = this.chips.querySelector('.is-active');
      if (active) active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    }

    _renderSongs() {
      const playlist = this._library.get(this._playlistId);
      const query = this.search.value.trim();
      this._shown = this._library.search(playlist.tracks, query);

      const n = this._shown.length;
      this.count.textContent = query
        ? n + (n === 1 ? ' match' : ' matches')
        : n + (n === 1 ? ' song' : ' songs');
      this.shuffleButton.hidden = !!query || !playlist.tracks.length;
      this.emptyNote.hidden = n > 0;
      this.emptyNote.textContent = query ? 'No songs match “' + query + '”' : 'No songs here yet';

      const fragment = document.createDocumentFragment();
      this._shown.forEach((track, index) => {
        const row = document.createElement('li');
        row.className = 'song';
        row.dataset.index = index;
        if (track.id === this._currentId) {
          row.classList.add('is-current');
          if (this._playing) row.classList.add('is-playing');
        }

        const text = document.createElement('div');
        text.className = 'song-text';
        const title = document.createElement('span');
        title.className = 'song-title';
        title.textContent = track.title;
        const artist = document.createElement('span');
        artist.className = 'song-artist';
        artist.textContent = track.artist;
        text.append(title, artist);
        row.appendChild(text);

        const time = document.createElement('span');
        time.className = 'song-time';
        // Bars while this song plays, its length otherwise.
        time.innerHTML = '<i class="bars" aria-hidden="true"><b></b><b></b><b></b></i>';
        const length = document.createElement('span');
        length.className = 'song-length';
        length.textContent = track.duration ? MP.formatTime(track.duration) : '';
        time.appendChild(length);
        row.appendChild(time);

        fragment.appendChild(row);
      });
      this.list.textContent = '';
      this.list.appendChild(fragment);
    }

    _revealCurrent() {
      const row = this.list.querySelector('.is-current');
      if (row && !this.search.value) row.scrollIntoView({ block: 'center' });
    }

    /** Drag the header down to dismiss, following the finger the whole way. */
    _attachDrag() {
      let start = null;

      this.head.addEventListener('pointerdown', (event) => {
        if (event.target.closest('input, button')) return;
        start = { y: event.clientY, t: Date.now() };
        this.head.setPointerCapture(event.pointerId);
        this.panel.classList.add('is-dragging');
      });

      this.head.addEventListener('pointermove', (event) => {
        if (!start) return;
        const dy = Math.max(0, event.clientY - start.y);
        this.panel.style.transform = 'translateY(' + dy + 'px)';
      });

      const end = (event) => {
        if (!start) return;
        const dy = event.clientY - start.y;
        const velocity = dy / Math.max(1, Date.now() - start.t);
        start = null;
        this.panel.classList.remove('is-dragging');
        if (dy > DISMISS_PX || velocity > DISMISS_VELOCITY) {
          this.close();
        } else {
          this.panel.style.transform = '';
        }
      };
      this.head.addEventListener('pointerup', end);
      this.head.addEventListener('pointercancel', end);
    }
  }

  MP.Sheet = Sheet;
})((window.MP = window.MP || {}));
