// Progress bar and transport controls. `Duration` arrives over SSE; the
// position is advanced locally and re-seeded from the server playback row
// (the only client poll) every 3s and from `position` SSE events.

function formatMs(ms) {
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const m = Math.floor(totalSec / 60);
    const s = String(totalSec % 60).padStart(2, '0');
    return `${m}:${s}`;
}

function updateProgressTip() {
    const label = durationMs ? `${formatMs(positionMs)} / ${formatMs(durationMs)}` : '';
    progressTip.textContent = label;
}

function updateProgress() {
    if (!durationMs || !playerctlAvailable || !positionSynced) {
        progressBar.hidden = true;
        return;
    }
    progressBar.hidden = false;
    const now = Date.now();
    if (progressActive && playBtn.classList.contains('playing')) {
        positionMs += now - lastTickTs;
    }
    lastTickTs = now;
    if (positionMs >= durationMs) {
        positionMs = durationMs;
        progressActive = false;
    }
    progressFill.style.width = (Math.min(1, positionMs / durationMs) * 100).toFixed(2) + '%';
    updateProgressTip();
    syncMediaSessionPosition();
}

async function fetchPosition() {
    try {
        const res = await fetch('/api/player/position');
        if (!res.ok) return;
        const data = await res.json();
        if (typeof data.Position !== 'number') return;
        positionMs = data.Position;
        lastTickTs = Date.now();
        progressActive = true;
        positionSynced = true;
    } catch {
        // ignore; SSE position events remain the live source
    }
}

async function sendControl(action) {
    if (action === 'play-pause') {
        playBtn.classList.toggle('playing');
        setMediaSessionState(
            playBtn.classList.contains('playing') ? 'playing' : 'paused'
        );
    }
    if (action === 'previous') {
        positionMs = 0;
        lastTickTs = Date.now();
        progressActive = true;
    }
    try {
        await fetch(`/api/player/${action}`, postInit());
    } catch {
        // ignore; playback state is reconciled by the next status event
    }
}

document.querySelectorAll('.btn').forEach((btn) => {
    btn.addEventListener('click', () => sendControl(btn.dataset.action));
});

setInterval(updateProgress, 1000);

// Server-confirmed position (`playback` row): the truth the local
// counter is re-seeded from on track change and after a seek.
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
    positionMs = data.Position;
    lastTickTs = Date.now();
    progressActive = true;
    positionSynced = true;
});
