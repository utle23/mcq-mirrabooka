/**
 * MCQ Mirrabooka — Service Worker
 *
 * Minimal SW that makes the PWA installable on Android Chrome. Network-first
 * for everything (the app needs fresh data); offline fallback to whatever
 * happens to be cached. No aggressive caching of HTML/JSON because we don't
 * want stale checklists.
 */
const CACHE_NAME = 'mcq-static-v1';
const STATIC_ASSETS = [
  '/static/style.css',
  '/static/logo.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(STATIC_ASSETS))
      .catch(() => {})    // tolerate missing assets
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Static assets → cache-first
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(req).then(cached => cached || fetch(req).then(resp => {
        const copy = resp.clone();
        caches.open(CACHE_NAME).then(c => c.put(req, copy)).catch(() => {});
        return resp;
      }).catch(() => cached))
    );
    return;
  }

  // Everything else (pages, JSON, uploads) → network-first, no cache.
  event.respondWith(
    fetch(req).catch(() => caches.match(req))
  );
});
