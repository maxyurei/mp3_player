// Service worker registration. Everything here is best-effort: the app works
// identically without it, just without offline launch.
(function (MP) {
  'use strict';

  if (!('serviceWorker' in navigator) || !MP.platform.secure) return;

  // Captured before registering: if a controller already exists, a later
  // controllerchange means an update replaced it and the page should reload
  // so markup and scripts match. On a first visit there is no controller, and
  // the worker claiming the page is not a reason to reload.
  const hadController = !!navigator.serviceWorker.controller;
  let reloading = false;

  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (!hadController || reloading) return;
    reloading = true;
    location.reload();
  });

  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js').catch(() => {
      // Private browsing and a few locked-down configurations refuse to
      // register. Not worth surfacing — nothing downstream depends on it.
    });
  });
})((window.MP = window.MP || {}));
