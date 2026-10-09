// Where tracks come from: local files chosen with the folder picker.
//
// This is the seam the R2 step swaps out. A source is:
//   name                       label for the status line
//   urlFor(track)  -> Promise<string>   what to feed <audio>.src
//   releaseUrl(url)                     let go of a url we are done with
// Nothing above this layer knows whether a track is a file on disk or an
// object in a bucket, so a remote source is a drop-in replacement.
(function (MP) {
  'use strict';

  // What iOS Safari and desktop browsers will actually decode. flac/opus are
  // uneven across browsers but harmless to offer — the browser rejects what it
  // cannot play and we skip to the next track.
  const AUDIO_EXTENSIONS = /\.(mp3|m4a|aac|mp4|wav|flac|ogg|oga|opus|webm)$/i;

  const LocalSource = {
    name: 'this device',

    /** FileList (from the picker) -> sorted track list. */
    fromFiles(fileList) {
      const files = Array.from(fileList || []).filter((file) =>
        AUDIO_EXTENSIONS.test(file.name)
      );
      // Sort on the full relative path so album folders stay together and the
      // non-shuffled order is predictable.
      files.sort((a, b) =>
        pathOf(a).localeCompare(pathOf(b), undefined, { numeric: true })
      );
      return files.map((file, index) => {
        const parsed = MP.parseName(file.name);
        return {
          id: pathOf(file) || file.name + ':' + index,
          title: parsed.title,
          artist: parsed.artist,
          folder: MP.folderOf(file.webkitRelativePath),
          file: file,
        };
      });
    },

    urlFor(track) {
      // Made fresh per play and revoked after, rather than one url per track
      // held for the session: a thousand live object urls pin a thousand file
      // handles for no benefit. The File itself is the durable handle.
      return Promise.resolve(URL.createObjectURL(track.file));
    },

    releaseUrl(url) {
      if (url) URL.revokeObjectURL(url);
    },
  };

  function pathOf(file) {
    return file.webkitRelativePath || file.name || '';
  }

  MP.LocalSource = LocalSource;
})((window.MP = window.MP || {}));
