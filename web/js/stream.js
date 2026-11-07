// The live track stream: renders the SSE payload into the card (artist /
// album / title rows, album art, feedback, OS media session) and handles
// playback status events. Track data is server-pushed; nothing here polls.

window.addEventListener('DOMContentLoaded', () => {
    const trackEl = document.getElementById('track');
    const artEl = document.getElementById('art');
    const controls = document.querySelector('.controls');
    const progress = document.getElementById('progress');

    // Hide controls and progress initially until we know playerctl availability
    if (controls) {
        controls.hidden = true;
    }
    if (progress) {
        progress.hidden = true;
    }

    function add_span(el) {
        const span = document.createElement('span')
        span.className = 'centered';
        span.textContent = 'No metadata available';
        el.append(span);
    }

    source.onmessage = (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch {
            data = null;
        }
        if (!data || data.error) {
            lastRenderKey = null;
            lastTrackData = null;
            similarActive = false;
            similarProbedId = null;
            similarCheckSeq += 1; // drop an in-flight probe
            if (data && data.error) {
                trackEl.innerHTML = data.error;
            } else {
                add_span(trackEl);
            }
            artEl.src = 'cover.svg';
            renderFeedback(null);
            clearMediaSession();
            return;
        }
        // A MusicBrainz recording id makes the track's ListenBrainz
        // similar-tracks lookup possible (see openSimilar below); what
        // it returns decides whether the title icon shows at all.
        currentMbid = typeof data.MBID === 'string' && data.MBID ? data.MBID : null;
        const id = data.TrackId;
        if (id !== lastTrackId) {
            if (lastTrackId === null) {
                progressActive = false;
                // Fresh page load: SSE only replays a position event if one
                // was recently published, so pull the stored position
                // directly instead of starting at 0.
                fetchPosition();
            } else {
                progressActive = true;
                positionMs = 0;
                lastTickTs = Date.now();
            }
            positionSynced = false;
            lastTrackId = id;
            // Unknown until the probes answer: both icons need real
            // ListenBrainz rows before they show up.
            similarActive = false;
            similarCheckSeq += 1; // drop a probe started for the old track
            similarProbedId = null; // the new track gets its own probe
        }
        if (currentMbid && id && id !== similarProbedId) {
            similarProbedId = id;
            void checkSimilar(id);
        }
        lastTrackData = data;
        durationMs = Number(data.Duration) || 0;
        renderFeedback(data);
        updateMediaSession(data);
        const art = data['Album Art'] || 'cover.svg';
        if (!artEl.src.endsWith(art)) {
            artEl.src = art;
        }
        // Album art changed (or absent): re-apply an adaptive theme so
        // its palette follows the cover shown for the current track —
        // the server serves the neutral fallback for a no-art track.
        const artVersion = data['Album Art']?.split('?v=')[1] || null;
        if (ADAPTIVE_THEMES.has(currentTheme) && artVersion !== lastArtVersion) {
            lastArtVersion = artVersion;
            applyTheme(currentTheme, artVersion);
        }
        // Rebuild the track rows only when the rendered content changes;
        // SSE re-emits the current track on every reconnect.
        const renderKey = JSON.stringify([
            data.Artist, data.Album, data.Title,
            data.Loved, data.Hated, data.Review, data.Biography, data.MBID,
        ]);
        if (renderKey === lastRenderKey) {
            playerctlAvailable = data.PlayerctlAvailable;
            return;
        }
        lastRenderKey = renderKey;
        renderRows(data);
        playerctlAvailable = data.PlayerctlAvailable;
    };

    // Render the Artist / Album / Title rows for `data`. The ListenBrainz
    // icon only appears once a probe has confirmed there are similar
    // recordings to show: the Labs datasets have no row for many tracks
    // (a click would then open an empty dialog).
    function renderRows(data) {
        const similarTrack = similarActive;
        const rows = [
            {
                icon: data.Biography ? 'icon-info' : 'icon-user',
                text: data.Artist,
                biography: data.Biography,
            },
            ...(data.Album && data.Album !== data.Title
                ? [{
                    icon: data.Review ? 'icon-info' : 'icon-disc',
                    text: data.Album,
                    review: data.Review,
                }]
                : []),
            {
                icon: similarTrack ? 'icon-listenbrainz' : 'icon-note',
                text: data.Title,
                popup: similarTrack ? 'recordings' : null,
            },
        ];
        trackEl.innerHTML = '';
        rows.forEach(({ icon, text, review, biography, popup }) => {
            const row = document.createElement('div');
            row.className = 'track-row';
            const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
            svg.classList.add('track-icon');
            if (review || biography) svg.classList.add('review');
            const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
            use.setAttribute('href', `#${icon}`);
            svg.appendChild(use);
            const value = document.createElement('span');
            value.textContent = text;
            row.append(svg, value);
            if (review || biography) {
                const content = biography || review;
                row.classList.add('has-review');
                row.title = biography
                    ? 'Read the artist biography'
                    : 'Read the album review';
                row.addEventListener('click', (e) => {
                    e.stopPropagation();
                    openReview(
                        content,
                        biography
                            ? `${data.Artist} - Biography`
                            : `${data.Artist} - ${data.Album}`
                    );
                });
            } else if (popup === 'recordings') {
                row.classList.add('has-similar');
                row.title = 'Show similar tracks';
                row.addEventListener('click', (e) => {
                    e.stopPropagation();
                    openSimilar(data.TrackId, `Similar to ${data.Artist} - ${data.Title}`);
                });
            }
            trackEl.appendChild(row);
        });
        renderFeedback(data);
    }

    // One ListenBrainz probe per track change (not a poll): `probe=1`
    // checks the Labs datasets only and skips the same-song rescue, so
    // an obscure track costs four dataset lookups instead of a
    // MusicBrainz search plus up to 48 more. It also warms the server
    // cache, so opening the dialog only has to run the rescue (when the
    // probe found nothing). `similarCheckSeq` drops answers for a
    // replaced track.
    async function checkSimilar(trackId) {
        const seq = ++similarCheckSeq;
        let tracks;
        try {
            const res = await fetch(`/api/track/${trackId}/similar?limit=1&probe=1`);
            if (!res.ok) return; // no MBID / API down: stay plain
            const body = await res.json();
            tracks = body && Array.isArray(body.Tracks) ? body.Tracks : [];
        } catch {
            return; // offline: leave the plain note icon
        }
        if (seq !== similarCheckSeq || !tracks.length) return;
        similarActive = true;
        renderCurrentRows(trackId);
    }

    // Re-render the current track's rows after a probe answered.
    function renderCurrentRows(trackId) {
        if (lastTrackData && lastTrackData.TrackId === trackId) {
            renderRows(lastTrackData);
        }
    }

    source.addEventListener('status', (event) => {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch {
            return;
        }
        const active = typeof data.Status === 'string';
        playBtn.classList.toggle('playing', data.Status === 'Playing');
        playerctlAvailable = active;
        controls.hidden = !active;
        setMediaSessionState(
            data.Status === 'Playing' ? 'playing' : active ? 'paused' : 'none'
        );
    });

    artEl.onerror = () => {
        if (artEl.src !== new URL('cover.svg', location.href).href) {
            artEl.src = 'cover.svg';
        }
    };

    source.onerror = () => {
        if (!trackEl.innerText || trackEl.innerText === 'Loading...') {
            trackEl.innerText = ''
            add_span(trackEl);
        }
    };
});
