// Runtime theme switcher: swaps the theme sheet for the settings picker and
// follows the OS light/dark scheme while no fixed theme is configured.
// The pre-paint part lives in js/theme-boot.js.

// `theme-boot.js` is a classic blocking script (a module would be deferred and
// could paint with the wrong palette first), so it hands this one function to
// the module graph through the global object.
const syncThemeColor = window.syncThemeColor ?? (() => {});

const themeMeta = document.querySelector('meta[name="nowplaying-theme"]');
const configuredTheme = (themeMeta && themeMeta.content) || '';
const ADAPTIVE_THEMES = new Set(['adaptive-dark', 'adaptive-light']);
const osLight = window.matchMedia('(prefers-color-scheme: light)');
let currentTheme = configuredTheme
    || (osLight.matches ? 'adaptive-light' : 'adaptive-dark');
// Cover version (`?v=`) of the last adaptive palette render; the theme only
// re-derives when the current track's cover changes.
let lastArtVersion = null;

export function getCurrentTheme() {
    return currentTheme;
}

export function applyTheme(name, version = lastArtVersion) {
    let link = document.getElementById('theme-override');
    if (!link) {
        link = document.createElement('link');
        link.rel = 'stylesheet';
        link.id = 'theme-override';
        document.head.appendChild(link);
    }
    const href = ADAPTIVE_THEMES.has(name) && version
        ? `themes/${name}.css?v=${version}`
        : `themes/${name}.css`;
    const url = new URL(href, location.href);
    // Re-sync the browser chrome color once the new sheet applies.
    link.onload = () => syncThemeColor();
    if (link.href !== url.href) {
        link.href = href;
    } else {
        syncThemeColor();
    }
    currentTheme = name;
}

// Re-derive an adaptive palette from the album art shown for the track on
// screen (the server serves the neutral fallback for a no-art track).
export function applyCoverTheme(version) {
    if (!ADAPTIVE_THEMES.has(currentTheme) || version === lastArtVersion) return;
    lastArtVersion = version;
    applyTheme(currentTheme, version);
}

// Follow OS light/dark only when no fixed theme is configured.
if (!configuredTheme) {
    osLight.addEventListener('change', (e) => {
        applyTheme(e.matches ? 'adaptive-light' : 'adaptive-dark');
    });
}
