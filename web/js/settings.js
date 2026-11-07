// Quick settings modal: behaviour toggles (AUTOSKIP / LISTENBRAINZ_SUBMIT) and
// the theme picker. Every change is persisted server-side to `.env` - the
// ground truth - and applied to the running config.

import { postInit } from './core.js';
import { applyTheme, getCurrentTheme } from './themes.js';
import { closeSleepMenu } from './sleep.js';

const settingsBtn = document.getElementById('settings-btn');
const settingsPopup = document.getElementById('settings-popup');
const settingsClose = document.getElementById('settings-close');
const settingsThemeSelect = document.getElementById('settings-theme');
const settingsSwitches = [...settingsPopup.querySelectorAll('.switch input[data-setting]')];
let settingsThemes = [];
let currentSettings = null;

function renderSettings() {
    for (const input of settingsSwitches) {
        input.checked = Boolean(currentSettings && currentSettings[input.dataset.setting]);
    }
    // Submission is a no-op without a token, so that switch is disabled
    // (and its row dimmed) until LISTENBRAINZ_TOKEN is configured.
    const tokenReady = Boolean(currentSettings && currentSettings.LISTENBRAINZ_TOKEN);
    const submission = settingsSwitches.find(
        (input) => input.dataset.setting === 'LISTENBRAINZ_SUBMIT'
    );
    if (submission) {
        submission.disabled = !tokenReady;
        const row = submission.closest('.settings-row');
        if (row) {
            row.classList.toggle('disabled', !tokenReady);
            row.title = tokenReady
                ? ''
                : 'Set LISTENBRAINZ_TOKEN in .env to enable.';
        }
    }
}

function renderThemeOptions() {
    const current = (currentSettings && currentSettings.THEME) || getCurrentTheme();
    const names = [...new Set([current, ...settingsThemes])];
    settingsThemeSelect.replaceChildren();
    for (const name of names) {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        settingsThemeSelect.appendChild(option);
    }
    settingsThemeSelect.value = names.includes(current) ? current : '';
}

async function loadSettings() {
    try {
        const res = await fetch('/api/settings');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        currentSettings = data.Settings || null;
        settingsThemes = Array.isArray(data.Themes) ? data.Themes : [];
        renderSettings();
        renderThemeOptions();
    } catch (err) {
        console.error('Failed to load settings:', err);
        const message = err instanceof Error ? err.message : '';
        renderThemeError(/^HTTP \d+$/.test(message) ? message : 'network error');
    }
}

// A missing/failing `/api/settings` (typically a web server older than
// this page) must not leave an unexplained blank dropdown.
function renderThemeError(detail) {
    settingsThemeSelect.replaceChildren();
    const option = document.createElement('option');
    option.value = '';
    option.textContent = `Couldn't load themes (${detail})`;
    option.disabled = true;
    settingsThemeSelect.appendChild(option);
    settingsThemeSelect.value = '';
}

async function setSetting(key, value) {
    try {
        const res = await fetch('/api/settings', postInit({
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [key]: value }),
        }));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        currentSettings = data.Settings || currentSettings;
        renderSettings();
        return true;
    } catch (err) {
        console.error(`Failed to set ${key}:`, err);
        return false;
    }
}

async function setTheme(name) {
    try {
        const res = await fetch('/api/settings', postInit({
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ THEME: name }),
        }));
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        currentSettings = data.Settings || currentSettings;
        // Hot-swap the sheet client-side; the server keeps the theme in .env.
        const meta = document.querySelector('meta[name="nowplaying-theme"]');
        if (meta) meta.content = name;
        applyTheme(name);
        renderThemeOptions();
    } catch (err) {
        console.error('Failed to set theme:', err);
        // Snap the selector back to the last known-good theme.
        renderThemeOptions();
    }
}

function openSettings() {
    closeSleepMenu();
    settingsPopup.hidden = false;
    document.body.classList.add('no-scroll');
    settingsBtn.setAttribute('aria-expanded', 'true');
    void loadSettings();
}

function closeSettings() {
    settingsPopup.hidden = true;
    document.body.classList.remove('no-scroll');
    settingsBtn.setAttribute('aria-expanded', 'false');
}

settingsBtn.addEventListener('click', () => {
    if (settingsPopup.hidden) openSettings();
    else closeSettings();
});
settingsClose.addEventListener('click', closeSettings);
settingsPopup.addEventListener('click', (e) => {
    if (e.target === settingsPopup) closeSettings();
});
settingsThemeSelect.addEventListener('change', (e) => {
    void setTheme(e.target.value);
});
for (const input of settingsSwitches) {
    input.addEventListener('change', () => {
        void setSetting(input.dataset.setting, input.checked).then((ok) => {
            // A rejected change snaps the native checkbox back to server state.
            if (!ok) renderSettings();
        });
    });
}

// One Escape handler for the two non-modal panels: the settings popup wins over
// the sleep menu (dialogs.js closes its own modal popup separately).
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!settingsPopup.hidden) closeSettings();
    else closeSleepMenu();
});
