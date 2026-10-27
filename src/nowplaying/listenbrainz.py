"""ListenBrainz similarity data: similar recordings and similar artists.

**Track similarity** comes from ListenBrainz Labs' precomputed
``similar-recordings`` datasets (session based: recordings played in the same
listening sessions), which take the seed and an algorithm and answer with a
``score`` per recording:

    POST https://labs.api.listenbrainz.org/similar-recordings/json
         [{"recording_mbids": ["<uuid>"], "algorithm": "<algorithm>"}]

Coverage is patchy and effectively random per seed, so `SIMILAR_SOURCES` lists
several datasets/algorithms that are tried in order until one of them knows the
seed (`similar_recordings`). Unlike the core API these endpoints tolerate
unmapped seeds - an unknown recording answers with an empty list, not an error.

**Artist similarity** has no equivalent dataset used here; the seed recording is
resolved to its artist MBID and LB Radio's artist pool (that artist plus its
most similar ones) is shown, one sample recording per artist:

1. ``POST /1/metadata/recording``     recording MBID -> artist MBID
2. ``GET  /1/lb-radio/artist/<mbid>`` recordings from the artist + similar ones
3. ``POST /1/metadata/recording``     sample MBIDs -> title

The core API is rate limited to one request per second and requires a
descriptive User-Agent (https://listenbrainz.readthedocs.io/en/latest/users/api/index.html),
so calls are serialized process-wide and results are cached per seed.
"""

import asyncio
import re
import time
from collections import OrderedDict
from typing import Any

import httpx

from nowplaying import config

API_ROOT = 'https://api.listenbrainz.org'
LABS_ROOT = 'https://labs.api.listenbrainz.org'
MUSICBRAINZ_ROOT = 'https://musicbrainz.org'
SIMILAR_RECORDINGS_PATH = '/similar-recordings/json'
MLHD_SIMILAR_RECORDINGS_PATH = '/mlhd-similar-recordings/json'

USER_AGENT = config.LISTENBRAINZ_USER_AGENT

MIN_INTERVAL = 1.1  # docs: never more than ONE call per second
LABS_MIN_INTERVAL = 0.3  # the Labs dataset hoster is not rate limited
METADATA_BATCH = 50  # recordings per POST /1/metadata/recording call
MODES = ('easy', 'medium', 'hard')

# Precomputed similarity sets from ListenBrainz Labs, as `(dataset, algorithm)`
# in the order they are tried (see `similar_recordings`).
SIMILAR_SOURCES = (
    (
        SIMILAR_RECORDINGS_PATH,
        'session_based_listens_session_300_contribution_5_threshold_15_limit_50_skip_30',
    ),
    (
        SIMILAR_RECORDINGS_PATH,
        'session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30',
    ),
    (
        SIMILAR_RECORDINGS_PATH,
        'session_based_days_7500_session_300_contribution_5_threshold_15_limit_50_skip_30_top_n_listeners_1000',
    ),
    (
        MLHD_SIMILAR_RECORDINGS_PATH,
        'session_based_mlhd_session_300_contribution_5_threshold_15_limit_50_skip_30',
    ),
)
SIMILAR_ALGORITHMS = tuple(algorithm for _, algorithm in SIMILAR_SOURCES)
_SIMILAR_DATASET = {algorithm: path for path, algorithm in SIMILAR_SOURCES}

CACHE_TTL = 3600.0  # seconds a seed's recommendations stay warm
CACHE_MAX = 128  # seeds kept in memory
MAX_PER_ALBUM = 2  # similar-track rows kept per album
RESCUE_CANDIDATES = 12
MUSICBRAINZ_RETRIES = 3

_MBID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)


class ListenBrainzError(RuntimeError):
    """Raised when ListenBrainz can't answer (HTTP error, unmapped MBID)."""


class _Limiter:
    """Serialize calls so every client shares the 1 request/second budget."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            delay = self._min_interval - (time.monotonic() - self._last)
            if delay > 0:
                await asyncio.sleep(delay)
            self._last = time.monotonic()


_limiter = _Limiter(MIN_INTERVAL)
_labs_limiter = _Limiter(LABS_MIN_INTERVAL)

# (recording mbid, algorithm) -> rows from the Labs similar-recordings dataset.
_similar_cache: OrderedDict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = (
    OrderedDict()
)
# recording mbid -> rows borrowed from another recording of the same song.
_rescue_cache: OrderedDict[tuple[str], tuple[float, list[dict[str, Any]]]] = (
    OrderedDict()
)
# (artist mbid, mode, max_similar_artists, max_recordings_per_artist) -> artists.
_artist_cache: OrderedDict[
    tuple[str, str, int, int], tuple[float, list[dict[str, Any]]]
] = OrderedDict()

# recording mbid -> (expiry, metadata); a seed is resolved once per dialog/call
_metadata_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    """The shared client; recreated after `aclose()` (app shutdown, tests)."""

    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=API_ROOT,
            headers={'User-Agent': USER_AGENT, 'Accept': 'application/json'},
            timeout=20.0,
            # the API 308-redirects paths that miss the trailing slash
            follow_redirects=True,
        )
    return _client


async def aclose() -> None:
    """Close the shared client; called from the web app's lifespan."""

    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _request(method: str, path: str, *, labs: bool = False, **kwargs: Any) -> Any:
    """One throttled API call; returns parsed JSON ({} for 204).

    `labs=True` uses the dataset hoster's own (much looser) budget, so the
    algorithm fallback chain does not eat the core API's 1 request/second.
    """

    await (_labs_limiter if labs else _limiter).wait()
    try:
        response = await _http().request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise ListenBrainzError(f'{method} {path} failed: {exc}') from exc

    # Stay inside the server's window instead of collecting 429s.
    if response.headers.get('X-RateLimit-Remaining') == '0':
        reset = response.headers.get('X-RateLimit-Reset-In', '')
        if reset.replace('.', '', 1).isdigit():
            await asyncio.sleep(float(reset) + 0.1)

    if response.status_code == 204:
        return {}
    if response.status_code >= 400:
        raise ListenBrainzError(
            f'{method} {path} -> HTTP {response.status_code}: '
            f'{response.text.strip()[:300]}'
        )
    try:
        return response.json()
    except ValueError as exc:  # non-JSON body (proxy error page, empty reply)
        raise ListenBrainzError(
            f'{method} {path} -> unparsable response: {response.text.strip()[:300]}'
        ) from exc


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


def _cover_url(release_mbid: str | None) -> str | None:
    """coverartarchive art URL, in the same form the monitor uses."""

    if not release_mbid:
        return None
    return f'https://coverartarchive.org/release/{release_mbid}/front-250'


def _artist_mbid(metadata: dict[str, Any]) -> str | None:
    for artist in (metadata.get('artist') or {}).get('artists') or []:
        if artist.get('artist_mbid'):
            return str(artist['artist_mbid'])
    return None


def _clean_mbid(value: str) -> str:
    """Normalize a user/DB supplied MBID, rejecting anything that isn't a uuid."""

    mbid = (value or '').strip().lower()
    if not _MBID_RE.match(mbid):
        raise ListenBrainzError(f'"{value}" is not a MusicBrainz MBID (uuid)')
    return mbid


async def recording_metadata(recording_mbids: list[str]) -> dict[str, Any]:
    """Map recording MBID -> artist / recording / release metadata.

    Per-recording results are cached for `CACHE_TTL` seconds, so a seed is only
    resolved once even though several callers ask for it.
    """

    now = time.monotonic()
    metadata: dict[str, Any] = {}
    missing: list[str] = []
    for mbid in dict.fromkeys(recording_mbids):
        cached = _metadata_cache.get(mbid)
        if cached is not None and cached[0] > now:
            metadata[mbid] = cached[1]
            _metadata_cache.move_to_end(mbid)
        else:
            missing.append(mbid)

    for batch in _chunks(missing, METADATA_BATCH):
        data = await _request(
            'POST',
            '/1/metadata/recording/',
            json={'recording_mbids': batch, 'inc': 'artist release'},
        )
        if not isinstance(data, dict):
            continue
        for mbid, entry in data.items():
            metadata[mbid] = entry
            _metadata_cache[mbid] = (now + CACHE_TTL, entry)
        while len(_metadata_cache) > CACHE_MAX * METADATA_BATCH:
            _metadata_cache.popitem(last=False)
    return metadata


async def recording_info(recording_mbid: str) -> dict[str, Any]:
    """Seed details: `{recording_mbid, title, artist, artist_mbid, album, ...}`."""

    mbid = _clean_mbid(recording_mbid)
    if not (metadata := (await recording_metadata([mbid])).get(mbid)):
        raise ListenBrainzError(f'ListenBrainz has no metadata for recording {mbid}')
    recording = metadata.get('recording') or {}
    release = metadata.get('release') or {}
    return {
        'recording_mbid': mbid,
        'title': recording.get('name'),
        'artist': (metadata.get('artist') or {}).get('name'),
        'artist_mbid': _artist_mbid(metadata),
        'album': release.get('name'),
        'length_ms': recording.get('length'),
        'cover_url': _cover_url(release.get('mbid')),
    }


async def radio_recordings_for_artist(
    artist_mbid: str,
    *,
    mode: str = 'easy',
    max_similar_artists: int = 5,
    max_recordings_per_artist: int = 1,
    pop_begin: float = 0.0,
    pop_end: float = 100.0,
) -> dict[str, list[dict[str, Any]]]:
    """Recordings for LB Radio seeded on one artist."""

    return await _request(
        'GET',
        f'/1/lb-radio/artist/{artist_mbid}',
        params={
            'mode': mode,
            'max_similar_artists': max_similar_artists,
            'max_recordings_per_artist': max_recordings_per_artist,
            'pop_begin': pop_begin,
            'pop_end': pop_end,
        },
    )


def _cached(
    cache: OrderedDict, key: tuple, *, refresh: bool
) -> list[dict[str, Any]] | None:
    """Rows for `key` while their TTL is live, else None (and drop the entry)."""

    if refresh:
        return None
    cached = cache.get(key)
    if cached is None:
        return None
    expires, rows = cached
    if expires > time.monotonic():
        cache.move_to_end(key)
        return rows
    cache.pop(key, None)
    return None


def _store(cache: OrderedDict, key: tuple, rows: list[dict[str, Any]]) -> None:
    """Remember `rows` for `CACHE_TTL` seconds, evicting the oldest entries."""

    cache[key] = (time.monotonic() + CACHE_TTL, rows)
    cache.move_to_end(key)
    while len(cache) > CACHE_MAX:
        cache.popitem(last=False)


def _clip(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return rows[:limit] if limit > 0 else list(rows)


async def similar_recordings(
    recording_mbid: str,
    *,
    algorithm: str | None = None,
    limit: int = 0,
    refresh: bool = False,
    rescue: bool = True,
) -> list[dict[str, Any]]:
    """Recordings ListenBrainz listeners play in the same sessions as this one.

    Walks `SIMILAR_SOURCES` in order and returns the first set that knows the
    seed (no coverage means no rows, not an error - each dataset is an offline
    build and misses a lot of tracks). With `rescue` (the default) a seed no
    dataset knows falls back to another recording of the same song (see
    `_rescue_rows`): the datasets are keyed by recording, so a live version or a
    remaster is a different, often unknown seed even when the studio version is
    well covered. Pass `algorithm` to query one dataset only (`algorithm` must
    be one of `SIMILAR_ALGORITHMS`) - that path never rescues.
    """

    seed = _clean_mbid(recording_mbid)
    if algorithm is not None:
        if algorithm not in SIMILAR_ALGORITHMS:
            raise ListenBrainzError(
                f'unknown similar-recordings algorithm {algorithm!r}'
            )
        rows = _clip(await _rows_for(seed, algorithm, refresh), limit)
        return await _with_artist_mbids(rows)

    for candidate in SIMILAR_ALGORITHMS:
        if rows := await _rows_for(seed, candidate, refresh):
            return await _with_artist_mbids(_clip(rows, limit))

    if not rescue:
        return []
    return await _with_artist_mbids(_clip(await _rescue_rows(seed, refresh), limit))


async def _rescue_rows(recording_mbid: str, refresh: bool) -> list[dict[str, Any]]:
    """Rows borrowed from another recording of the same song.

    Session similarity is keyed by recording MBID, so the dataset knows
    "Breathe (In the Air)" from the album but not the live version of the same
    song on a 1995 release - and the latter is exactly what a player reports
    when it plays that release. MusicBrainz is asked for the other recordings of
    the same artist + title and the first one a dataset knows is used as the
    seed. Rows are cached for `CACHE_TTL` seconds; a MusicBrainz failure is not
    cached, so a later request tries again.
    """

    key = (recording_mbid,)
    rows = _cached(_rescue_cache, key, refresh=refresh)
    if rows is not None:
        return rows
    try:
        info = await recording_info(recording_mbid)
        artist = str(info.get('artist') or '').strip()
        title = str(info.get('title') or '').strip()
        if not artist or not title:
            return []
        for candidate in await _same_song_candidates(recording_mbid, artist, title):
            for algorithm in SIMILAR_ALGORITHMS:
                if found := await _rows_for(candidate, algorithm, refresh=False):
                    # Copies: the sibling's own cache entry stays untouched (the
                    # tag would otherwise leak into a direct query for it).
                    rescued = [dict(row, rescued_from=candidate) for row in found]
                    _store(_rescue_cache, key, rescued)
                    return rescued
    except ListenBrainzError:
        return []
    _store(_rescue_cache, key, [])
    return []


async def _same_song_candidates(
    recording_mbid: str, artist: str, title: str
) -> list[str]:
    """Other recording MBIDs for the same artist + title, best match first.

    MusicBrainz answers 503 when its per-second budget is exceeded (it is much
    stricter than ListenBrainz), so the search is retried with a growing pause
    instead of losing the rescue to one hiccup.
    """

    payload: Any = {}
    for attempt in range(MUSICBRAINZ_RETRIES):
        try:
            payload = await _request(
                'GET',
                MUSICBRAINZ_ROOT + '/ws/2/recording/',
                params={
                    'query': (
                        f'artist:"{_query_term(artist)}" '
                        f'AND recording:"{_query_term(title)}"'
                    ),
                    'limit': RESCUE_CANDIDATES + 1,
                    'fmt': 'json',
                },
            )
            break
        except ListenBrainzError:
            if attempt == MUSICBRAINZ_RETRIES - 1:
                raise
            await asyncio.sleep(1.0 * (attempt + 1))
    if not isinstance(payload, dict):
        return []
    candidates: list[str] = []
    for item in payload.get('recordings') or []:
        candidate = str(item.get('id') or '').lower()
        if candidate and candidate != recording_mbid and candidate not in candidates:
            candidates.append(candidate)
    return candidates[:RESCUE_CANDIDATES]


def _query_term(text: str) -> str:
    """Make a term safe to embed in a quoted MusicBrainz Lucene query."""

    return ' '.join(text.replace('"', ' ').replace('\\', ' ').split())


async def _rows_for(
    recording_mbid: str, algorithm: str, refresh: bool
) -> list[dict[str, Any]]:
    """Raw rows for exactly one dataset/algorithm, cached, artist MBIDs unset."""

    key = (recording_mbid, algorithm)
    rows = _cached(_similar_cache, key, refresh=refresh)
    if rows is not None:
        return rows

    payload = await _request(
        'POST',
        LABS_ROOT + _SIMILAR_DATASET[algorithm],
        json=[{'recording_mbids': [recording_mbid], 'algorithm': algorithm}],
        labs=True,
    )
    rows = _similar_rows(payload, recording_mbid)
    for row in rows:
        row['algorithm'] = algorithm
    _store(_similar_cache, key, rows)
    return rows


def _similar_rows(payload: Any, seed_mbid: str) -> list[dict[str, Any]]:
    """Map the Labs response onto the display fields the UI needs."""

    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        candidate = str(item.get('recording_mbid') or '').lower()
        if not candidate or candidate == seed_mbid or candidate in seen:
            continue
        seen.add(candidate)
        credits = item.get('artist_credit_mbids') or []
        rows.append(
            {
                'recording_mbid': candidate,
                'artist': item.get('artist_credit_name'),
                'artist_mbid': str(credits[0]) if credits else None,
                'title': item.get('recording_name'),
                'album': item.get('release_name'),
                'score': item.get('score') or 0,
                'cover_url': _cover_url(
                    item.get('caa_release_mbid') or item.get('release_mbid')
                ),
            }
        )
    # Labelled by score already, but keep the order explicit (and stable).
    rows.sort(key=lambda row: row['score'], reverse=True)
    return _dedupe_albums(rows)


def _dedupe_albums(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep at most `MAX_PER_ALBUM` rows per album (per artist when unknown).

    Session similarity lumps an album's tracks together - without this a
    "similar tracks" list is often the same album five times over.
    """

    counts: dict[tuple[str, str], int] = {}
    kept: list[dict[str, Any]] = []
    for row in rows:
        key = (
            str(row['artist'] or ''),
            str(row['album'] or row['title'] or ''),
        )
        if counts.get(key, 0) >= MAX_PER_ALBUM:
            continue
        counts[key] = counts.get(key, 0) + 1
        kept.append(row)
    return kept


async def _with_artist_mbids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fill missing `artist_mbid`s from the (cached) recording metadata.

    The Labs dataset ships `artist_credit_mbids: null` for every row, so one
    batched metadata call is what makes the artists linkable.
    """

    missing = [row['recording_mbid'] for row in rows if not row['artist_mbid']]
    if not missing:
        return rows
    details = await recording_metadata(missing)
    for row in rows:
        if not row['artist_mbid']:
            row['artist_mbid'] = _artist_mbid(details.get(row['recording_mbid']) or {})
    return rows


async def similar_artists(
    recording_mbid: str,
    *,
    mode: str = 'easy',
    max_similar_artists: int = 10,
    max_recordings_per_artist: int = 1,
    limit: int = 0,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Artists similar to the artist behind `recording_mbid` (seed excluded).

    The seed recording is resolved to its artist MBID and LB Radio's artist pool
    is used as the similarity source; every pool artist contributes one sample
    recording. Each row is `{artist, artist_mbid, recording_mbid, title,
    listen_count}`. The pool depends on the *artist*, not on the seed recording,
    so it is cached per artist MBID - every later track of that artist reuses
    it. Results are cached for `CACHE_TTL` seconds; pass `refresh=True` to
    bypass the cache.
    """

    recording_mbid = _clean_mbid(recording_mbid)
    if mode not in MODES:
        raise ListenBrainzError(f'unknown LB Radio mode {mode!r}')

    artist_mbid = (await recording_info(recording_mbid))['artist_mbid']
    if not artist_mbid:
        raise ListenBrainzError(f'recording {recording_mbid} has no artist MBID')

    key = (artist_mbid, mode, max_similar_artists, max_recordings_per_artist)
    rows = _cached(_artist_cache, key, refresh=refresh)
    if rows is None:
        rows = await _fetch_similar_artists(
            artist_mbid,
            mode=mode,
            max_similar_artists=max_similar_artists,
            max_recordings_per_artist=max_recordings_per_artist,
        )
        _store(_artist_cache, key, rows)
    return _clip(rows, limit)


async def _fetch_similar_artists(
    artist_mbid: str,
    *,
    mode: str,
    max_similar_artists: int,
    max_recordings_per_artist: int,
) -> list[dict[str, Any]]:
    """Uncached lookup: artist -> LB Radio pool -> artist rows."""

    pool = await radio_recordings_for_artist(
        artist_mbid,
        mode=mode,
        max_similar_artists=max_similar_artists,
        max_recordings_per_artist=max_recordings_per_artist,
    )

    artists: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pool_artist_mbid, entries in pool.items():
        if pool_artist_mbid == artist_mbid:  # the seed's own artist
            continue
        sample = next(
            (entry for entry in entries or [] if entry.get('recording_mbid')), None
        )
        if sample is None:
            continue
        candidate = str(sample['recording_mbid']).lower()
        if candidate in seen:
            continue
        seen.add(candidate)
        artists.append(
            {
                'artist': sample.get('similar_artist_name'),
                'artist_mbid': sample.get('similar_artist_mbid') or pool_artist_mbid,
                'recording_mbid': candidate,
                'title': None,
                'listen_count': sample.get('total_listen_count') or 0,
            }
        )
    if not artists:
        return []

    # One batched metadata call names the sample recordings (cached per mbid).
    details = await recording_metadata([artist['recording_mbid'] for artist in artists])
    for artist in artists:
        detail = details.get(artist['recording_mbid']) or {}
        artist['title'] = (detail.get('recording') or {}).get('name')
    return artists
