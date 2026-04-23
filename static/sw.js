// Weather Bot Dashboard — Service Worker
// Strategy: network-first for /api/*, cache-first for static assets.
const CACHE  = 'weatherbot-v1';
const STATIC = ['/', '/static/index.html', '/static/manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE)
      .then(c => c.addAll(STATIC))
      .catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API çağrıları: her zaman network, cache'leme (canlı veri)
  if (url.pathname.startsWith('/api/')) {
    return; // default network-only behavior
  }

  // GET dışı isteklere karışma
  if (e.request.method !== 'GET') return;

  // Statik: cache-first, yoksa network (ve cache'e ekle)
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(resp => {
        if (resp && resp.status === 200 && resp.type === 'basic') {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy)).catch(() => {});
        }
        return resp;
      }).catch(() => cached);
    })
  );
});
