/**
 * Service worker for the warden PWA.
 *
 * The warden's device may lose signal the moment the alarm sounds, so the shell
 * and the zone roster are cached at drill start and the app must start from
 * cache with no network at all.
 *
 * The caching strategy differs by what is being fetched, and the difference is
 * deliberate:
 *
 *   the app shell        cache first — it never changes during a drill, and a
 *                        network round trip before the first paint is time a
 *                        warden does not have
 *   API reads            network first, cache as fallback — a stale roster is
 *                        better than no roster, but a fresh one is better still
 *   API writes           never cached, never retried here — they go through the
 *                        IndexedDB queue, which owns ordering and durability
 *
 * A write must not be retried by the service worker. Background Sync would
 * replay it without a sequence number and out of order, which is exactly the
 * failure the device-assigned sequence exists to prevent.
 */

const SHELL_CACHE = 'evac120-shell-v1';
const DATA_CACHE = 'evac120-data-v1';

const SHELL = [
  '/evac/warden',
  '/static/warden.html',
  '/static/css/app.css',
  '/static/js/warden.js',
  '/static/js/queue.js',
  '/static/js/i18n.js',
  '/static/js/api.js',
  '/static/manifest.webmanifest',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(
        names
          .filter((name) => name !== SHELL_CACHE && name !== DATA_CACHE)
          .map((name) => caches.delete(name)),
      ))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  const url = new URL(request.url);

  // Writes are the queue's business. Passing them through untouched means a
  // failure surfaces to the app, which stores the action and retries in order.
  if (request.method !== 'GET') return;

  if (url.pathname.startsWith('/api/')) {
    event.respondWith(networkFirst(request));
    return;
  }

  event.respondWith(cacheFirst(request));
});

async function cacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) return cached;
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(SHELL_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    // Offline and not cached. Better an explicit failure the app can render
    // than a blank page with no explanation.
    return new Response('offline and not cached', {
      status: 503, statusText: 'offline',
    });
  }
}

async function networkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(DATA_CACHE);
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    const cached = await caches.match(request);
    if (cached) {
      // Marked so the app can tell a live answer from a remembered one and say
      // so on screen rather than implying the roster is current.
      const body = await cached.text();
      return new Response(body, {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
          'X-EVAC-From-Cache': '1',
        },
      });
    }
    return new Response(JSON.stringify({ detail: 'offline, nothing cached' }), {
      status: 503, headers: { 'Content-Type': 'application/json' },
    });
  }
}

/** Let the page tell the worker a drill has started, so it warms the cache. */
self.addEventListener('message', (event) => {
  if (event.data?.type === 'CACHE_ZONE' && event.data.url) {
    event.waitUntil(
      caches.open(DATA_CACHE).then((cache) => cache.add(event.data.url)),
    );
  }
});
