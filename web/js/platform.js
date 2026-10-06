// What this browser can actually do. Checked once, read everywhere.
(function (MP) {
  'use strict';

  const ua = navigator.userAgent || '';

  // iPadOS 13+ deliberately reports itself as a Mac, so the user-agent alone
  // is not enough — a "Mac" with a touchscreen is an iPad.
  const iOS =
    /iPad|iPhone|iPod/.test(ua) ||
    (/Macintosh/.test(ua) && (navigator.maxTouchPoints || 0) > 1);

  // Launched from the home screen rather than inside Safari. iOS uses the
  // legacy navigator.standalone; everything else uses the display-mode query.
  const standalone =
    window.navigator.standalone === true ||
    (window.matchMedia &&
      window.matchMedia('(display-mode: standalone)').matches);

  // Folder picking cannot be feature-detected: iOS Safari exposes the
  // webkitdirectory attribute but ignores it, offering individual files
  // anyway. So the platform check has to come first, and the property check
  // only rules out older desktop browsers.
  const folderPicking =
    !iOS && 'webkitdirectory' in document.createElement('input');

  // Service workers — and therefore installing as a PWA — need HTTPS. Opening
  // index.html straight off disk is fine, it just has no offline support.
  const secure =
    location.protocol === 'https:' ||
    location.hostname === 'localhost' ||
    location.hostname === '127.0.0.1';

  MP.platform = { iOS, standalone, folderPicking, secure };
})((window.MP = window.MP || {}));
