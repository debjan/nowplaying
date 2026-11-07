// Sleep timer.
//
// The timer is server-owned (`POST /api/sleep/{mode}`) so it keeps counting
// while the phone is asleep and survives a reload; this side only renders the
// state pushed over SSE.

import { postInit, source } from './core.js';

const sleepBtn = document.getElementById('sleep-btn');
const sleepLabel = document.getElementById('sleep-label');
const sleepMenu = document.getElementById('sleep-menu');
const sleepOffOption = document.getElementById('sleep-off-option');
const sleepRadios = [...sleepMenu.querySelectorAll('input[name="sleep-mode"]')];
let sleepMode = null;
let sleepEndsAt = null;

export function closeSleepMenu() {
    sleepMenu.hidden = true;
    sleepBtn.setAttribute('aria-expanded', 'false');
}

function toggleSleepMenu() {
    sleepMenu.hidden = !sleepMenu.hidden;
    sleepBtn.setAttribute('aria-expanded', String(!sleepMenu.hidden));
}

function formatSleepRemaining(ms) {
    const total = Math.max(0, Math.ceil(ms / 1000));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function renderSleep() {
    const armed = Boolean(sleepMode);
    sleepBtn.classList.toggle('armed', armed);
    sleepOffOption.hidden = !armed;
    for (const input of sleepRadios) {
        input.checked = input.value === sleepMode;
    }
    if (!armed) {
        sleepLabel.textContent = '';
        sleepBtn.title = 'Sleep timer';
        return;
    }
    if (sleepMode === 'end') {
        sleepLabel.textContent = 'End';
        sleepBtn.title = 'Sleep timer: end of song';
        return;
    }
    const remaining = sleepEndsAt ? sleepEndsAt - Date.now() : 0;
    sleepLabel.textContent = formatSleepRemaining(remaining);
    sleepBtn.title = `Sleep timer: ${formatSleepRemaining(remaining)}`;
}

function applySleepState(timer) {
    sleepMode = timer && timer.Mode ? timer.Mode : null;
    sleepEndsAt = timer && typeof timer.EndsAt === 'number' ? timer.EndsAt : null;
    renderSleep();
}

async function setSleep(mode) {
    try {
        const res = await fetch(`/api/sleep/${mode}`, postInit());
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        applySleepState(await res.json());
    } catch (err) {
        console.error('Failed to set the sleep timer:', err);
    }
}

sleepBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleSleepMenu();
});

for (const input of sleepRadios) {
    input.addEventListener('click', (e) => {
        if (input.value !== sleepMode || input.value === 'off') return;
        e.preventDefault();
        closeSleepMenu();
        void setSleep('off');
    });
}

sleepMenu.addEventListener('change', (e) => {
    const input = e.target.closest('input[name="sleep-mode"]');
    if (!input) return;
    closeSleepMenu();
    void setSleep(input.value);
});

document.addEventListener('click', (e) => {
    if (!sleepBtn.closest('.picker').contains(e.target)) closeSleepMenu();
});

renderSleep();

setInterval(renderSleep, 1000);

// Server-pushed sleep timer state: the countdown is derived from `EndsAt`
// locally, so the stream only fires on arm / cancel / expiry.
source.addEventListener('sleep', (event) => {
    let data;
    try {
        data = JSON.parse(event.data);
    } catch {
        return;
    }
    applySleepState(data);
});
