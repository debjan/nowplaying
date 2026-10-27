import asyncio
import colorsys
import hashlib
import json
import re
import shutil
import sqlite3
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import NamedTuple

import aiofiles
import aiofiles.os
import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)
from PIL import Image

from nowplaying import config, db, listenbrainz

WEB_DIR = Path(__file__).resolve().parent.parent.parent / 'web'
INDEX = WEB_DIR / 'index.html'
CSS = WEB_DIR / 'style.css'
COVER_SVG = WEB_DIR / 'cover.svg'
THEMES_DIR = WEB_DIR / 'themes'

PLAYERCTL_ACTIONS = frozenset({'play-pause', 'next', 'previous'})

# ListenBrainz "similar" dialogs: how many rows to return by default/max.
SIMILAR_LIMIT_DEFAULT = 12
SIMILAR_LIMIT_MAX = 50
SIMILAR_ARTIST_LIMIT_DEFAULT = 10

if config.SOURCE == 'windows':
    from nowplaying import windows_control
    PLAYERCTL_AVAILABLE = True
else:
    PLAYERCTL_AVAILABLE = shutil.which('playerctl') is not None

_ACCENT_RE = re.compile(r'--accent\s*:\s*(#[0-9a-fA-F]{3,8})')

# Dynamic themes: their palettes are derived from the current album cover's
# average color (see `_adaptive_palette`) instead of a static CSS file.
ADAPTIVE_THEMES = frozenset({'adaptive-light.css', 'adaptive-dark.css'})

_ADAPTIVE_FALLBACKS = {
    'adaptive-dark.css': {
        'foreground': '#DCDCDC',
        'background': '#141414',
        'shadow': '#242424',
        'accent': '#8A9A7B',  # muted sage, visibly not Gruvbox green
    },
    'adaptive-light.css': {
        'foreground': '#2A2A2A',
        'background': '#F2F2F2',
        'shadow': '#CFCFCF',
        'accent': '#4F6B45',
    },
}

# Adaptive palettes stay deliberately muted: a high-chroma cover would
# otherwise tint the whole UI with a garish accent. These cap the HSL
# saturation of the generated colors; the cover's hue is always preserved.
_TINT_SAMPLE_SATURATION = 0.25  # pixel qualifies as a tint source above this
_ADAPTIVE_TINT_SATURATION = 0.16  # background
_ADAPTIVE_SHADOW_SATURATION = 0.20
# Below this cover saturation the art is effectively grayscale: keep the neutral
# fallback accent instead of inventing a hue from rounding noise.
_ACHROMATIC_SATURATION = 0.12


class _AdaptiveStyle(NamedTuple):
    """Tuning for one adaptive theme; HSL values are all in [0, 1]."""

    foreground: tuple[float, float]  # (saturation cap, lightness)
    background_l: float
    shadow_l: float
    accent_s: tuple[float, float]  # (saturation floor, saturation cap)
    accent_l: tuple[float, float]  # lightness clamp


# Per theme: foreground (saturation cap, lightness), background/shadow
# lightness, and the accent's saturation band + lightness clamp.
_ADAPTIVE_STYLES = {
    'adaptive-dark.css': _AdaptiveStyle(
        foreground=(0.08, 0.88),
        background_l=0.08,
        shadow_l=0.16,
        accent_s=(0.20, 0.46),
        accent_l=(0.58, 0.78),
    ),
    'adaptive-light.css': _AdaptiveStyle(
        foreground=(0.10, 0.20),
        background_l=0.92,
        shadow_l=0.84,
        accent_s=(0.18, 0.42),
        accent_l=(0.40, 0.52),
    ),
}

_asset_cache: dict[Path, tuple[str, bytes]] = {}

_adaptive_palette_cache: dict[str, tuple[tuple[int | None, bool], dict[str, str]]] = {}


def _hex(h: float, s: float, l: float) -> str:
    """HSL (all in [0, 1]) as '#RRGGBB'."""
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return f'#{round(r * 255):02X}{round(g * 255):02X}{round(b * 255):02X}'


def _cover_hsl() -> tuple[float, float, float] | None:
    """Average color of the current album cover as (hue, lightness, saturation).

    Samples only clearly saturated pixels (falling back to the plain whole-image
    mean for near-grayscale art): averaging the entire cover cancels complementary
    hues and washes the mean out to near-zero saturation, which made most covers
    trip the achromatic rule and rendered the palette Gruvbox-like even with art.
    """
    try:
        with Image.open(config.COVER_FILE) as img:
            thumb = img.convert('RGB').resize((32, 32))
        raw = thumb.tobytes()
    except (OSError, ValueError):
        return None
    pixels = [(raw[i], raw[i + 1], raw[i + 2]) for i in range(0, len(raw), 3)]
    tinted = [
        (r, g, b)
        for (r, g, b) in pixels
        if _pixel_saturation(r, g, b) >= _TINT_SAMPLE_SATURATION
    ]
    chosen = tinted or pixels
    n = len(chosen)
    rgb = tuple(sum(px[i] for px in chosen) / n / 255 for i in range(3))
    return colorsys.rgb_to_hls(*rgb)


def _pixel_saturation(r: int, g: int, b: int) -> float:
    """HSL saturation of one 8-bit RGB pixel."""
    _, _, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    return s


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def _adaptive_palette(name: str) -> dict[str, str]:
    """Palette for an adaptive theme, tinted by the cover's average color."""
    sample = _cover_hsl()
    style = _ADAPTIVE_STYLES[name]
    if sample is None:
        return dict(_ADAPTIVE_FALLBACKS[name])
    h, l, s = sample
    accent = _ADAPTIVE_FALLBACKS[name]['accent']
    if s >= _ACHROMATIC_SATURATION:
        accent = _hex(h, _clamp(s * 0.55, *style.accent_s), _clamp(l, *style.accent_l))
    fg_s, fg_l = style.foreground
    return {
        'foreground': _hex(h, min(s, fg_s), fg_l),
        'background': _hex(h, min(s, _ADAPTIVE_TINT_SATURATION), style.background_l),
        'shadow': _hex(h, min(s, _ADAPTIVE_SHADOW_SATURATION), style.shadow_l),
        'accent': accent,
    }


def _adaptive_css_text(palette: dict[str, str]) -> bytes:
    """Serialize a palette in the same shape as the static theme files."""
    text = (
        ':root {\n'
        f"  --foreground: {palette['foreground']};\n"
        f"  --background: {palette['background']};\n"
        f"  --shadow: {palette['shadow']};\n"
        f"  --accent: {palette['accent']};\n"
        '}\n'
    )
    return text.encode('utf-8')


async def _adaptive_palette_cached(name: str) -> dict[str, str]:
    """Palette for an adaptive theme, recomputed when the cover or track changes.

    Follows the *current* track's art: the cover file is persistent, so a track
    without album art must yield the neutral fallback even while a previous
    album's cover file lingers on disk.
    """
    try:
        st = await aiofiles.os.stat(config.COVER_FILE)
        mtime: int | None = st.st_mtime_ns
    except OSError:
        mtime = None
    try:
        track = await read_track()
    except (OSError, sqlite3.Error):
        track = None
    key = (mtime, bool(track and track.get('CoverUrl')))
    cached = _adaptive_palette_cache.get(name)
    if cached is None or cached[0] != key:
        if key[0] is not None and key[1]:
            palette = await asyncio.to_thread(_adaptive_palette, name)
        else:
            palette = dict(_ADAPTIVE_FALLBACKS[name])
        cached = (key, palette)
        _adaptive_palette_cache[name] = cached
    return cached[1]


async def _adaptive_asset(name: str) -> tuple[str, bytes]:
    """`(etag, bytes)` for a generated adaptive theme."""
    palette = await _adaptive_palette_cached(name)
    content = _adaptive_css_text(palette)
    etag = f'"adp-{name[:-4]}-{hashlib.sha1(content).hexdigest()[:12]}"'
    return etag, content


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    yield
    if _broadcaster_task is not None:
        _broadcaster_task.cancel()
        with suppress(asyncio.CancelledError):
            await _broadcaster_task
    await listenbrainz.aclose()
    await db.aclose_all()


app = FastAPI(title='nowplaying', lifespan=lifespan)


async def _load_asset(path: Path) -> tuple[str, bytes] | None:
    """Return `(etag, content)` for `path`, or None if it doesn't exist."""

    try:
        st = await aiofiles.os.stat(path)
    except OSError:
        return None
    etag = f'"{st.st_mtime_ns:x}-{st.st_size:x}"'
    cached = _asset_cache.get(path)
    if cached is not None and cached[0] == etag:
        return cached
    async with aiofiles.open(path, 'rb') as f:
        content = await f.read()
    _asset_cache[path] = (etag, content)
    return etag, content


def _not_modified(request: Request, etag: str) -> bool:
    return request.headers.get('if-none-match') == etag


def _not_modified_response(etag: str) -> Response:
    return Response(status_code=304, headers={'ETag': etag, 'Cache-Control': 'no-cache'})


def _playerctl_cmd(action: str) -> list[str]:
    cmd = ['playerctl']
    if config.PLAYERCTL_PLAYER:
        cmd += ['--player', config.PLAYERCTL_PLAYER]
    return cmd + [action]


async def run_playerctl(action: str) -> None:
    if config.SOURCE == 'windows':
        await windows_control.control(action)
        return
    proc = await asyncio.create_subprocess_exec(
        *_playerctl_cmd(action),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode('utf-8', 'replace').strip() or 'playerctl failed')


async def _player_status() -> str | None:
    """Return the playback status string, or None if no player is active."""
    if config.SOURCE == 'windows':
        return await windows_control.get_status()
    try:
        proc = await asyncio.create_subprocess_exec(
            *_playerctl_cmd('status'),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return stdout.decode('utf-8', 'replace').strip()


async def read_track() -> dict | None:
    """Return the latest history row joined with artist/album/track, or None."""

    return await db.aget_current(config.DB_PATH)


@app.get('/')
async def index(request: Request) -> Response:
    loaded = await _load_asset(INDEX)
    if loaded is None:
        return HTMLResponse('<h1>Home page not found</h1>', status_code=404)
    etag, content = loaded
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return Response(
        content=content,
        media_type='text/html',
        headers={'Cache-Control': 'no-cache', 'ETag': etag},
    )


@app.get('/style.css')
async def css(request: Request) -> Response:
    loaded = await _load_asset(CSS)
    if loaded is None:
        return Response('/* stylesheet not found */', status_code=404, media_type='text/css')
    etag, content = loaded
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return Response(
        content=content,
        media_type='text/css',
        headers={'Cache-Control': 'no-cache', 'ETag': etag},
    )


@app.get('/themes/{filename}')
async def theme(filename: str, request: Request) -> Response:
    if (
        Path(filename).name != filename
        or not filename.endswith('.css')
        or filename.startswith('.')
        or '\\' in filename
    ):
        raise HTTPException(status_code=400, detail='Invalid theme filename')
    loaded = (
        await _adaptive_asset(filename)
        if filename in ADAPTIVE_THEMES
        else await _load_asset(THEMES_DIR / filename)
    )
    if loaded is None:
        return Response('/* theme not found */', status_code=404, media_type='text/css')
    etag, content = loaded
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return Response(
        content=content,
        media_type='text/css',
        headers={'Cache-Control': 'no-cache', 'ETag': etag},
    )


@app.get('/api/themes')
async def themes() -> list[dict[str, str | None]]:
    try:
        names = await aiofiles.os.listdir(THEMES_DIR)
    except OSError:
        return []
    result: list[dict[str, str | None]] = []
    for name in sorted(names):
        if not name.endswith('.css') or name.startswith('.'):
            continue
        if name in ADAPTIVE_THEMES:  # generated dynamically below
            continue
        try:
            async with aiofiles.open(THEMES_DIR / name, 'r', encoding='utf-8') as f:
                content = await f.read()
        except OSError:
            content = ''
        match = _ACCENT_RE.search(content)
        result.append({
            'name': name.removesuffix('.css'),
            'accent': match.group(1) if match else None,
        })
    for name in sorted(ADAPTIVE_THEMES):
        accent = (await _adaptive_palette_cached(name))['accent']
        result.append({'name': name.removesuffix('.css'), 'accent': accent})
    result.sort(key=lambda item: item['name'] or '')
    return result


@app.get('/cover.svg')
async def cover() -> Response:
    try:
        async with aiofiles.open(COVER_SVG, 'rb') as f:
            content = await f.read()
    except FileNotFoundError:
        return Response(status_code=404)
    return Response(
        content=content,
        media_type='image/svg+xml',
        headers={'Cache-Control': 'public, max-age=86400'},
    )


@app.get(config.COVER_URL)
async def cover_png() -> Response:
    path = Path(config.COVER_FILE)
    try:
        await aiofiles.os.stat(path)
    except OSError:
        return Response(status_code=404)

    return FileResponse(
        path,
        media_type='image/png',
        headers={'Cache-Control': 'public, max-age=604800, immutable'},
    )


@app.get('/favicon')
async def favicon() -> Response:
    content = '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><rect width="256" height="256" fill="none"/><line x1="48" y1="96" x2="48" y2="160" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="88" y1="32" x2="88" y2="224" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="128" y1="64" x2="128" y2="192" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="168" y1="96" x2="168" y2="160" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="208" y1="80" x2="208" y2="176" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/></svg>'
    return Response(
        content=content,
        media_type='image/svg+xml',
        headers={'Cache-Control': 'public, max-age=86400'},
    )


@app.get('/api/track')
async def track() -> Response:
    data = await read_track()
    if data is None:
        return PlainTextResponse('No track info yet', status_code=200)
    return Response(
        content=json.dumps(_payload(data)),
        media_type='application/json',
    )


@app.get('/api/track/{track_id}/similar')
async def track_similar(
    track_id: int,
    limit: int = SIMILAR_LIMIT_DEFAULT,
    algorithm: str | None = None,
) -> dict:
    """ListenBrainz similar recordings for one stored track.

    Seeded on the track's MusicBrainz recording id and served from the Labs
    `similar-recordings` datasets (session-based co-listening scores). Without
    an explicit `algorithm` the datasets are tried in order until one knows the
    seed, and a seed none of them knows is rescued through another recording of
    the same song; the response names whichever algorithm answered.
    """

    if algorithm is not None and algorithm not in listenbrainz.SIMILAR_ALGORITHMS:
        raise HTTPException(status_code=400, detail=f'Invalid algorithm: {algorithm}')
    track_row = await db.aget_track(config.DB_PATH, track_id)
    if track_row is None:
        raise HTTPException(status_code=404, detail='Unknown track') from None
    if not (mbid := track_row.get('MBID')):
        raise HTTPException(
            status_code=404, detail='Track has no MusicBrainz recording id'
        ) from None

    limit = max(1, min(limit, SIMILAR_LIMIT_MAX))
    try:
        rows = await listenbrainz.similar_recordings(
            mbid, algorithm=algorithm, limit=limit
        )
    except listenbrainz.ListenBrainzError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    return {
        'MBID': mbid,
        'Artist': track_row.get('Artist'),
        'Title': track_row.get('Title'),
        'Algorithm': rows[0]['algorithm'] if rows else algorithm,
        'RescuedFrom': rows[0].get('rescued_from') if rows else None,
        'Tracks': [
            {
                'Artist': row['artist'],
                'ArtistMBID': row['artist_mbid'],
                'Title': row['title'],
                'Album': row['album'],
                'MBID': row['recording_mbid'],
                'Score': row['score'],
                'Cover': row['cover_url'],
            }
            for row in rows
        ],
    }


@app.get('/api/track/{track_id}/similar-artists')
async def track_similar_artists(
    track_id: int,
    limit: int = SIMILAR_ARTIST_LIMIT_DEFAULT,
    mode: str = 'easy',
) -> dict:
    """ListenBrainz artists similar to the one behind one stored track.

    Seeded on the track's MusicBrainz recording id: `listenbrainz.similar_artists`
    resolves it to its artist and lists LB Radio's artist pool.
    """

    if mode not in listenbrainz.MODES:
        raise HTTPException(status_code=400, detail=f'Invalid mode: {mode}')
    track_row = await db.aget_track(config.DB_PATH, track_id)
    if track_row is None:
        raise HTTPException(status_code=404, detail='Unknown track') from None
    if not (mbid := track_row.get('MBID')):
        raise HTTPException(
            status_code=404, detail='Track has no MusicBrainz recording id'
        ) from None

    limit = max(1, min(limit, SIMILAR_LIMIT_MAX))
    try:
        rows = await listenbrainz.similar_artists(mbid, mode=mode, limit=limit)
    except listenbrainz.ListenBrainzError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None

    return {
        'MBID': mbid,
        'Artist': track_row.get('Artist'),
        'Title': track_row.get('Title'),
        'Mode': mode,
        'Artists': [
            {
                'Artist': row['artist'],
                'ArtistMBID': row['artist_mbid'],
                'Title': row['title'],
                'MBID': row['recording_mbid'],
                'Listens': row['listen_count'],
            }
            for row in rows
        ],
    }


def _served_cover(track: dict) -> str | None:
    """Map a stored raw cover URL to the served `/cover.png?v=` URL for the frontend.

    The version hash includes the track identity (artist|album) so a shared raw
    thumbnail URL across two albums still produces distinct URLs per album -
    the browser never serves one album's cached art for another.
    """
    if not (raw := track.get('CoverUrl')):
        return None
    extra = f"{track.get('Artist') or ''}|{track.get('Album') or ''}"
    return config.cover_url(raw, extra)


def _payload(track: dict) -> dict:
    return {
        'Artist': track.get('Artist', 'Unknown Artist'),
        'Album': track.get('Album'),
        'Title': track.get('Title', 'Unknown Title'),
        'Album Art': _served_cover(track),
        'Duration': track.get('Duration'),
        'TrackId': track.get('TrackId'),
        'MBID': track.get('MBID'),
        'Review': track.get('Review'),
        'Loved': bool(
            config.PREVIOUS_LOVE and track.get('FavouriteCount', 0) >= 3
        ),
        'PlayerctlAvailable': PLAYERCTL_AVAILABLE,
    }


_subscribers: set[asyncio.Queue[str]] = set()
_broadcaster_task: asyncio.Task | None = None
_broadcaster_lock = asyncio.Lock()

_last_track_event: str | None = None
_last_position_event: str | None = None
_last_status_event: str | None = None


def _publish(event: str) -> None:
    for q in tuple(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            _subscribers.discard(q)


_UNSET = object()


async def _broadcaster() -> None:
    global _last_track_event, _last_position_event, _last_status_event
    last_token: object = _UNSET
    last_position_key: tuple[int, str] | None = None
    last_status: object = _UNSET
    status_ts = 0.0
    while True:
        now = time.monotonic()
        try:
            token = await db.alatest_change_token(config.DB_PATH)
        except (OSError, sqlite3.Error):
            token = None
        if token != last_token:
            last_token = token
            try:
                data = await read_track()
            except (OSError, sqlite3.Error):
                data = None
            payload = (
                json.dumps(_payload(data))
                if data is not None
                else json.dumps({'error': 'No track info yet'})
            )
            _last_track_event = 'data: ' + payload + '\n\n'
            _publish(_last_track_event)
            last_position_key = None
        else:
            try:
                position = await db.aget_position(config.DB_PATH)
            except (OSError, sqlite3.Error):
                position = None
            if position is not None and position != last_position_key:
                last_position_key = position
                _last_position_event = (
                    'event: position\ndata: '
                    + json.dumps({'Position': position[0]})
                    + '\n\n'
                )
                _publish(_last_position_event)
        if now - status_ts >= 3.0:
            status_ts = now
            try:
                status = await _player_status()
            except (OSError, RuntimeError):
                status = None
            event = 'event: status\ndata: ' + json.dumps({'Status': status}) + '\n\n'
            _last_status_event = event
            if status != last_status:
                last_status = status
                _publish(event)
        await asyncio.sleep(0.5)


async def _ensure_broadcaster() -> None:
    global _broadcaster_task
    async with _broadcaster_lock:
        if _broadcaster_task is None or _broadcaster_task.done():
            _broadcaster_task = asyncio.create_task(_broadcaster())


@app.get('/api/track/stream')
async def track_stream() -> StreamingResponse:
    async def event_stream():
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)
        _subscribers.add(queue)
        await _ensure_broadcaster()
        try:
            for snapshot in (
                _last_track_event,
                _last_position_event,
                _last_status_event,
            ):
                if snapshot is not None:
                    yield snapshot
            while True:
                try:
                    yield await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ': ping\n\n'
        finally:
            _subscribers.discard(queue)

    return StreamingResponse(
        event_stream(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@app.post('/api/player/{action}')
async def player_control(action: str) -> dict:
    if action not in PLAYERCTL_ACTIONS:
        raise HTTPException(status_code=400, detail=f'Invalid action: {action}')
    if action == 'next':
        with suppress(Exception):
            current = await db.aget_current(config.DB_PATH)
            if current and current.get('TrackId'):
                await db.aincrement_skip(config.DB_PATH, current['TrackId'])
    elif action == 'previous':
        with suppress(Exception):
            current = await db.aget_current(config.DB_PATH)
            if (
                current
                and current.get('TrackId')
                and await _player_status() == 'Playing'
            ):
                await db.aincrement_favourite(config.DB_PATH, current['TrackId'])
    try:
        await run_playerctl(action)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail='playerctl not installed') from None
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    return {'status': 'success', 'action': action}


@app.get('/api/player/status')
async def player_status() -> dict:
    status = await _player_status()
    if status is None:
        raise HTTPException(status_code=404, detail='No active MPRIS player found') from None
    return {'status': status}


@app.get('/api/player/position')
async def player_position() -> dict:
    """Return the D-Bus-polled playback position in ms (see monitor poll loops)."""
    try:
        stored = await db.aget_position(config.DB_PATH)
    except (OSError, sqlite3.Error):
        raise HTTPException(status_code=500, detail='Position unavailable') from None
    if stored is None:
        raise HTTPException(status_code=404, detail='No position yet') from None
    return {'Position': stored[0]}


def main() -> None:
    """Serve the app on the configured host/port (config.HOST/PORT)."""

    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        timeout_graceful_shutdown=3,
    )


if __name__ == '__main__':
    main()
