// Progress bar and position reconciliation.
//
// `Duration` arrives over SSE (stream.js); the position is advanced locally and
// re-seeded from the server's `playback` row every 3s - the one allowed client
// poll - and from `position` SSE events, which fire on seeks.

import { source } from './core.js';
import { state } from './state.js';
import { playBtn, syncMediaSessionPosition } from './playback.js';

const progressBar = document.getElementById('progress');
const progressFill = document.getElementById('progress-fill');
const progressTip = document.getElementById('progress-tip');

function formatMs(ms) {
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(totalSec / 60);
    const s = String(totalSec % 60).padStart(2, '0');
    return `${m}:${s}`;
}

function updateProgressTip() {
    const label = state.durationMs
        ? `${formatMs(state.positionMs)} / ${formatMs(state.durationMs)}`
        : '';
    progressTip.textContent = label;
}

function updateProgress() {
    if (!state.durationMs || !state.playerctlAvailable || !state.positionSynced) {
        progressBar.hidden = true;
        return;
    }
    progressBar.hidden = false;
    const now = Date.now();
    if (state.progressActive && playBtn.classList.contains('playing')) {
        state.positionMs += now - state.lastTickTs;
    }
    state.lastTickTs = now;
    if (state.positionMs >= state.durationMs) {
        state.positionMs = state.durationMs;
        state.progressActive = false;
    }
    const ratio = Math.min(1, state.positionMs / state.durationMs);
    progressFill.style.width = (ratio * 100).toFixed(2) + '%';
    updateProgressTip();
    syncMediaSessionPosition();
}

// The stored position: opening the page mid-track shows the real position
// instead of a false restart from 0.
export async function fetchPosition() {
    try {
        const res = await fetch('/api/player/position');
        if (!res.ok) return;
        const data = await res.json();
        if (typeof data.Position !== 'number') return;
        state.positionMs = data.Position;
        state.lastTickTs = Date.now();
        state.progressActive = true;
        state.positionSynced = true;
    } catch {
        // ignore; SSE position events remain the live source
    }
}

setInterval(updateProgress, 1000);

// Server-confirmed position (`playback` row): the truth the local counter is
// re-seeded from after a seek / on track change.
source.addEventListener('position', (event) => {
    let data;
    try {
        data = JSON.parse(event.data);
    } catch {
        return;
    }
    if (typeof data.Position !== 'number') {
        return;
    }
    state.positionMs = data.Position;
    state.lastTickTs = Date.now();
    state.progressActive = true;
    state.positionSynced = true;
});
