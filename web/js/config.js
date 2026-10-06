// Where the library lives.
//
// Set libraryBase to the bucket's public URL (step 5) or, once the password
// Worker is in front of it (step 6), to the Worker's URL. Leave it empty and
// the app falls back to the local file picker, which is how it behaves before
// a bucket exists.
//
// No trailing slash. The app appends '/tracks.json' and the object keys.
(function (MP) {
  'use strict';

  // The R2 bucket's public development URL. Readable by anyone who has it —
  // unguessable rather than private — until the password Worker goes in front
  // of it, at which point only this line changes.
  const DEFAULT_LIBRARY_BASE =
    'https://pub-acd1ffd9c18240da8a5cb49883eca419.r2.dev';

  // A localStorage override, so a bucket can be pointed at from one device
  // without a redeploy — useful while setting up, and the obvious lever when
  // something is wrong and you want to test against a different URL.
  let override = null;
  try {
    override = localStorage.getItem('player.libraryBase');
  } catch (error) {
    // Private mode and locked-down configurations throw on access.
  }

  MP.config = {
    libraryBase: (override || DEFAULT_LIBRARY_BASE).replace(/\/+$/, ''),
  };
})((window.MP = window.MP || {}));
