// Artist-biography / album-review popup and the ListenBrainz similar-tracks
// dialog. Review prose is rendered from a minimal Markdown subset; the
// similar table is built with DOM APIs so titles cannot inject markup.

function renderMarkdown(text) {
    const esc = (s) =>
        s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const inline = (s) =>
        s
            .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_m, label, url) => {
                let u;
                try {
                    u = new URL(url, location.href);
                } catch {
                    return label;
                }
                if (u.protocol !== 'http:' && u.protocol !== 'https:') return label;
                return `<a href="${u.href}" target="_blank" rel="noreferrer noopener">${label}</a>`;
            })
            .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
            .replace(/\*([^*]+)\*/g, '<em>$1</em>')
            .replace(/`([^`]+)`/g, '<code>$1</code>');
    const out = [];
    let inList = false;
    let inQuote = false;
    const flush = () => {
        if (inList) { out.push('</ul>'); inList = false; }
        if (inQuote) { out.push('</blockquote>'); inQuote = false; }
    };
    for (const raw of text.split('\n')) {
        const line = raw.trim();
        if (!line) { flush(); continue; }
        const h1 = line.match(/^###\s+(.*)$/);
        const h2 = line.match(/^##\s+(.*)$/);
        const h3 = line.match(/^#\s+(.*)$/);
        if (h1 || h2 || h3) {
            flush();
            const tag = h1 ? 'h3' : h2 ? 'h4' : 'h5';
            out.push(`<${tag}>${inline(esc((h1 || h2 || h3)[1]))}</${tag}>`);
            continue;
        }
        if (line.startsWith('- ')) {
            if (!inList) { out.push('<ul>'); inList = true; }
            out.push(`<li>${inline(esc(line.slice(2)))}</li>`);
            continue;
        }
        if (line.startsWith('> ')) {
            if (!inQuote) { out.push('<blockquote>'); inQuote = true; }
            out.push(`<p>${inline(esc(line.slice(2)))}</p>`);
            continue;
        }
        flush();
        out.push(`<p>${inline(esc(line))}</p>`);
    }
    flush();
    return out.join('\n');
}

const popup = document.getElementById('review-popup');
const popupCard = document.querySelector('.review-popup-card');
const popupTitle = document.getElementById('review-title');
const popupBody = document.getElementById('review-body');
const popupClose = document.getElementById('review-close');

const SIMILAR_LIMIT = 12;

function resetPopupMode() {
    popupCard.classList.remove('wide');
    popupBody.classList.remove('popup-table');
}

function openReview(review, title) {
    similarSeq += 1; // drop any in-flight similar-tracks fetch
    resetPopupMode();
    popupTitle.textContent = title;
    popupBody.innerHTML = renderMarkdown(review);
    popup.hidden = false;
    document.body.classList.add('no-scroll');
}

function closeReview() {
    similarSeq += 1; // drop any in-flight similar-tracks fetch
    popup.hidden = true;
    popupBody.innerHTML = '';
    resetPopupMode();
    document.body.classList.remove('no-scroll');
}

function statusLine(text) {
    const p = document.createElement('p');
    p.className = 'popup-status';
    p.textContent = text;
    return p;
}

// Build a table row by row (never from a rendered string) so titles and
// albums can't inject markup.

const MBID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function listenbrainzUrl(mbid, kind) {
    return typeof mbid === 'string' && MBID_RE.test(mbid)
        ? `https://listenbrainz.org/${kind}/${mbid}/`
        : null;
}

function buildTable(headers, className) {
    const table = document.createElement('table');
    table.className = className ? `similar-table ${className}` : 'similar-table';
    const row = table.createTHead().insertRow();
    for (const [label, cellClass] of headers) {
        const th = document.createElement('th');
        th.textContent = label;
        if (cellClass) th.className = cellClass;
        row.appendChild(th);
    }
    table.createTBody();
    return table;
}

// A row (and its name anchor) opens `url`; the anchor keeps the row
// keyboard accessible and handles the modifier/middle clicks a row can't.
// An empty label stays plain text - an anchor without text is invisible
// but still focusable.
function linkCell(cell, text, url, title) {
    if (!url || !text) {
        cell.textContent = text;
        return;
    }
    const link = document.createElement('a');
    link.href = url;
    link.target = '_blank';
    link.rel = 'noreferrer noopener';
    if (title) link.title = title;
    link.textContent = text;
    cell.appendChild(link);
}

// Similar tracks: ListenBrainz Labs' co-listening score per recording.
function buildSimilarTable(tracks) {
    const table = buildTable([
        ['', ''], ['Artist', ''], ['Title', ''],
        ['Album', ''], ['Match', 'num'],
    ]);
    const body = table.tBodies[0];
    tracks.forEach((track) => {
        const row = body.insertRow();
        const art = row.insertCell();
        if (typeof track.Cover === 'string' && track.Cover.startsWith('https://')) {
            const img = document.createElement('img');
            img.className = 'similar-cover';
            img.src = track.Cover;
            img.alt = '';
            img.loading = 'lazy';
            img.addEventListener('error', () => img.remove());
            art.appendChild(img);
        }
        const url = listenbrainzUrl(track.ArtistMBID, 'artist');
        const artistCell = row.insertCell();
        linkCell(artistCell, track.Artist || '', url);
        // The title points at the recording itself on ListenBrainz.
        const trackUrl = listenbrainzUrl(track.MBID, 'recording');
        const titleCell = row.insertCell();
        linkCell(
            titleCell,
            track.Title || '',
            trackUrl,
            'Open the recording on ListenBrainz'
        );
        row.insertCell().textContent = track.Album || '';
        const score = row.insertCell();
        score.className = 'num';
        score.textContent = typeof track.Score === 'number'
            ? track.Score.toLocaleString()
            : '';
        if (url) {
            row.classList.add('linked');
            row.title = 'Open the artist on ListenBrainz';
            row.addEventListener('click', (event) => {
                if (event.target.closest('a')) return;
                window.open(url, '_blank', 'noopener');
            });
        }
    });
    return table;
}

// Shared dialog loader: opens the wide table popup, fetches `url`, and
// renders `data[field]` with `build`. `similarSeq` invalidates a fetch
// whose popup has since been replaced.
async function loadPopupTable({ label, url, field, noun, build, empty }) {
    const seq = ++similarSeq;
    resetPopupMode();
    popupCard.classList.add('wide');
    popupBody.classList.add('popup-table');
    popupTitle.textContent = label;
    popupBody.replaceChildren(statusLine('Searching ListenBrainz…'));
    popup.hidden = false;
    document.body.classList.add('no-scroll');

    let data;
    try {
        const res = await fetch(url);
        if (seq !== similarSeq) return;
        if (!res.ok) {
            let detail = `HTTP ${res.status}`;
            try {
                const body = await res.json();
                if (body && body.detail) detail = body.detail;
            } catch {
                // non-JSON error body; keep the status text
            }
            popupBody.replaceChildren(statusLine(`Could not load ${noun}: ${detail}`));
            return;
        }
        data = await res.json();
    } catch {
        if (seq === similarSeq) {
            popupBody.replaceChildren(statusLine('Could not reach ListenBrainz.'));
        }
        return;
    }
    if (seq !== similarSeq) return;

    const rows = data && Array.isArray(data[field]) ? data[field] : [];
    popupBody.replaceChildren(rows.length ? build(rows) : statusLine(empty));
}

// Similar recordings for the current track (seeded on its MusicBrainz
// recording id; the server caches them for an hour).
function openSimilar(trackId, label) {
    if (!trackId) return;
    return loadPopupTable({
        label,
        url: `/api/track/${trackId}/similar?limit=${SIMILAR_LIMIT}`,
        field: 'Tracks',
        noun: 'similar tracks',
        build: buildSimilarTable,
        empty: 'No similar tracks found for this track yet.',
    });
}

popupClose.addEventListener('click', closeReview);
popup.addEventListener('click', (e) => {
    if (e.target === popup) closeReview();
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !popup.hidden) closeReview();
});
