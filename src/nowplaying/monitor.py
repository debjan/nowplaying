import asyncio
import atexit
import concurrent.futures
import difflib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from contextlib import suppress
from io import BytesIO
from typing import TypedDict
from urllib.parse import unquote, urlsplit

import httpx
from loguru import logger
from PIL import Image, ImageChops

from nowplaying import config, db, listenbrainz

try:
    from curl_cffi import requests as _curl_cffi
except ImportError:
    _curl_cffi = None

COVER_FILE = config.COVER_FILE

_DEFAULTS = {
    'Artist': 'Unknown Artist',
    'Album': 'Unknown Album',
    'Title': 'Unknown Title',
    'Duration': '0',
}
# Fields that identify a track; a signal that only refines the others (e.g.
# Duration) must not record a second history row for the same track.
_IDENTITY_FIELDS = ('Artist', 'Album', 'Title')


class _ListenTrack(TypedDict):
    """Track fields captured on write, used when its listen is submitted."""

    artist: str
    title: str
    album: str | None
    duration: int | None
    mbid: str | None
    artist_mbid: str | None
    release_group_id: str | None


_EMOJI_RE = re.compile(
    r'[\U0001F600-\U0001F64F]'   # Emoticons
    r'|[\U0001F300-\U0001F5FF]'  # Symbols & Pictographs
    r'|[\U0001F680-\U0001F6FF]'  # Transport & Map Symbols
    r'|[\U0001F1E0-\U0001F1FF]'  # Flags
    r'|[\u2700-\u27BF]'          # Dingbats
    r'|[\u2600-\u26FF]'          # Miscellaneous Symbols
)
_HTTP = httpx.Client(
    timeout=10.0,
    follow_redirects=True,
    headers={'User-Agent': 'nowplaying/1.0.0 (https://github.com/debjan/nowplaying)'},
)
atexit.register(_HTTP.close)
_MB_MIN_INTERVAL = 1.1  # MusicBrainz requires <= 1 request/second
_QUICK_SETTINGS_POLL_SECONDS = 5.0
_QUALIFIER_WORDS = frozenset({
    'remaster', 'remastered', 'remix', 'remixed', 'mix', 'live', 'edit',
    'version', 'mono', 'stereo', 'demo', 'acoustic', 'instrumental',
    'deluxe', 'anniversary', 'bonus', 'explicit', 'clean', 'soundtrack',
    'single', 'radio', 'extended', 'reprise', 'feat', 'featuring', 'ft',
    'official', 'video', 'audio', 'lyric', 'lyrics', 'hd', '4k',
})
_TRAILING_GROUP_RE = re.compile(r'\s*[\(\[](?P<body>[^()\[\]]*)[\)\]]\s*$')
_TRAILING_DASH_RE = re.compile(r'\s+[-\u2013\u2014]\s+(?P<body>[^-\u2013\u2014]+)$')
_MAX_COVER_BYTES = 10 * 1024 * 1024

_ALLMUSIC_IMPERSONATE = 'chrome'
_ALLMUSIC_MIN_INTERVAL = 1.0
_BROWSER_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
_LDJSON_RE = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)
_DIV_TAG_RE = re.compile(r'<(/?)div\b[^>]*>', re.IGNORECASE)
_WIKI_HEADING_RE = re.compile(r'^(={2,6})\s*(.+?)\s*\1$')
_WIKI_BIO_HEADING_RE = re.compile(
    r'biograph|early life|life and career', re.IGNORECASE
)

logger.remove()
logger.add(sys.stderr, level='WARNING')
if config.DEBUG:
    logger.add(config.DEBUG_LOG, level='DEBUG', rotation='50 MB')


def detect_side_borders(img: Image.Image, tolerance=50) -> dict:
    """Detects left and right side borders (pillarboxing)."""

    img = img.convert('RGB')
    width, height = img.size

    bg_color = img.getpixel((0, 0))
    bg = Image.new('RGB', img.size, bg_color)

    diff = ImageChops.difference(img, bg)
    if tolerance > 0:
        diff = diff.point(lambda p: 255 if p > tolerance else 0)

    if not (bbox := diff.getbbox()):
        return {
            'has_side_borders': False,
            'left_border_px': 0,
            'right_border_px': 0,
            'crop_box': (0, 0, width, height),
            'bg_color': bg_color,
        }

    left_content, _, right_content, _ = bbox
    left_border = left_content
    right_border = width - right_content

    return {
        'has_side_borders': left_border > 0 or right_border > 0,
        'left_border_px': left_border,
        'right_border_px': right_border,
        'bg_color': bg_color,
        'crop_box': (left_content, 0, right_content, height)
    }


def detect_top_borders(img: Image.Image, tolerance=50) -> dict:
    """Detects top and bottom borders (letterboxing)."""

    img = img.convert('RGB')
    width, height = img.size

    bg_color = img.getpixel((0, 0))
    bg = Image.new('RGB', img.size, bg_color)

    diff = ImageChops.difference(img, bg)
    if tolerance > 0:
        diff = diff.point(lambda p: 255 if p > tolerance else 0)

    if not (bbox := diff.getbbox()):
        return {
            'has_top_bottom_borders': False,
            'top_border_px': 0,
            'bottom_border_px': 0,
            'crop_box': (0, 0, width, height),
            'bg_color': bg_color,
        }

    _, top_content, _, bottom_content = bbox
    top_border = top_content
    bottom_border = height - bottom_content

    return {
        'has_top_bottom_borders': top_border > 0 or bottom_border > 0,
        'top_border_px': top_border,
        'bottom_border_px': bottom_border,
        'bg_color': bg_color,
        'crop_box': (0, top_content, width, bottom_content)
    }


def auto_crop(img: Image.Image) -> Image.Image:
    """Detects and crops out all surrounding borders, saving the clean image."""

    top_info = detect_top_borders(img)
    if top_info['has_top_bottom_borders']:
        logger.debug(f'[crop] Top: {top_info["top_border_px"]}px | Bottom: {top_info["bottom_border_px"]}px')
        img = img.crop(top_info['crop_box'])

    side_info = detect_side_borders(img)
    if side_info['has_side_borders']:
        logger.debug(f'[crop] Left: {side_info["left_border_px"]}px | Right: {side_info["right_border_px"]}px')
        img = img.crop(side_info['crop_box'])

    return img


def download_cover(url: str, trim: bool = True) -> bool:
    """Download a thumbnail into COVER_FILE; optionally trim black borders.

    Returns True once the file is in place, False when the art was skipped.
    """
    try:
        resp = _HTTP.get(url)
        resp.raise_for_status()
        if len(resp.content) > _MAX_COVER_BYTES:
            logger.warning('[cover] image too large; skipping art')
            return False
        img = Image.open(BytesIO(resp.content))
        if trim:
            img = auto_crop(img)
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(COVER_FILE) or '.', suffix='.png'
        )
        try:
            with os.fdopen(fd, 'wb') as f:
                img.save(f, format='PNG')
            os.replace(tmp, COVER_FILE)
        finally:
            with suppress(OSError):
                os.remove(tmp)
    except Exception as e:
        logger.error(f'[Error] could not save album art: {e}')
        return False
    return True


def yt_dlp(artist: str, album: str, title: str) -> str | None:
    """Return a YouTube thumbnail URL for the track, or None if unavailable."""

    if not (shutil.which("yt-dlp") and config.YT_DLP):
        return None
    query = f'ytsearch1:{artist},{album},{title}'
    cmd = ['yt-dlp', query, '--flat-playlist', '--dump-single-json', '--playlist-items', '1']
    if sys.platform != 'win32':
        cmd = ['nice', '-n', '15'] + cmd
    logger.debug(f'[yt-dlp] {query}')
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=15)
        js = json.loads(proc.stdout)
    except Exception as e:
        logger.warning(f'[Error] album art lookup failed: {e}')
        return None
    if entries := js.get("entries"):
        if video_id := entries[0].get("id"):
            logger.debug(f'[yt-dlp] {video_id}')
            return video_id
    else:
        logger.warning('[yt-dlp] No entries')
    return None


def get_youtube_image_url(video_id: str) -> str | None:
    """Return cover url from YouTube video_id"""

    url_prefix = f'https://img.youtube.com/vi/{video_id}'
    url = (
        validate_url(f'{url_prefix}/maxresdefault.jpg') or
        validate_url(f'{url_prefix}/sddefault.jpg') or
        validate_url(f'{url_prefix}/hqdefault.jpg')
    )
    return url


class RateLimiter:
    """Keeps calls to one service under a minimum interval"""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        """Sleep just long enough to respect the minimum interval."""
        elapsed = time.monotonic() - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()


_ALLMUSIC_LIMITER = RateLimiter(_ALLMUSIC_MIN_INTERVAL)


def _is_qualifier(text: str) -> bool:
    """True when a trailing fragment is a version qualifier, not the title."""

    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return bool(re.search(r'\b(?:19|20)\d{2}\b', text))
    return words[0] in _QUALIFIER_WORDS or bool(
        re.fullmatch(r'(?:19|20)\d{2}', words[0])
    )


def _clean_title(title: str) -> str:
    """Strip trailing version qualifiers from a title before searching."""

    original = ' '.join(title.split())
    cleaned = original
    for _ in range(3):
        previous = cleaned
        if match := _TRAILING_GROUP_RE.search(cleaned):
            if _is_qualifier(match.group('body')):
                cleaned = cleaned[: match.start()].strip()
        if match := _TRAILING_DASH_RE.search(cleaned):
            if _is_qualifier(match.group('body')):
                cleaned = cleaned[: match.start()].strip()
        if not cleaned or cleaned == previous:
            break
    return cleaned or original


def _mb_term(text: str) -> str:
    """Make a term safe to embed in a quoted MusicBrainz Lucene query."""

    return ' '.join(text.replace('"', ' ').replace('\\', ' ').split())


def get_release(artist: str, album: str, mb_limiter: RateLimiter) -> str | None:
    """Return the `release_group_id` for an album, or None."""
    try:
        mb_limiter.wait()
        response = _HTTP.get(
            'https://musicbrainz.org/ws/2/release/',
            params={
                'query': f'artist:"{artist}" AND release:"{album}"',
                'limit': 1,
                'fmt': 'json',
            },
        )
        response.raise_for_status()
        if data := response.json().get('releases'):
            if difflib.SequenceMatcher(None, data[0]['title'], album).ratio() > 0.8:
                logger.debug(f"[musicbrainz] {data[0]['release-group']['id']}")
                return data[0]['release-group']['id']
        logger.debug(f'[musicbrainz] No match for "{artist}" - "{album}"')
    except Exception as e:
        logger.warning(f'[musicbrainz] Error: {e}')


def get_recording(
    artist: str, title: str, album: str | None, mb_limiter: RateLimiter
) -> tuple[str | None, str | None, int | None] | None:
    """Return `(recording_mbid, artist_mbid, length_ms)` from the MusicBrainz search."""

    query_title = _clean_title(title)
    artist_term = _mb_term(artist)
    title_term = _mb_term(query_title)
    queries: list[str] = []
    if album:
        queries.append(
            f'artist:"{artist_term}" AND recording:"{title_term}" '
            f'AND release:"{_mb_term(album)}"'
        )
    queries.append(f'artist:"{artist_term}" AND recording:"{title_term}"')
    for query in queries:
        try:
            mb_limiter.wait()
            response = _HTTP.get(
                'https://musicbrainz.org/ws/2/recording/',
                params={'query': query, 'limit': 1, 'fmt': 'json'},
            )
            response.raise_for_status()
            if data := response.json().get('recordings'):
                if difflib.SequenceMatcher(None, data[0]['title'], query_title).ratio() > 0.8:
                    mbid = data[0].get('id')
                    credits = data[0].get('artist-credit') or []
                    credited = (credits[0] or {}).get('artist') or {}
                    artist_mbid = credited.get('id')
                    length = data[0].get('length')
                    logger.debug(
                        f'[musicbrainz] recording {mbid} artist {artist_mbid} '
                        f'length {length}'
                    )
                    return mbid, artist_mbid, int(length) if length else None
            logger.debug(f'[musicbrainz] No recording match for "{artist}" - "{title}"')
        except Exception as e:
            logger.warning(f'[musicbrainz] Error: {e}')
            return None
    return None, None, None


def fanart_cover(release_group_id: str) -> str | None:
    """Return Fanart cover URL"""
    try:
        response = _HTTP.get(
            f'https://webservice.fanart.tv/v3.2/music/albums/{release_group_id}',
            params={'api_key': config.FANART_API_KEY or config.FANART_PROJECT}
        )
        if error := response.json().get('error'):
            logger.debug(f'[fanart] {error}')
            return
        response.raise_for_status()
        if albums := response.json().get('albums'):
            if albumcover := albums[0].get('albumcover'):
                return albumcover[0]['url']
    except Exception as e:
        logger.warning(f'[fanart] Error: {e}')


def validate_url(url: str) -> str | None:
    try:
        if _HTTP.head(url, follow_redirects=True).status_code == 200:
            return url
        else:
            logger.debug(f'[validate_url] Invalid url: {url}')
            return
    except Exception as e:
        logger.error(f'[validate_url] Error {e}')


def verify_coverarchive(release_group_id: str) -> str | None:
    """Verify coverartarchive url resolves."""

    return validate_url(f'https://coverartarchive.org/release-group/{release_group_id}/front-500')


def critiquebrainz_review(release_group_id: str) -> str | None:
    """Return the CritiqueBrainz review markdown for an album."""
    try:
        response = _HTTP.get(
            'https://critiquebrainz.org/ws/1/review/',
            params={
                'entity_id': release_group_id,
                'entity_type': 'release_group',
                'limit': 1,
                'fmt': 'json',
            },
        )
        response.raise_for_status()
        if reviews := response.json().get('reviews'):
            logger.debug(f"[critiquebrainz] {reviews[0]['text']}")
            return reviews[0]['text']
    except Exception as e:
        logger.warning(f'[critiquebrainz] Error: {e}')


def _allmusic_page(url: str) -> str:
    """Fetch an AllMusic page, getting past Cloudflare's bot challenge."""
    if _curl_cffi is None:
        response = _HTTP.get(url, headers={'User-Agent': _BROWSER_UA})
        response.raise_for_status()
        return response.text
    response = _curl_cffi.get(url, impersonate=_ALLMUSIC_IMPERSONATE, timeout=20)
    response.raise_for_status()
    return response.text


def _allmusic_page_review(page: str) -> str | None:
    """AllMusic's staff review from the page's schema.org `Review` blocks."""
    for block in _LDJSON_RE.findall(page):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        items = data.get('@graph', data) if isinstance(data, dict) else data
        for item in items if isinstance(items, list) else [items]:
            if not isinstance(item, dict) or item.get('@type') != 'Review':
                continue
            author = item.get('author')
            name = author.get('name') if isinstance(author, dict) else author
            body = item.get('reviewBody')
            if name == 'AllMusic' and isinstance(body, str) and body.strip():
                return html.unescape(body).strip()
    return None


def allmusic_review(release_group_id: str, mb_limiter: RateLimiter) -> str | None:
    """Return the AllMusic review for an album."""
    try:
        mb_limiter.wait()
        response = _HTTP.get(
            f'https://musicbrainz.org/ws/2/release-group/{release_group_id}',
            params={'inc': 'url-rels', 'fmt': 'json'},
        )
        response.raise_for_status()
        relations = response.json().get('relations') or []
        resource = [
            r['url']['resource']
            for r in relations
            if r.get('type') == 'allmusic' and r.get('url')
        ]
        if not resource:
            return None
        _ALLMUSIC_LIMITER.wait()
        logger.debug(f'[allmusic] Processing {resource[0]}')
        if review := _allmusic_page_review(_allmusic_page(resource[0])):
            return review
        logger.debug(f'[allmusic] No review in {resource[0]}')
    except Exception as e:
        logger.warning(f'[allmusic] Error: {e}')


def _extract_div_by_id(page: str, element_id: str) -> str | None:
    """Inner HTML of the first `<div id="...">`, honouring nested divs."""

    opening = re.search(
        rf'<div\b[^>]*\bid="{re.escape(element_id)}"[^>]*>',
        page,
        re.IGNORECASE,
    )
    if opening is None:
        return None
    depth = 1
    for tag in _DIV_TAG_RE.finditer(page, opening.end()):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return page[opening.end() : tag.start()]
    return page[opening.end() :]


def _html_text(fragment: str) -> str:
    """Strip tags into readable plain text, one paragraph per line."""

    fragment = re.sub(
        r'<(script|style)\b.*?</\1>', ' ', fragment, flags=re.IGNORECASE | re.DOTALL
    )
    text = re.sub(
        r'<(?:br\s*/?|/p|/div|/li|/h[1-6])\s*>', '\n', fragment, flags=re.IGNORECASE
    )
    text = re.sub(r'<[^>]+>', '', text)
    text = html.unescape(text)
    lines = [' '.join(line.split()) for line in text.splitlines()]
    return '\n'.join(line for line in lines if line)


def _allmusic_page_biography(page: str) -> str | None:
    """AllMusic artist biography from the page's `biography` div."""

    inner = _extract_div_by_id(page, 'biography')
    if inner is None:
        return None
    # The heading carries the artist name and author credit; the dialog already
    # names the artist, so keep only the prose.
    inner = re.sub(r'<h3\b.*?</h3>', ' ', inner, flags=re.IGNORECASE | re.DOTALL)
    return _html_text(inner) or None


def _wikidata_wikipedia_url(wikidata_url: str) -> str | None:
    """Return the English Wikipedia URL for a Wikidata entity URL."""

    wikidata_id = wikidata_url.rstrip('/').rsplit('/', 1)[-1]
    if not wikidata_id:
        return None
    response = _HTTP.get(
        'https://www.wikidata.org/w/api.php',
        params={
            'action': 'wbgetentities',
            'format': 'json',
            'props': 'sitelinks/urls',
            'ids': wikidata_id,
            'sitefilter': 'enwiki',
        },
    )
    response.raise_for_status()
    entity = response.json().get('entities', {}).get(wikidata_id, {})
    url = ((entity.get('sitelinks') or {}).get('enwiki') or {}).get('url')
    return str(url) if url else None


def _wikipedia_biography(url: str) -> str | None:
    """Plain-text biography section of an English Wikipedia article."""

    path = urlsplit(url).path
    if '/wiki/' not in path:
        return None
    title = unquote(path.split('/wiki/', 1)[1]).replace('_', ' ')
    if not title:
        return None
    response = _HTTP.get(
        'https://en.wikipedia.org/w/api.php',
        params={
            'action': 'query',
            'prop': 'extracts',
            'explaintext': 1,
            'exsectionformat': 'wiki',
            'redirects': 1,
            'titles': title,
            'format': 'json',
            'formatversion': 2,
        },
    )
    response.raise_for_status()
    pages = response.json().get('query', {}).get('pages') or []
    text = (pages[0].get('extract') if pages else None) or ''
    lines = text.splitlines()
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        if (heading := _WIKI_HEADING_RE.match(line)) is None:
            continue
        if start is None:
            if _WIKI_BIO_HEADING_RE.search(heading.group(2)):
                start = index + 1
                level = len(heading.group(1))
        elif len(heading.group(1)) <= level:
            return '\n'.join(lines[start:index]).strip() or None
    if start is not None:
        return '\n'.join(lines[start:]).strip() or None
    return None


def artist_biography(artist_mbid: str, mb_limiter: RateLimiter) -> str | None:
    """Return an artist biography: AllMusic first, English Wikipedia after."""

    try:
        mb_limiter.wait()
        response = _HTTP.get(
            f'https://musicbrainz.org/ws/2/artist/{artist_mbid}',
            params={'inc': 'url-rels', 'fmt': 'json'},
        )
        response.raise_for_status()
        relations = response.json().get('relations') or []
    except Exception as e:
        logger.warning(f'[biography] MusicBrainz error: {e}')
        return None

    resources = {
        r['type']: r['url']['resource']
        for r in relations
        if r.get('type') in ('allmusic', 'wikidata') and r.get('url')
    }
    if allmusic_url := resources.get('allmusic'):
        # A broken AllMusic fetch must not cost us the Wikipedia fallback.
        try:
            _ALLMUSIC_LIMITER.wait()
            logger.debug(f'[allmusic] Processing artist {allmusic_url}')
            if biography := _allmusic_page_biography(_allmusic_page(allmusic_url)):
                return biography
            logger.debug(f'[allmusic] No biography in {allmusic_url}')
        except Exception as e:
            logger.warning(f'[allmusic] Biography error: {e}')
    if wikidata_url := resources.get('wikidata'):
        try:
            if wikipedia_url := _wikidata_wikipedia_url(wikidata_url):
                logger.debug(f'[wikipedia] Processing {wikipedia_url}')
                return _wikipedia_biography(wikipedia_url)
        except Exception as e:
            logger.warning(f'[wikipedia] Biography error: {e}')
    return None


class TrackMonitor:
    """Tracks the current track (BlueZ and/or MPRIS); writes it out only when something changed."""

    _COVER_CACHE_MAX = 256
    _COVER_RETRY_TTL = 3600
    _RECORDING_RETRY_TTL = 60.0

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.track_info = dict(_DEFAULTS)
        self._conn = db.connect(config.DB_PATH)
        self.player_path: str | None = None
        self.player_service: str | None = None
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix='track'
        )
        self._mb_limiter = RateLimiter(_MB_MIN_INTERVAL)
        self._review_attempted: set[tuple[str, str]] = set()
        self._biography_attempted: set[str] = set()
        self._duration_attempted: set[tuple[str, str]] = set()
        self._recording_cache: dict[
            tuple[str, str, str | None],
            tuple[str | None, str | None, int | None],
        ] = {}
        # Lookups MusicBrainz could not answer, with the time they failed: not
        # cached as a result, so a later playback tries again.
        self._recording_failed: dict[tuple[str, str, str | None], float] = {}
        self._cover_cache: OrderedDict[tuple[str, str, str], tuple[str | None, str | None, float | None]] = OrderedDict()

        # Identity + row id of the last recorded track, so a late metadata
        # refinement (duration) updates that row instead of duplicating history.
        self._last_identity: tuple[str, str, str] | None = None
        self._last_track_id: int | None = None

        # ListenBrainz submission state for the current play: the furthest
        # position seen (our played-time estimate), whether the listen already
        # went out, and the payload captured when the track was written.
        self._max_position_ms = 0
        self._listen_submitted = False
        self._listen_track: _ListenTrack | None = None

        if os.path.exists(COVER_FILE):
            self._last_cover_key = db.latest_cover_key(self._conn)
        else:
            self._last_cover_key = (None, None, None)

    def submit(self, fn, *args) -> None:
        """Run fn on the worker thread; failures are logged, never raised."""
        def _safe():
            try:
                fn(*args)
            except Exception:
                logger.exception(f'[Error] worker task {getattr(fn, "__name__", fn)} failed')

        self._executor.submit(_safe)

    def close(self) -> None:
        """Drain and shut down the worker, then close the DB connection."""
        self._executor.shutdown(wait=True)
        self._conn.close()

    def _is_complete(self) -> bool:
        """True once Artist and Title are real."""

        return (
            self.track_info['Artist'] != _DEFAULTS['Artist']
            and self.track_info['Title'] != _DEFAULTS['Title']
        )

    def _cover_for(self, artist: str, album: str, title: str) -> tuple[str | None, str | None]:
        """Return the raw thumbnail URL and MusicBrainz release-group id."""

        release_group_id = db.release_for(self._conn, artist, album)
        query = (artist, album, title)
        if query not in self._cover_cache:
            if len(self._cover_cache) >= self._COVER_CACHE_MAX:
                self._cover_cache.popitem(last=False)
            url = None
            if album == _DEFAULTS['Album']:
                if video_id := yt_dlp(artist, '', title):
                    url = get_youtube_image_url(video_id)
            else:
                if not release_group_id:
                    release_group_id = get_release(artist, album, self._mb_limiter)
                if release_group_id:
                    url = fanart_cover(release_group_id) or verify_coverarchive(release_group_id)
                if not url:
                    if video_id := yt_dlp(artist, album, title):
                        url = get_youtube_image_url(video_id)
            self._cover_cache[query] = (url, release_group_id, time.monotonic() if not url else None)
        else:
            self._cover_cache.move_to_end(query)
        url, release_group_id, failed_at = self._cover_cache[query]

        if not url:
            if failed_at is not None and time.monotonic() - failed_at >= self._COVER_RETRY_TTL:
                self._cover_cache.pop(query, None)
            return None, release_group_id

        key_album = title if album == _DEFAULTS['Album'] else album
        key = (url, artist, key_album)
        if key != self._last_cover_key:
            logger.debug(f'[cover] downloading new art: {url}')
            if not download_cover(url, trim=url.startswith('https://img.youtube')):
                logger.debug('[cover] download failed; skipping art')
                return None, release_group_id
            self._last_cover_key = key
        return url, release_group_id

    @staticmethod
    def _log_future(fut: concurrent.futures.Future) -> None:
        """Log background control failures; never raise."""
        try:
            fut.result()
        except Exception:
            logger.debug('[auto-skip] windows control failed')

    def _auto_skip(self) -> None:
        """Best-effort skip; a failure just means the track plays."""

        if config.SOURCE == 'windows':
            from nowplaying import windows_control

            try:
                fut = asyncio.run_coroutine_threadsafe(
                    windows_control.control('next'), self._loop
                )
                fut.add_done_callback(self._log_future)
            except Exception:
                logger.debug('[auto-skip] failed to schedule windows control')
            return
        cmd = ['playerctl']
        if config.PLAYERCTL_PLAYER:
            cmd += ['--player', config.PLAYERCTL_PLAYER]
        cmd += ['next']
        with suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )

    def set_position(self, position_ms: int) -> None:
        """Persist a seek/position update (Position-only signal, no history row)."""
        try:
            db.set_position(self._conn, position_ms)
        except Exception as e:
            logger.warning(f'[Error] could not store position: {e}')
            return
        self._track_listen(position_ms)

    def _track_listen(self, position_ms: int) -> None:
        """Submit one listen once the current play passes the play-time gate."""

        if self._listen_track is None or not config.LISTENBRAINZ_SUBMIT:
            return
        threshold = config.LISTENBRAINZ_MIN_PLAY_MS
        if position_ms < 1000 and self._max_position_ms >= threshold:
            # Repeat-one: the player restarted the track, so this is a new play.
            self._listen_submitted = False
            self._max_position_ms = position_ms
        # The furthest position, not the current one: a seek backwards must not
        # erase the played time already counted.
        self._max_position_ms = max(self._max_position_ms, position_ms)
        if not self._listen_submitted and self._max_position_ms >= threshold:
            self._listen_submitted = True
            self._submit_listen()

    @staticmethod
    def _log_listen(fut: concurrent.futures.Future) -> None:
        """Log a failed listen submission; never raise (also on shutdown)."""
        try:
            fut.result()
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f'[listenbrainz] listen not submitted: {e}')

    def _submit_listen(self) -> None:
        """Ship the current play to ListenBrainz; never blocks the worker."""

        track = self._listen_track
        if track is None:
            return
        logger.debug(f'[listenbrainz] submitting {track["artist"]} - {track["title"]}')
        try:
            fut = asyncio.run_coroutine_threadsafe(
                listenbrainz.submit_listen(
                    artist=track['artist'],
                    title=track['title'],
                    album=track['album'],
                    duration_ms=track['duration'],
                    recording_mbid=track['mbid'],
                    artist_mbid=track['artist_mbid'],
                    release_group_mbid=track['release_group_id'],
                    # ListenBrainz wants the playback start, not "now".
                    listened_at=int(time.time()) - self._max_position_ms // 1000,
                ),
                self._loop,
            )
        except Exception:
            logger.debug('[listenbrainz] failed to schedule listen submission')
            return
        fut.add_done_callback(self._log_listen)

    def _recording(
        self, artist: str, title: str, album: str | None
    ) -> tuple[str | None, str | None, int | None] | None:
        """`(recording_mbid, artist_mbid, length_ms)` from MusicBrainz, cached per track."""

        key = (artist, title, album)
        if key in self._recording_cache:
            return self._recording_cache[key]
        failed_at = self._recording_failed.get(key)
        if failed_at is not None:
            if time.monotonic() - failed_at < self._RECORDING_RETRY_TTL:
                return None
            del self._recording_failed[key]
        if len(self._recording_cache) >= self._COVER_CACHE_MAX:
            self._recording_cache.clear()
            self._recording_failed.clear()
        result = get_recording(artist, title, album, self._mb_limiter)
        if result is None:
            self._recording_failed[key] = time.monotonic()
            return None
        self._recording_cache[key] = result
        return result

    def _mbids_for(
        self, artist: str, title: str, album: str | None
    ) -> tuple[str | None, str | None]:
        """Recording and artist MBIDs: MusicBrainz first, stored values after."""

        recording = self._recording(artist, title, album)
        if recording:
            recording_mbid, artist_mbid = recording[0], recording[1]
            return (
                recording_mbid or db.mbid_for(self._conn, artist, title),
                artist_mbid or db.artist_mbid_for(self._conn, artist),
            )
        return db.mbid_for(self._conn, artist, title), db.artist_mbid_for(
            self._conn, artist
        )

    def _fallback_duration(
        self, artist: str, title: str, album: str | None
    ) -> int | None:
        """Duration for players that don't report one: DB first, then MusicBrainz."""

        if stored := db.stored_duration(self._conn, artist, title):
            return stored
        if (artist, title) in self._duration_attempted:
            return None
        recording = self._recording(artist, title, album)
        if recording is None:
            return None  # MusicBrainz unreachable: retry on a later playback
        self._duration_attempted.add((artist, title))
        return recording[2]

    def apply(self, fields: dict[str, str]) -> None:
        """Apply one D-Bus signal's field updates; write once if anything changed."""

        prev = dict(self.track_info)
        self.track_info = dict(_DEFAULTS)
        for key, value in fields.items():
            value = _EMOJI_RE.sub('', value).strip()
            if value := ' '.join(value.split()):
                self.track_info[key] = value
        if self.track_info == prev:
            logger.debug('[apply] no change vs previous; skipping write')
            return
        identity = tuple(self.track_info[k] for k in _IDENTITY_FIELDS)
        if identity == self._last_identity:
            self._refine_track()
            return
        self.write()

    def _refine_track(self) -> None:
        """Persist a late duration for the current track; no new history row."""

        raw = self.track_info['Duration']
        if raw == _DEFAULTS['Duration'] or self._last_track_id is None:
            return
        try:
            duration = int(raw)
        except ValueError:
            return
        if duration <= 0:
            return
        try:
            db.set_duration(self._conn, self._last_track_id, duration)
            logger.debug(f'[apply] duration {duration}ms for track {self._last_track_id}')
        except Exception as e:
            logger.warning(f'[Error] could not update duration: {e}')

    def write(self) -> None:
        """Persist once the metadata is complete."""

        # Re-read the dashboard's quick settings here: this write is exactly when
        # auto-skip is decided, so a toggle applies to the very next track
        # instead of waiting for the watcher's poll.
        config.reload_quick_settings()
        logger.success(f'[write] {self.track_info}')

        if not self._is_complete():
            logger.debug('[write] skip: Artist/Title not complete yet')
            return

        album = (
            None
            if self.track_info['Album'] == _DEFAULTS['Album']
            else self.track_info['Album']
        )
        duration = (
            int(self.track_info['Duration'])
            if self.track_info['Duration'] != _DEFAULTS['Duration']
            else None
        )
        review = None
        artist = self.track_info['Artist']
        album_name = self.track_info['Album']
        title = self.track_info['Title']
        cover = release_group_id = None

        if duration is None:
            duration = self._fallback_duration(artist, title, album)

        try:
            mbid, artist_mbid = self._mbids_for(artist, title, album)
        except Exception:
            logger.exception('[Error] MusicBrainz recording lookup failed')
            mbid = artist_mbid = None

        biography = None
        if artist_mbid and artist not in self._biography_attempted:
            self._biography_attempted.add(artist)
            try:
                if not db.biography_for(self._conn, artist):
                    biography = artist_biography(artist_mbid, self._mb_limiter)
                    logger.debug(f'[write] biography={"yes" if biography else "none"}')
            except Exception:
                logger.exception('[Error] artist biography lookup failed')

        try:
            cover, release_group_id = self._cover_for(
                self.track_info['Artist'],
                self.track_info['Album'],
                self.track_info['Title'],
            )
            logger.debug(f'[write] cover={cover} release_group_id={release_group_id}')
        except Exception:
            logger.exception('[Error] cover lookup failed; persisting track without art')
            cover = release_group_id = None

        if cover and album is None:
            album = title

        if release_group_id and (artist, album_name) not in self._review_attempted:
            self._review_attempted.add((artist, album_name))
            if not (review := allmusic_review(release_group_id, self._mb_limiter)):
                review = critiquebrainz_review(release_group_id)

        try:
            # Read before record_history/set_position overwrite them: the newest
            # row and the position of the play that is ending.
            previous_history = db.latest_history(self._conn)
            previous_position = db.current_position(self._conn)
            track_id = db.resolve_track(
                self._conn,
                artist,
                album,
                title,
                cover,
                duration,
                release_group_id,
                review,
                mbid,
                artist_mbid,
                biography,
            )
            logger.debug(f'[write] resolved track_id={track_id} mbid={mbid}')
            self._last_track_id = track_id
            self._last_identity = (artist, album_name, title)
            feedback = db.feedback_score(self._conn, track_id)
            # Only a hated verdict auto-skips (there is no skip counter any
            # more - `feedback` is the single verdict store).
            auto = config.AUTOSKIP and feedback == listenbrainz.FEEDBACK_HATE
            logger.debug(f'[write] feedback={feedback} auto_skip={auto}')
            db.record_history(self._conn, track_id, auto_skipped=auto)
            db.set_position(self._conn, 0)
        except Exception as e:
            logger.critical(f'[Error] could not write to database: {e}')
            auto = False
            self._listen_track = None
        else:
            self._listen_track = {
                'artist': artist,
                'title': title,
                'album': album,
                'duration': duration,
                'mbid': mbid,
                'artist_mbid': artist_mbid,
                'release_group_id': release_group_id,
            }
            self._max_position_ms = 0
            # A restart re-writes the track that is already playing; the
            # position continued, so that play was already submitted.
            continuing = (
                previous_history is not None
                and previous_position >= config.LISTENBRAINZ_MIN_PLAY_MS
                and previous_history[1] == track_id
            )
            # Auto-skipped tracks never reach the dashboard, so they don't
            # reach ListenBrainz either.
            self._listen_submitted = bool(auto or continuing)

        if auto:
            logger.info(f"[Auto-skip] {artist} - {album_name} - {title}")
            self._auto_skip()
        else:
            logger.info(f"[Updated Track] {artist} - {album_name} - {self.track_info['Title']}")


async def _watch_quick_settings(interval: float = _QUICK_SETTINGS_POLL_SECONDS) -> None:
    """Reload quick settings from the `.env` file so dashboard toggles apply live.

    The web server persists dashboard setting changes to the project-root `.env`;
    this polls it and re-applies the flags into the config module without a restart.
    """

    try:
        previous = config.quick_settings_from_disk()
        while True:
            await asyncio.sleep(interval)
            current = config.quick_settings_from_disk()
            if current == previous:
                continue
            previous = dict(current)
            logger.info(f'[settings] quick settings reloaded from {config.ENV_PATH}: {current}')
            config.reload_quick_settings()
    except Exception:
        # A settings reload must never take the monitor down with it.
        logger.exception('[settings] quick-settings watcher stopped')


def main():
    async def _log_token() -> None:
        """Log once at startup whether the ListenBrainz token is usable."""

        try:
            user = await listenbrainz.validate_token()
        except listenbrainz.ListenBrainzError as exc:
            logger.warning(f'[listenbrainz] token check failed: {exc}')
            return
        if user:
            logger.info(f'[listenbrainz] submitting listens as {user}')
        else:
            logger.warning('[listenbrainz] token invalid; listens will not be submitted')

    async def _amain() -> None:
        loop = asyncio.get_running_loop()
        monitor = TrackMonitor(loop=loop)
        logger.debug(f'DB: {config.DB_PATH} | cover: {config.COVER_FILE}')
        logger.debug(f'Source: {config.SOURCE}')

        token_check = (
            asyncio.create_task(_log_token()) if config.LISTENBRAINZ_SUBMIT else None
        )
        quick_settings = asyncio.create_task(_watch_quick_settings())
        try:
            if config.SOURCE == 'windows':
                from nowplaying.sources.windows_source import WindowsSource
                source = WindowsSource(monitor, loop)
                await source.run()
            else:
                from nowplaying.sources.dbus_source import DbusSource
                from nowplaying.sources.mpris_source import MprisSource

                async def _run_source(source, name: str) -> None:
                    try:
                        await source.run()
                    except Exception:
                        logger.exception(f'[Error] {name} source terminated unexpectedly')

                await asyncio.gather(
                    _run_source(DbusSource(monitor, loop), 'BlueZ'),
                    _run_source(MprisSource(monitor, loop), 'MPRIS'),
                )
        finally:
            if token_check is not None:
                token_check.cancel()
                with suppress(asyncio.CancelledError):
                    await token_check
            quick_settings.cancel()
            with suppress(asyncio.CancelledError):
                await quick_settings
            monitor.close()
            await listenbrainz.aclose()

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception('[Error] monitor terminated unexpectedly')


if __name__ == '__main__':
    main()
