// Pre-paint theme bootstrap, loaded as a blocking <script> in <head>:
// applies the server-injected THEME (else the adaptive palette matching
// the OS scheme) before the first paint and keeps meta[theme-color] in
// sync with the active palette. `syncThemeColor()` is called by the
// runtime theme switcher (js/themes.js) as well.

// One step away in the blue channel: an imperceptible colour change that
// Chromium still treats as a change (it compares the parsed colour).
function nudgeColor(color) {
    const match = /^#([0-9a-f]{6})$/i.exec(color);
    if (!match) return null;
    const nudged = (parseInt(match[1], 16) ^ 1).toString(16).padStart(6, '0');
    return `#${nudged}`;
}
function syncThemeColor(force = false) {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    const style = getComputedStyle(document.documentElement);
    const background = (
        style.getPropertyValue('--background-color') ||
        style.getPropertyValue('--background')
    ).trim();
    if (!background) return;
    if (force === true && meta.content === background) {
        const nudged = nudgeColor(background);
        if (!nudged) return;
        meta.content = nudged;
        setTimeout(() => syncThemeColor(), 16);
        return;
    }
    meta.content = background;
}
(function () {
    // Apply the theme before first paint: the server-injected `THEME`
    // when configured, else the adaptive palette matching the OS scheme.
    const meta = document.querySelector('meta[name="nowplaying-theme"]');
    const configured = (meta && meta.content) || '';
    const name = configured || (
        window.matchMedia('(prefers-color-scheme: light)').matches
            ? 'adaptive-light'
            : 'adaptive-dark'
    );
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = `themes/${name}.css`;
    link.id = 'theme-override';
    link.onload = () => syncThemeColor();
    document.head.appendChild(link);
})();
// Final catch-up once every stylesheet (style.css included) has applied.
window.addEventListener('load', () => syncThemeColor());
// Re-apply the caption colour whenever the window comes back: Chromium
// leaves the inactive colour behind after a focus change (see above).
window.addEventListener('focus', () => syncThemeColor(true));
window.addEventListener('pageshow', () => syncThemeColor(true));
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) syncThemeColor(true);
});

// The dashboard itself is an ES module graph (js/app.js), deferred and
// module-scoped, so it cannot see this file's scope. Expose the one function it
// needs (the runtime switcher in js/themes.js) as the single intentional global.
window.syncThemeColor = syncThemeColor;
