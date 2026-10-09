// Playlists and search over the loaded track list.
//
// A playlist is a top-level folder, nothing more: Music/Gym/x.mp3 is in "Gym".
// Organising happens in the file manager, and the same rule reads both a
// bucket key (audio/Gym/x.mp3) and a picked folder's relative path
// (Music/Gym/x.mp3), because both lead with one segment that is not the
// folder — the bucket prefix or the folder that was picked.
(function (MP) {
  'use strict';

  const ALL = 'all';

  /** Top-level folder of a path, or '' for a song sitting at the root. */
  function folderOf(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    return parts.length >= 3 ? parts[1] : '';
  }

  // Case- and accent-insensitive, so "beyonce" finds "Beyoncé".
  function fold(text) {
    return String(text || '')
      .normalize('NFD')
      .replace(/[̀-ͯ]/g, '')
      .toLowerCase();
  }

  class Library {
    constructor(tracks) {
      this.tracks = tracks || [];
      for (const track of this.tracks) {
        track._search = fold(track.title + ' ' + track.artist + ' ' + (track.album || ''));
      }

      const byFolder = new Map();
      for (const track of this.tracks) {
        if (!track.folder) continue;
        if (!byFolder.has(track.folder)) byFolder.set(track.folder, []);
        byFolder.get(track.folder).push(track);
      }
      const folders = Array.from(byFolder.keys()).sort((a, b) =>
        a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
      );

      this.playlists = [{ id: ALL, name: 'All songs', tracks: this.tracks }].concat(
        folders.map((name) => ({ id: 'folder:' + name, name: name, tracks: byFolder.get(name) }))
      );
    }

    get(id) {
      return this.playlists.find((p) => p.id === id) || this.playlists[0];
    }

    /** Every word has to appear somewhere in title, artist or album. */
    search(tracks, query) {
      const words = fold(query).split(/\s+/).filter(Boolean);
      if (!words.length) return tracks;
      return tracks.filter((track) => words.every((w) => track._search.includes(w)));
    }
  }

  Library.ALL = ALL;
  MP.Library = Library;
  MP.folderOf = folderOf;
})((window.MP = window.MP || {}));
