#!/usr/bin/env python3

import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import OrderedDict
from contextlib import suppress
from io import BytesIO

import httpx
from dbus_next import Message
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, MessageType
from dbus_next.errors import DBusError
from loguru import logger
from PIL import Image, ImageChops

from nowplaying import config, db

COVER_FILE = config.COVER_FILE
COVER_URL = config.COVER_URL

_MATCH_RULE = (
    "type='signal',interface='org.freedesktop.DBus.Properties',"
    "member='PropertiesChanged',arg0='org.bluez.MediaPlayer1'"
)

_SIGNAL_KEYS = ('Artist', 'Album', 'Title', 'Duration')

_DEFAULTS = {
    'Artist': 'Unknown Artist',
    'Album': 'Unknown Album',
    'Title': 'Unknown Title',
    'Duration': '0',
}

_HTTP = httpx.Client(
    timeout=10.0,
    follow_redirects=True,
    headers={'User-Agent': 'nowplaying/1.0.0 (https://github.com/debjan/nowplaying)'},
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

    bbox = diff.getbbox()

    if not bbox:
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

    bbox = diff.getbbox()

    if not bbox:
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
        img = Image.open(BytesIO(_HTTP.get(url).content))
        if trim:
            img = auto_crop(img)
        img.save(COVER_FILE)
    except Exception as e:
        logger.error(f'[Error] could not save album art: {e}')
        return None
    return cover_url(url)


def yt_dlp(artist: str, album: str, title: str) -> str | None:
    """Return a YouTube thumbnail URL for the track, or None if unavailable."""

    if not shutil.which("yt-dlp"):
        return None
    query = f'ytsearch1:{artist},{album},{title}'
    cmd = ['nice', '-n', '15', 'yt-dlp', query, '--flat-playlist', '--dump-single-json', '--playlist-items', '1']
    logger.debug(f'[yt-dlp] {query}')
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
        js = json.loads(proc.stdout)
    except Exception as e:
        logger.warning(f'[Error] album art lookup failed: {e}')
        return None
    if entries := js.get("entries"):
        video_id = entries[0].get("id")
        if video_id:
            logger.debug(f'[yt-dlp] {video_id}')
            return f'https://img.youtube.com/vi/{video_id}/hqdefault.jpg'
    else:
        logger.warning('[yt-dlp] No entries')
    return None


def get_release(artist: str, album: str) -> str | None:
    """Return the `release_group_id` for an album, or None."""
    try:
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
            logger.debug(f'[fanart] {albums}')
            if albumcover := albums[0].get('albumcover'):
                logger.debug(f'[fanart] {albumcover}')
                return albumcover[0]['url']
    except Exception as e:
        logger.warning(f'[fanart] Error: {e}')


def verify_coverarchive(release_group_id: str) -> str | None:
    """Verify coverartarchive url resolves."""

    url = f'https://coverartarchive.org/release-group/{release_group_id}/front-500'
    try:
        if _HTTP.head(url, follow_redirects=True).status_code == 200:
            return url
        else:
            logger.debug(f'[verify] Invalid url: {url}')
            return
    except Exception as e:
        logger.error(f'[verify] Error {e}')


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
        return None


def allmusic_review(release_group_id: str) -> str | None:
    """Return the AllMusic review for an album."""
    try:
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
        r = httpx.get(resource[0], headers={'User-agent': 'Mozilla/5.0'})
        logger.debug(f'[allmusic] Processing {resource[0]}')
        r.raise_for_status()
        if m := re.findall(
            r'<div id="review".*?>\s*?<p>(.*)</p>\s*?</div>', r.text, re.DOTALL
        ):
            return re.sub(r'<[^>]+>', '', m[0]).strip()
        logger.debug(f'[allmusic] No match in {resource[0]}')
    except Exception as e:
        logger.warning(f'[allmusic] Error: {e}')


def remove_emojis(text: str) -> str:
    # Remove emoticons, symbols, pictographs, and transport from track titles
    emoji_pattern = re.compile(
        r'[\U0001F600-\U0001F64F]'   # Emoticons
        r'|[\U0001F300-\U0001F5FF]'  # Symbols & Pictographs
        r'|[\U0001F680-\U0001F6FF]'  # Transport & Map Symbols
        r'|[\U0001F1E0-\U0001F1FF]'  # Flags
        r'|[\u2700-\u27BF]'          # Dingbats
        r'|[\u2600-\u26FF]'          # Miscellaneous Symbols
    )
    return emoji_pattern.sub('', text)


class TrackMonitor:
    """Tracks the current BlueZ track; writes it out only when something changed."""

    _COVER_CACHE_MAX = 256
    _COVER_RETRY_TTL = 3600  # seconds before a failed cover lookup is retried

    def __init__(self) -> None:
        self.track_info = dict(_DEFAULTS)
        self._conn = db.connect(config.DB_PATH)
        self._review_attempted: set[tuple[str, str]] = set()
        self._cover_cache: OrderedDict[tuple[str, str], tuple[str | None, str | None, float | None]] = OrderedDict()

        if os.path.exists(COVER_FILE):
            self._last_cover_url = db.latest_cover_url(self._conn)
        else:
            self._last_cover_url = None

    def close(self) -> None:
        """Close the held-open DB connection."""
        self._conn.close()

    def _is_complete(self) -> bool:
        """True once Artist and Title are real."""

        return (
            self.track_info['Artist'] != _DEFAULTS['Artist']
            and self.track_info['Title'] != _DEFAULTS['Title']
        )

    def _cover_for(self, artist: str, album: str, title: str) -> tuple[str | None, str | None]:
        """Return the raw thumbnail URL and MusicBrainz release-group id."""

        if album == _DEFAULTS['Album']:
            return None, None

        query = (artist, album)
        if query not in self._cover_cache:
            if len(self._cover_cache) >= self._COVER_CACHE_MAX:
                self._cover_cache.popitem(last=False)
            release_group_id = db.release_for(self._conn, artist, album)
            if not release_group_id:
                release_group_id = get_release(artist, album)
            url = None
            if release_group_id:
                url = fanart_cover(release_group_id) or verify_coverarchive(release_group_id)
            if not url and config.YT_DLP:
                url = yt_dlp(artist, album, title)
            if url:
                self._cover_cache[query] = (url, release_group_id, None)
            else:
                self._cover_cache[query] = (None, release_group_id, time.monotonic())
        else:
            self._cover_cache.move_to_end(query)
        url, release_group_id, failed_at = self._cover_cache[query]

        if not url:
            if failed_at is not None and time.monotonic() - failed_at >= self._COVER_RETRY_TTL:
                self._cover_cache.pop(query, None)
            return None, None

        if url != self._last_cover_url:
            logger.debug(f'[cover] downloading new art: {url}')
            if download_cover(url, trim=url.startswith('https://img.youtube')) is None:
                logger.debug('[cover] download failed; skipping art')
                return None, None
            self._last_cover_url = url
        logger.debug(f'[_cover_for] {url}')
        return url, release_group_id

    def _auto_skip(self) -> None:
        """Best-effort `playerctl next`; a failure just means the track plays."""

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

    def apply(self, fields: dict[str, str]) -> None:
        """Apply one D-Bus signal's field updates; write once if anything changed."""

        prev = dict(self.track_info)
        self.track_info = dict(_DEFAULTS)
        for key, value in fields.items():
            value = remove_emojis(value).strip()
            value = ' '.join(value.split())
            if value:
                self.track_info[key] = value
        if self.track_info != prev:
            self.write()
        else:
            logger.debug('[apply] no change vs previous; skipping write')

    def write(self) -> None:
        """Persist once the metadata is complete."""

        logger.success(f'[write] {self.track_info}')

        if not self._is_complete():
            logger.debug('[write] skip: Artist/Title not complete yet')
            return

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

        if release_group_id and (artist, album_name) not in self._review_attempted:
            self._review_attempted.add((artist, album_name))
            review = allmusic_review(release_group_id)
            if not review:
                review = critiquebrainz_review(release_group_id)
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
            )
            logger.debug(f'[write] resolved track_id={track_id}')
            skip_count = db.skip_count(self._conn, track_id)
            auto = config.SKIP_FOREVER and skip_count >= 3
            logger.debug(f'[write] skip_count={skip_count} auto_skip={auto}')
            db.record_history(self._conn, track_id, auto_skipped=auto)
        except Exception as e:
            logger.critical(f'[Error] could not write to database: {e}')
            auto = False

        if auto:
            logger.info(f"[Auto-skip] {artist} - {album_name} - {title}")
            self._auto_skip()
        else:
            logger.info(f"[Updated Track] {artist} - {album_name} - {self.track_info['Title']}")


def _extract_track(meta: dict) -> dict[str, str]:
    """Extract our fields from a BlueZ MediaPlayer1 `Track` dict (a{sv})."""

    fields = {}
    for key in _SIGNAL_KEYS:
        value = meta.get(key)
        if value is not None:
            fields[key] = str(value.value)
    return fields


async def _on_track_signal(bus: MessageBus, monitor: TrackMonitor, msg: Message) -> None:
    """Fetch the MediaPlayer1 `Track` property and apply its metadata."""

    try:
        reply = await bus.call(
            Message(
                destination=msg.sender,
                path=msg.path,
                interface='org.freedesktop.DBus.Properties',
                member='Get',
                signature='ss',
                body=['org.bluez.MediaPlayer1', 'Track'],
            )
        )
        if reply is None:
            logger.debug('[dbus] Get Track returned no reply')
            return
        if reply.message_type != MessageType.METHOD_RETURN:
            logger.debug(f'[dbus] Get Track error: {reply.error_name}')
            return
        meta = reply.body[0].value
        fields = _extract_track(meta)
        logger.debug(f'[dbus] Get Track -> {fields}')
        if any(key in fields for key in _SIGNAL_KEYS[:3]):
            monitor.apply(fields)
        else:
            logger.debug('[dbus] Track metadata incomplete; nothing to apply')
    except Exception:
        logger.exception('[Error] failed to fetch track properties; continuing')


async def _run(bus: MessageBus, monitor: TrackMonitor) -> None:
    """Register the signal match and run until the bus disconnects."""

    def handler(msg: Message) -> None:
        if (
            msg.message_type != MessageType.SIGNAL
            or msg.interface != 'org.freedesktop.DBus.Properties'
            or msg.member != 'PropertiesChanged'
        ):
            return
        if not msg.body or msg.body[0] != 'org.bluez.MediaPlayer1':
            return
        changed = msg.body[1]
        invalidated = msg.body[2] if len(msg.body) > 2 else []
        if 'Track' not in changed and 'Track' not in invalidated:
            return
        if 'Track' in changed:
            fields = _extract_track(changed['Track'].value)
            logger.debug(f'[dbus] Track changed -> {fields}')
            if any(key in fields for key in _SIGNAL_KEYS[:3]):
                try:
                    monitor.apply(fields)
                except Exception:
                    logger.exception('[Error] failed to apply signal; continuing')
                return
        logger.debug('[dbus] Track invalidated; fetching via Get')
        asyncio.create_task(_on_track_signal(bus, monitor, msg))

    bus.add_message_handler(handler)
    try:
        await bus.call(
            Message(
                destination='org.freedesktop.DBus',
                path='/org/freedesktop/DBus',
                interface='org.freedesktop.DBus',
                member='AddMatch',
                signature='s',
                body=[_MATCH_RULE],
            )
        )
    except DBusError as e:
        logger.error(f'[dbus] AddMatch failed: {e} — no signals will be delivered')
    else:
        logger.debug(f'[dbus] AddMatch registered: {_MATCH_RULE}')
    await bus.wait_for_disconnect()


def main():
    async def _amain() -> None:
        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        monitor = TrackMonitor()
        logger.debug(f'DB: {config.DB_PATH} | cover: {config.COVER_FILE}')
        logger.debug('Monitoring D-Bus passively for track metadata...')
        try:
            await _run(bus, monitor)
        finally:
            monitor.close()
            bus.disconnect()

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.exception('[Error] monitor terminated unexpectedly')


if __name__ == '__main__':
    main()
