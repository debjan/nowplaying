// Love / hate thumbs over the album art. A vote is stored in the local
// `feedback` table and, with a ListenBrainz token, mirrored as feedback;
// the SSE change token carries the score so every client re-renders.

// --- Love / hate ------------------------------------------------------
// Two thumbs over the album art. A vote is stored in the local `feedback`
// table and, with a ListenBrainz token, submitted as love/hate feedback.
// The SSE change token includes the score, so every open client re-renders
// the filled variant (the local DB is the only source of truth).
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

function renderFeedback(data) {
    const active = Boolean(data && data.TrackId);
    feedbackEl.hidden = !active;
    if (!active) return;
    renderThumb(thumbUp, Boolean(data.Loved), 'up');
    renderThumb(thumbDown, Boolean(data.Hated), 'down');
}

async function sendFeedback(score) {
    if (!lastTrackData || !lastTrackData.TrackId) return;
    const trackId = lastTrackData.TrackId;
    const previous = {
        Loved: Boolean(lastTrackData.Loved),
        Hated: Boolean(lastTrackData.Hated),
    };
    // Pressing the thumb that is already filled withdraws the vote; the
    // server mirrors this, so keep the optimistic state in step.
    const withdrawing = (score === 1 && previous.Loved)
        || (score === -1 && previous.Hated);
    lastTrackData = {
        ...lastTrackData,
        Loved: withdrawing ? false : score === 1,
        Hated: withdrawing ? false : score === -1,
    };
    renderFeedback(lastTrackData);
    try {
        const res = await fetch(`/api/feedback/${score}`, postInit());
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (lastTrackData && data.TrackId === lastTrackData.TrackId) {
            lastTrackData = {
                ...lastTrackData,
                Loved: Boolean(data.Loved),
                Hated: Boolean(data.Hated),
            };
            renderFeedback(lastTrackData);
        }
    } catch (err) {
        // Refused or offline: put the previous verdict back (unless the
        // stream already moved on to another track).
        if (lastTrackData && lastTrackData.TrackId === trackId) {
            lastTrackData = { ...lastTrackData, ...previous };
            renderFeedback(lastTrackData);
        }
        console.error('Failed to submit feedback:', err);
    }
}

for (const btn of [thumbUp, thumbDown]) {
    btn.addEventListener('click', () => {
        sendFeedback(Number(btn.dataset.score));
    });
}
