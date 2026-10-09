// Where tracks come from: a bucket, described by a tracks.json manifest.
//
// Same shape as LocalSource, so nothing above this layer changes:
//   urlFor(track) -> Promise<string>
//   releaseUrl(url)
// There is no scan step here. A browser cannot list a bucket or read ID3 tags
// without downloading whole files, so the manifest is built once on the
// laptop by tools/sync-library.py and uploaded alongside the audio.
(function (MP) {
  'use strict';

  // Each path segment encoded separately: encodeURIComponent would eat the
  // slashes, encodeURI would leave '#' and '?' in a filename unescaped and
  // truncate the URL there.
  function encodeKey(key) {
    return String(key)
      .split('/')
      .map(encodeURIComponent)
      .join('/');
  }

  function RemoteSource(base) {
    const root = String(base || '').replace(/\/+$/, '');

    return {
      name: 'library',
      base: root,

      /** Fetch and validate the manifest. Returns a track list. */
      async load() {
        // no-store because the whole point of re-opening the app after a sync
        // is to see songs added minutes ago; an edge-cached manifest would
        // hide them.
        const response = await fetch(root + '/tracks.json', {
          cache: 'no-store',
          credentials: 'omit',
        });
        if (!response.ok) {
          throw new Error('manifest HTTP ' + response.status);
        }
        const data = await response.json();
        const raw = Array.isArray(data) ? data : data && data.tracks;
        if (!Array.isArray(raw)) {
          throw new Error('manifest has no tracks array');
        }

        return raw
          .filter((entry) => entry && entry.key)
          .map((entry, index) => ({
            id: entry.id || entry.key || String(index),
            key: entry.key,
            title: entry.title || MP.parseName(entry.key.split('/').pop()).title,
            artist: entry.artist || MP.UNKNOWN_ARTIST,
            album: entry.album || '',
            folder: MP.folderOf(entry.key),
            duration: Number(entry.duration) || 0,
            cover: entry.cover ? root + '/' + encodeKey(entry.cover) : '',
          }));
      },

      urlFor(track) {
        return Promise.resolve(root + '/' + encodeKey(track.key));
      },

      releaseUrl() {
        // Nothing to release: these are plain URLs, not object URLs. The
        // browser's own media cache handles the bytes.
      },
    };
  }

  MP.RemoteSource = RemoteSource;
})((window.MP = window.MP || {}));
