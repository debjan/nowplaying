// Love / hate thumbs over the album art.
//
// A vote is stored in the local `feedback` table and, with a ListenBrainz
// token, mirrored as feedback; the SSE change token carries the score so every
// open client re-renders the filled variant (the local DB is the only source
// of truth).

import { postInit } from './core.js';
import { state } from './state.js';

const feedbackEl = document.getElementById('feedback');
const thumbUp = feedbackEl.querySelector('.feedback-btn[data-score="1"]');
const thumbDown = feedbackEl.querySelector('.feedback-btn[data-score="-1"]');

function renderThumb(btn, pressed, kind) {
    const icon = pressed ? `icon-thumbs-${kind}-filled` : `icon-thumbs-${kind}`;
    btn.querySelector('use').setAttribute('href', `#${icon}`);
    btn.classList.toggle('active', pressed);
    btn.setAttribute('aria-pressed', String(pressed));
    // The label describes what a click does next (rate vs. withdraw).
    const label = kind === 'up'
        ? (pressed ? 'Remove love' : 'Love this track')
        : (pressed ? 'Remove hate' : 'Hate this track');
    btn.title = label;
    btn.setAttribute('aria-label', label);
}

export function renderFeedback(data) {
    const active = Boolean(data && data.TrackId);
    feedbackEl.hidden = !active;
    if (!active) return;
    renderThumb(thumbUp, Boolean(data.Loved), 'up');
    renderThumb(thumbDown, Boolean(data.Hated), 'down');
}

async function sendFeedback(score) {
    const current = state.lastTrackData;
    if (!current || !current.TrackId) return;
    const trackId = current.TrackId;
    const previous = {
        Loved: Boolean(current.Loved),
        Hated: Boolean(current.Hated),
    };
    // Pressing the thumb that is already filled withdraws the vote; the
    // server mirrors this, so keep the optimistic state in step.
    const withdrawing = (score === 1 && previous.Loved)
        || (score === -1 && previous.Hated);
    state.lastTrackData = {
        ...current,
        Loved: withdrawing ? false : score === 1,
        Hated: withdrawing ? false : score === -1,
    };
    renderFeedback(state.lastTrackData);
    try {
        const res = await fetch(`/api/feedback/${score}`, postInit());
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        const latest = state.lastTrackData;
        if (latest && data.TrackId === latest.TrackId) {
            state.lastTrackData = {
                ...latest,
                Loved: Boolean(data.Loved),
                Hated: Boolean(data.Hated),
            };
            renderFeedback(state.lastTrackData);
        }
    } catch (err) {
        // Refused or offline: put the previous verdict back (unless the
        // stream already moved on to another track).
        const latest = state.lastTrackData;
        if (latest && latest.TrackId === trackId) {
            state.lastTrackData = { ...latest, ...previous };
            renderFeedback(state.lastTrackData);
        }
        console.error('Failed to submit feedback:', err);
    }
}

for (const btn of [thumbUp, thumbDown]) {
    btn.addEventListener('click', () => {
        sendFeedback(Number(btn.dataset.score));
    });
}
