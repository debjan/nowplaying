// Playback: the transport buttons and the OS Media Session.
//
// `sendControl()` translates a dashboard click (or an OS media key, see below)
// into a command for whichever player is active; the server is stateless on
// this path, so nothing here touches the DB. The Media Session API publishes
// the track to the OS (lock screen, media keys, Bluetooth headsets, the Windows
// SMTC overlay) and routes its controls back through the same `sendControl()`.

import { postInit } from './core.js';
import { state } from './state.js';

export const playBtn = document.querySelector('.btn-play');

export async function sendControl(action) {
    if (action === 'play-pause') {
        playBtn.classList.toggle('playing');
        setMediaSessionState(
            playBtn.classList.contains('playing') ? 'playing' : 'paused'
        );
    }
    if (action === 'previous') {
        state.positionMs = 0;
        state.lastTickTs = Date.now();
        state.progressActive = true;
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

const mediaSession = 'mediaSession' in navigator ? navigator.mediaSession : null;
// Guard: the server can disable the browser Media Session API via env var.
const mediaSessionEnabled =
    mediaSession !== null &&
    document.querySelector('meta[name="nowplaying-media-session"]')?.content !== '0';
// The silent keep-alive element (see `keepSessionAlive` further down).
const sessionAudio = document.getElementById('session-audio');
// Chromium only accepts raster artwork for the session, so the SVG
// placeholder is never advertised - the PWA icon stands in instead.
const MEDIA_ART_FALLBACK = new URL('icon/512.png', location.href).href;
// Skip reassigning identical metadata: the SSE stream re-emits the
// current track on every reconnect and the OS would re-render the card.
let mediaSessionKey = null;
// Whether real playback is happening right now; gates the silent
// keep-alive stream, so the OS card exists only while music plays.
let mediaSessionWanted = false;

function absoluteUrl(url) {
    try {
        return new URL(url, location.href).href;
    } catch {
        return MEDIA_ART_FALLBACK;
    }
}

export function setMediaSessionState(playbackState) {
    if (!mediaSessionEnabled) return;
    try {
        mediaSession.playbackState = playbackState;
    } catch {
        // unsupported state value; the status event retries on change
    }
    mediaSessionWanted = playbackState === 'playing';
    if (mediaSessionWanted) {
        // Real playback started: (re)open the OS session.
        keepSessionAlive();
    } else if (!sessionAudio.paused) {
        // Nothing is playing: drop the silent stream so the OS card
        // disappears instead of lingering in a paused state.
        sessionAudio.pause();
    }
}

export function updateMediaSession(data) {
    if (!mediaSessionEnabled || !data) return;
    const key = JSON.stringify([data.Title, data.Artist, data.Album, data['Album Art']]);
    if (key === mediaSessionKey) return;
    mediaSessionKey = key;
    const artwork = data['Album Art'] ? absoluteUrl(data['Album Art']) : MEDIA_ART_FALLBACK;
    try {
        mediaSession.metadata = new MediaMetadata({
            title: data.Title || 'Unknown Title',
            artist: data.Artist || 'Unknown Artist',
            album: data.Album || '',
            artwork: [{ src: artwork, sizes: '512x512', type: 'image/png' }],
        });
    } catch {
        // MediaMetadata unavailable: leave the OS display untouched
    }
}

export function clearMediaSession() {
    if (!mediaSessionEnabled) return;
    mediaSessionKey = null;
    mediaSession.metadata = null;
    try {
        mediaSession.setPositionState(null);
    } catch {
        // already cleared
    }
    setMediaSessionState('none');
}

// Feed the OS scrubber; the browser rejects a state without a positive
// duration, so this only runs once a duration is known.
export function syncMediaSessionPosition() {
    if (!mediaSessionEnabled) return;
    try {
        mediaSession.setPositionState({
            duration: state.durationMs / 1000,
            position: Math.min(state.positionMs, state.durationMs) / 1000,
            playbackRate: 1,
        });
    } catch {
        // invalid state (no duration yet); updateProgress retries
    }
}

if (mediaSessionEnabled) {
    const actions = {
        // `play`/`pause` are the OS asking for a specific direction; the
        // player only exposes a toggle, so no-op when already there.
        play: () => {
            if (!playBtn.classList.contains('playing')) sendControl('play-pause');
        },
        pause: () => {
            if (playBtn.classList.contains('playing')) sendControl('play-pause');
        },
        previoustrack: () => sendControl('previous'),
        nexttrack: () => sendControl('next'),
    };
    for (const [action, handler] of Object.entries(actions)) {
        try {
            mediaSession.setActionHandler(action, handler);
        } catch {
            // action unsupported by this browser
        }
    }
}

// Chromium (Edge/Chrome) only hands a page's Media Session to the OS -
// the Windows media flyout, the lock screen, the SMTC overlay - while a
// media *element* is actually playing
let sessionAudioBlocked = true;

function silentWav(seconds = 10, rate = 8000) {
    const samples = seconds * rate;
    const buffer = new ArrayBuffer(44 + samples);
    const view = new DataView(buffer);
    const ascii = (offset, text) => {
        for (let i = 0; i < text.length; i += 1) {
            view.setUint8(offset + i, text.charCodeAt(i));
        }
    };
    ascii(0, 'RIFF');
    view.setUint32(4, 36 + samples, true);
    ascii(8, 'WAVE');
    ascii(12, 'fmt ');
    view.setUint32(16, 16, true); // fmt chunk size
    view.setUint16(20, 1, true); // PCM
    view.setUint16(22, 1, true); // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate, true); // byte rate (8-bit mono)
    view.setUint16(32, 1, true); // block align
    view.setUint16(34, 8, true); // bits per sample
    ascii(36, 'data');
    view.setUint32(40, samples, true);
    // 8-bit PCM silence is 0x80 (0x00 is full negative DC, i.e. a thump).
    new Uint8Array(buffer, 44).fill(0x80);
    return new Blob([buffer], { type: 'audio/wav' });
}

function keepSessionAlive() {
    if (!mediaSession || !mediaSessionWanted || !sessionAudio.paused) return;
    sessionAudio.play().then(
        () => {
            sessionAudioBlocked = false;
        },
        () => {
            sessionAudioBlocked = true;
        }
    );
}

sessionAudio.src = URL.createObjectURL(silentWav());
for (const type of ['pointerdown', 'keydown', 'touchstart']) {
    document.addEventListener(
        type,
        () => {
            if (sessionAudioBlocked) keepSessionAlive();
        },
        { passive: true }
    );
}
window.addEventListener('focus', keepSessionAlive);
document.addEventListener('visibilitychange', () => {
    if (!document.hidden) keepSessionAlive();
});

if (!mediaSessionEnabled) {
    sessionAudio.remove();
}
