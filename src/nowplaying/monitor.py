#!/usr/bin/env python3

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

import httpx
from loguru import logger
from PIL import Image, ImageChops

from nowplaying import config, db

try:
    from curl_cffi import requests as _curl_cffi
except ImportError:
    _curl_cffi = None

COVER_FILE = config.COVER_FILE
COVER_URL = config.COVER_URL

_DEFAULTS = {
    'Artist': 'Unknown Artist',
    'Album': 'Unknown Album',
    'Title': 'Unknown Title',
    'Duration': '0',
}
# Fields that identify a track; a signal that only refines the others (e.g.
# Duration) must not record a second history row for the same track.
_IDENTITY_FIELDS = ('Artist', 'Album', 'Title')
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

logger.remove()
logger.add(sys.stderr, level='WARNING')
if config.DEBUG:
    logger.add(config.DEBUG_LOG, level='DEBUG', rotation='50 MB')


def cover_url(url: str) -> str:
    """Served URL for a cover thumbnail: COVER_URL with a version query param."""

    return config.cover_url(url)


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


def download_cover(url: str, trim: bool = True) -> str | None:
    """Download a thumbnail into COVER_FILE; optionally trim black borders."""
    try:
        resp = _HTTP.get(url)
        resp.raise_for_status()
        if len(resp.content) > _MAX_COVER_BYTES:
            logger.warning('[cover] image too large; skipping art')
            return None
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
        return None
    return cover_url(url)


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


def get_recording_length(
    artist: str, title: str, mb_limiter: RateLimiter
) -> int | None:
    """Return the track length in ms from the MusicBrainz recording search, or None."""
    try:
        mb_limiter.wait()
        response = _HTTP.get(
            'https://musicbrainz.org/ws/2/recording/',
            params={
                'query': f'artist:"{artist}" AND recording:"{title}"',
                'limit': 1,
                'fmt': 'json',
            },
        )
        response.raise_for_status()
        if data := response.json().get('recordings'):
            if difflib.SequenceMatcher(None, data[0]['title'], title).ratio() > 0.8:
                if length := data[0].get('length'):
                    logger.debug(f'[musicbrainz] recording length {length}')
                    return int(length)
        logger.debug(f'[musicbrainz] No recording match for "{artist}" - "{title}"')
    except Exception as e:
        logger.warning(f'[musicbrainz] Error: {e}')


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


def verify_description(desc: str) -> str | None:
    if (
        not desc
        or len(desc) > 5000  # essay
        or desc.startswith('Provided to YouTube')  # autogenerated
        or desc.count('http') > 3  # bloated with links
        or desc.count('www') > 3  # bloated with links
    ):
        return

    return desc


def verify_coverarchive(release_group_id: str) -> str | None:
    """Verify coverartarchive url resolves."""

    return validate_url(f'https://coverartarchive.org/release-group/{release_group_id}/front-500')


def youtube_description(video_id: str) -> str | None:
    if not config.GOOGLE_API_KEY:
        return
    try:
        response = _HTTP.get(
            'https://www.googleapis.com/youtube/v3/videos',
            params={
                'part': 'snippet',
                'id': video_id,
                'key': config.GOOGLE_API_KEY
            }
        )
        response.raise_for_status()
        if items := response.json().get('items'):
            return verify_description(items[0]['snippet'].get('description'))
    except Exception as e:
        logger.warning(f'[youtube_description] Error: {e}')


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


class TrackMonitor:
    """Tracks the current track (BlueZ and/or MPRIS); writes it out only when something changed."""

    _COVER_CACHE_MAX = 256
    _COVER_RETRY_TTL = 3600

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
        self._description_attempted: set[str] = set()
        self._duration_attempted: set[tuple[str, str]] = set()
        self._cover_cache: OrderedDict[tuple[str, str, str], tuple[str | None, str | None, str | None, float | None]] = OrderedDict()

        # Identity + row id of the last recorded track, so a late metadata
        # refinement (duration) updates that row instead of duplicating history.
        self._last_identity: tuple[str, str, str] | None = None
        self._last_track_id: int | None = None

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

    def _cover_for(self, artist: str, album: str, title: str) -> tuple[str | None, str | None, str | None]:
        """Return the raw thumbnail URL, MusicBrainz release-group id, and YouTube video id."""

        release_group_id = db.release_for(self._conn, artist, album)
        query = (artist, album, title)
        if query not in self._cover_cache:
            if len(self._cover_cache) >= self._COVER_CACHE_MAX:
                self._cover_cache.popitem(last=False)
            url = None
            video_id = None
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
            self._cover_cache[query] = (url, release_group_id, video_id, time.monotonic() if not url else None)
        else:
            self._cover_cache.move_to_end(query)
        url, release_group_id, video_id, failed_at = self._cover_cache[query]

        if not url:
            if failed_at is not None and time.monotonic() - failed_at >= self._COVER_RETRY_TTL:
                self._cover_cache.pop(query, None)
            return None, release_group_id, video_id

        # Rewrite COVER_FILE whenever the track identity changes, not just the
        # raw URL: two different albums can resolve to the same thumbnail URL,
        # and skipping the download would leave the previous album's bytes on
        # disk under the same (browser-cached) URL. No-album tracks resolve to
        # a pseudo-album named after the title, mirror that for the key so the
        # restart seed (db.latest_cover_key) matches.
        key_album = title if album == _DEFAULTS['Album'] else album
        key = (url, artist, key_album)
        if key != self._last_cover_key:
            logger.debug(f'[cover] downloading new art: {url}')
            if download_cover(url, trim=url.startswith('https://img.youtube')) is None:
                logger.debug('[cover] download failed; skipping art')
                return None, release_group_id, video_id
            self._last_cover_key = key
        return url, release_group_id, video_id

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

    def _fallback_duration(self, artist: str, title: str) -> int | None:
        """Duration for players that don't report one: DB first, then MusicBrainz.

        Some players (e.g. foobar2000 via SMTC) never populate the timeline,
        so `Duration` arrives as 0; a missing duration keeps `tracks.duration`
        NULL, which hides the web UI progress bar and skips the cover lookup.
        """

        if stored := db.stored_duration(self._conn, artist, title):
            return stored
        if (artist, title) in self._duration_attempted:
            return None
        self._duration_attempted.add((artist, title))
        return get_recording_length(artist, title, self._mb_limiter)

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
            # Same track as the last recorded one, only metadata refined (late
            # Duration, or a re-apply after a partial signal reset track_info to
            # the defaults): update the track row in place instead of
            # duplicating the history row.
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
        cover = release_group_id = video_id = None

        if duration is None:
            duration = self._fallback_duration(artist, title)

        if duration:
            try:
                cover, release_group_id, video_id = self._cover_for(
                    self.track_info['Artist'],
                    self.track_info['Album'],
                    self.track_info['Title'],
                )
                logger.debug(f'[write] cover={cover} release_group_id={release_group_id}')
            except Exception:
                logger.exception('[Error] cover lookup failed; persisting track without art')
                cover = release_group_id = video_id = None

            if cover and album is None:
                album = title

        if release_group_id and (artist, album_name) not in self._review_attempted:
            self._review_attempted.add((artist, album_name))
            if not (review := allmusic_review(release_group_id, self._mb_limiter)):
                review = critiquebrainz_review(release_group_id)

        description = None
        if video_id and video_id not in self._description_attempted:
            self._description_attempted.add(video_id)
            if description := youtube_description(video_id):
                logger.debug(f'[write] description ({len(description)} chars) for {video_id}')
        try:
            track_id = db.resolve_track(
                self._conn,
                artist,
                album,
                title,
                cover,
                duration,
                release_group_id,
                review,
                description,
            )
            logger.debug(f'[write] resolved track_id={track_id}')
            self._last_track_id = track_id
            self._last_identity = (artist, album_name, title)
            skip_count = db.skip_count(self._conn, track_id)
            auto = config.SKIP_FOREVER and skip_count >= 3
            logger.debug(f'[write] skip_count={skip_count} auto_skip={auto}')
            db.record_history(self._conn, track_id, auto_skipped=auto)
            db.set_position(self._conn, 0)
        except Exception as e:
            logger.critical(f'[Error] could not write to database: {e}')
            auto = False

        if auto:
            logger.info(f"[Auto-skip] {artist} - {album_name} - {title}")
            self._auto_skip()
        else:
            logger.info(f"[Updated Track] {artist} - {album_name} - {self.track_info['Title']}")


def main():
    async def _amain() -> None:
        loop = asyncio.get_running_loop()
        monitor = TrackMonitor(loop=loop)
        logger.debug(f'DB: {config.DB_PATH} | cover: {config.COVER_FILE}')
        logger.debug(f'Source: {config.SOURCE}')

        if config.SOURCE == 'windows':
            from nowplaying.sources.windows_source import WindowsSource
            source = WindowsSource(monitor, loop)
            try:
                await source.run()
            finally:
                monitor.close()
        else:
            from nowplaying.sources.dbus_source import DbusSource
            from nowplaying.sources.mpris_source import MprisSource

            async def _run_source(source, name: str) -> None:
                try:
                    await source.run()
                except Exception:
                    logger.exception(f'[Error] {name} source terminated unexpectedly')

            try:
                await asyncio.gather(
                    _run_source(DbusSource(monitor, loop), 'BlueZ'),
                    _run_source(MprisSource(monitor, loop), 'MPRIS'),
                )
            finally:
                monitor.close()

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception('[Error] monitor terminated unexpectedly')


if __name__ == '__main__':
    main()
