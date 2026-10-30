// nowplaying service worker: precache the app shell on install, serve
// navigations network-first (falling back to the cached shell offline) and
// static assets stale-while-revalidate. Live /api/ data is never cached.

const VERSION = 'v4';
const SHELL_CACHE = `shell-${VERSION}`;

const SHELL_URLS = [
    '/',
    '/style.css',
    '/favicon',
    '/cover.svg',
    '/manifest.webmanifest',
    '/icon/192.png',
    '/icon/512.png',
];

const SHELL_FALLBACK = '/';

const SHELL_PATHS = new Set(SHELL_URLS);

self.addEventListener('install', (event) => {
    event.waitUntil(
        (async () => {
            const cache = await caches.open(SHELL_CACHE);
            await Promise.all(SHELL_URLS.map((url) => cache.add(url).catch(() => {})));
            await self.skipWaiting();
        })()
    );
});

self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches
            .keys()
            .then((keys) =>
                Promise.all(
                    keys
                        .filter((key) => key !== SHELL_CACHE)
                        .map((key) => caches.delete(key))
                )
            )
            .then(() => self.clients.claim())
    );
});

self.addEventListener('fetch', (event) => {
    const { request } = event;
    if (request.method !== 'GET') return;
    const url = new URL(request.url);
    if (url.origin !== self.location.origin) return;
    // Live data must always come from the network.
    if (url.pathname.startsWith('/api/')) return;
    if (request.mode !== 'navigate' && !SHELL_PATHS.has(url.pathname)) return;

    event.respondWith(
        (async () => {
            const cache = await caches.open(SHELL_CACHE);
            if (request.mode === 'navigate') {
                // App shell: network-first, fall back to the cached entry offline.
                try {
                    const response = await fetch(request);
                    if (response.ok) {
                        cache.put(SHELL_FALLBACK, response.clone()).catch(() => {});
                    }
                    return response;
                } catch {
                    const fallback = await cache.match(SHELL_FALLBACK);
                    if (fallback) return fallback;
                    throw new Error('App shell not cached');
                }
            }
            const cached = await cache.match(request);
            const fetchPromise = fetch(request)
                .then((response) => {
                    if (response.ok) {
                        cache.put(request, response.clone());
                    }
                    return response;
                })
                .catch(() => cached);
            if (cached) {
                // Stale-while-revalidate: serve the cache, refresh in the background.
                fetchPromise.catch(() => {});
                return cached;
            }
            return fetchPromise;
        })()
    );
});
