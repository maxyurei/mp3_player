// Offline shell cache.
//
// Bump VERSION whenever a file in SHELL changes. That is the trigger for the
// whole update: a byte-different sw.js makes the browser install a new worker,
// which refetches the shell and drops the old cache.
const VERSION = 'v3';
const CACHE = 'player-shell-' + VERSION;

const SHELL = [
  './',
  './index.html',
  './manifest.json',
  './css/app.css',
  './js/platform.js',
  './js/config.js',
  './js/shuffle.js',
  './js/queue.js',
  './js/metadata.js',
  './js/source-local.js',
  './js/source-remote.js',
  './js/audio.js',
  './js/media-session.js',
  './js/gestures.js',
  './js/ui.js',
  './js/pwa.js',
  './js/app.js',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './icons/apple-touch-icon-180.png',
  './icons/favicon-32.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      // cache: 'reload' so install goes to the network rather than being
      // served the stale files out of the browser's own HTTP cache — which
      // would make a version bump cache the exact files it meant to replace.
      .then((cache) =>
        cache.addAll(SHELL.map((url) => new Request(url, { cache: 'reload' })))
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key.startsWith('player-shell-') && key !== CACHE)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  let url;
  try {
    url = new URL(request.url);
  } catch (error) {
    return;
  }

  // blob: (local files) and data: are not ours to cache or serve.
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;
  // Cross-origin, which from the R2 step onwards means the audio itself.
  // Streaming and its auth belong to the network, not to this cache.
  if (url.origin !== self.location.origin) return;
  // A range request is a seek. Serving a 200 from cache in answer to one
  // breaks scrubbing, so these always go to the network untouched.
  if (request.headers.has('range')) return;

  // Navigation: network first, so a deploy is picked up as soon as it is
  // reachable, with the cache as the offline fallback.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put('./index.html', copy));
          return response;
        })
        .catch(() =>
          caches
            .match('./index.html')
            .then((cached) => cached || caches.match('./'))
        )
    );
    return;
  }

  // Shell assets: serve from cache immediately, refresh in the background.
  event.respondWith(
    caches.match(request).then((cached) => {
      const network = fetch(request)
        .then((response) => {
          if (response && response.ok) {
            const copy = response.clone();
            caches.open(CACHE).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});
