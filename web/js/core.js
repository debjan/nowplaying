// App plumbing: the trusted-POST helper (every state-changing request
// carries the token injected into <meta>), the SSE EventSource shared by
// every module, and the PWA service-worker registration.

if ('serviceWorker' in navigator) {
    let hadController = Boolean(navigator.serviceWorker.controller);
    navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (!hadController) {
            hadController = true; // first install claimed this page
            return;
        }
        window.location.reload();
    });
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js').catch((err) => {
            console.error('Service worker registration failed:', err);
        });
    });
}
const source = new EventSource('/api/track/stream');
const NOWPLAYING_TOKEN =
    document.querySelector('meta[name="nowplaying-token"]')?.content || '';

function postInit(options = {}) {
    const init = { method: 'POST', ...options };
    if (NOWPLAYING_TOKEN) {
        init.headers = {
            ...(options.headers || {}),
            'X-Nowplaying-Token': NOWPLAYING_TOKEN,
        };
    }
    return init;
}
