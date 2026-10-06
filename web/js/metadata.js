// Title and artist out of a filename.
//
// Step 1 reads names only, no ID3 — tag parsing means pulling bytes out of
// every file at scan time, and the R2 manifest in a later step will carry
// real tags anyway. So this stays a best-effort guess with no dependencies.
(function (MP) {
  'use strict';

  const EXTENSION = /\.[a-z0-9]{1,5}$/i;
  // Downloads from the desktop app land as "Title [videoid].mp3"; the id is a
  // cache key, not part of the name.
  const VIDEO_ID_SUFFIX = /\s*\[[A-Za-z0-9_-]{11}\]$/;
  // "01 - ", "01. ", "1 ", "07_" — a leading track number, which would
  // otherwise be mistaken for the artist by the " - " split below.
  const TRACK_NUMBER_PREFIX = /^\s*\d{1,3}\s*[-._)]\s*/;
  const UNKNOWN_ARTIST = 'Unknown artist';

  function clean(text) {
    return text.replace(/\s+/g, ' ').trim();
  }

  function parseName(filename) {
    let stem = filename.replace(EXTENSION, '');
    stem = stem.replace(VIDEO_ID_SUFFIX, '');
    // Underscores-for-spaces only when the name has no real spaces, so a
    // deliberate underscore in an otherwise normal title survives.
    if (!stem.includes(' ')) stem = stem.replace(/_/g, ' ');
    stem = stem.replace(TRACK_NUMBER_PREFIX, '');
    stem = clean(stem);

    const split = stem.indexOf(' - ');
    if (split > 0) {
      const artist = clean(stem.slice(0, split));
      const title = clean(stem.slice(split + 3));
      if (artist && title) return { title, artist };
    }
    return { title: stem || filename, artist: UNKNOWN_ARTIST };
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const total = Math.floor(seconds);
    const minutes = Math.floor(total / 60);
    const secs = total % 60;
    return minutes + ':' + String(secs).padStart(2, '0');
  }

  MP.parseName = parseName;
  MP.formatTime = formatTime;
  MP.UNKNOWN_ARTIST = UNKNOWN_ARTIST;
})((window.MP = window.MP || {}));
