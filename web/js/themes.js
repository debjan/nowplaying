// Runtime theme switcher: swaps the theme sheet for the settings picker
// and follows the OS light/dark scheme while no fixed theme is configured.
// The pre-paint part lives in js/theme-boot.js.

// --- Theme ------------------------------------------------------------
// Top level: the SSE handler (above, inside DOMContentLoaded) and the
// settings modal (below) share the same theme state and switcher.
const themeMeta = document.querySelector('meta[name="nowplaying-theme"]');
const configuredTheme = (themeMeta && themeMeta.content) || '';
const ADAPTIVE_THEMES = new Set(['adaptive-dark', 'adaptive-light']);
const osLight = window.matchMedia('(prefers-color-scheme: light)');
let currentTheme = configuredTheme
    || (osLight.matches ? 'adaptive-light' : 'adaptive-dark');
let lastArtVersion = null;

function applyTheme(name, version) {
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

// Follow OS light/dark only when no fixed theme is configured.
if (!configuredTheme) {
    osLight.addEventListener('change', (e) => {
        const name = e.matches ? 'adaptive-light' : 'adaptive-dark';
        applyTheme(name, lastArtVersion);
    });
}
