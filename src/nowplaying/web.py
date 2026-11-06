import asyncio
import colorsys
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import NamedTuple

import aiofiles
import aiofiles.os
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    StreamingResponse,
)
from loguru import logger
from PIL import Image

from nowplaying import config, db, listenbrainz

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
WEB_DIR = ROOT_DIR / 'web'
INDEX = WEB_DIR / 'index.html'
CSS = WEB_DIR / 'style.css'
COVER_SVG = WEB_DIR / 'cover.svg'
THEMES_DIR = WEB_DIR / 'themes'
PWA_DIR = WEB_DIR / 'PWA'
MANIFEST = PWA_DIR / 'manifest.webmanifest'
SW = PWA_DIR / 'sw.js'
ICON_FILES = {192: 'icon-192.png', 512: 'icon-512.png'}
# Manifest screenshot (narrow/portrait) for the richer install sheet.
SCREENSHOT = ROOT_DIR / 'img' / 'screenshot.png'

PLAYERCTL_ACTIONS = frozenset({'play-pause', 'next', 'previous', 'pause'})

# ListenBrainz "similar" dialogs: how many rows to return by default/max.
SIMILAR_LIMIT_DEFAULT = 12
SIMILAR_LIMIT_MAX = 50

if config.SOURCE == 'windows':
    from nowplaying import windows_control
    PLAYERCTL_AVAILABLE = True
else:
    PLAYERCTL_AVAILABLE = shutil.which('playerctl') is not None

# Dynamic themes: their palettes are derived from the current album cover's
# average color (see `_adaptive_palette`) instead of a static CSS file.
ADAPTIVE_THEMES = frozenset({'adaptive-light.css', 'adaptive-dark.css'})

THEME = ''
if config.THEME:
    if (
        config.THEME in {name.removesuffix('.css') for name in ADAPTIVE_THEMES}
        or (
            re.fullmatch(r'[a-z0-9-]+', config.THEME) is not None
            and (THEMES_DIR / f'{config.THEME}.css').is_file()
        )
    ):
        THEME = config.THEME
    else:
        logger.warning(f'[web] unknown THEME {config.THEME!r}; using the adaptive theme')

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
    if _sleep_task is not None:
        _sleep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _sleep_task
    if _broadcaster_task is not None:
        _broadcaster_task.cancel()
        with suppress(asyncio.CancelledError):
            await _broadcaster_task
    for feedback_task in tuple(_feedback_tasks):
        feedback_task.cancel()
        with suppress(asyncio.CancelledError):
            await feedback_task
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


async def require_trusted(request: Request) -> None:
    """Reject state-changing requests that did not come from the dashboard.

    Browsers label every request with `Sec-Fetch-Site`; a page on another
    origin can fire a simple cross-site form POST at us without a preflight,
    so anything not `same-origin`/`none` is refused. Requests without the
    header (older browsers, curl, scripts on the LAN) fall back to the
    optional shared secret in `NOWPLAYING_TOKEN`; leaving it empty keeps a
    fresh install working.
    """

    site = request.headers.get('sec-fetch-site')
    if site is not None:
        if site not in ('same-origin', 'none'):
            raise HTTPException(status_code=403, detail='Cross-site request blocked')
        return
    if not config.TOKEN:
        return
    supplied = request.headers.get('x-nowplaying-token', '')
    if not secrets.compare_digest(supplied, config.TOKEN):
        raise HTTPException(
            status_code=403, detail='Missing or invalid X-Nowplaying-Token'
        )


_THEME_META = b'<meta name="nowplaying-theme" content="">'


def _themed_index(etag: str, content: bytes) -> tuple[str, bytes]:
    """Inject the configured theme into the served HTML; cache-bust the ETag."""
    if not THEME:
        return etag, content
    meta = f'<meta name="nowplaying-theme" content="{THEME}">'.encode()
    return f'{etag[:-1]}-{THEME}"', content.replace(_THEME_META, meta)


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
    etag, content = _themed_index(etag, content)
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


@app.get('/manifest.webmanifest')
async def web_manifest(request: Request) -> Response:
    """Serve the PWA web app manifest."""
    loaded = await _load_asset(MANIFEST)
    if loaded is None:
        return Response(status_code=404, content='Manifest not found')
    etag, content = loaded
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return Response(
        content=content,
        media_type='application/manifest+json',
        headers={'Cache-Control': 'public, max-age=3600', 'ETag': etag},
    )


@app.get('/sw.js')
async def service_worker(request: Request) -> Response:
    """Serve the PWA service worker; no-cache so a new worker deploys at once."""
    loaded = await _load_asset(SW)
    if loaded is None:
        return Response(status_code=404, content='Service worker not found')
    etag, content = loaded
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return Response(
        content=content,
        media_type='application/javascript',
        headers={'Cache-Control': 'no-cache', 'ETag': etag},
    )


@app.get('/icon/{size:int}.png')
async def icon(size: int, request: Request) -> Response:
    """Serve a PWA install icon (192/512 - keys of ICON_FILES)."""
    filename = ICON_FILES.get(size)
    if filename is None:
        return Response(status_code=404, content='Icon not found')
    loaded = await _load_asset(PWA_DIR / filename)
    if loaded is None:
        return Response(status_code=404, content='Icon not found')
    etag, content = loaded
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return Response(
        content=content,
        media_type='image/png',
        headers={'Cache-Control': 'public, max-age=86400', 'ETag': etag},
    )


@app.get('/screenshot.png')
async def screenshot(request: Request) -> Response:
    """Serve the manifest screenshot (narrow install sheet preview)."""
    loaded = await _load_asset(SCREENSHOT)
    if loaded is None:
        return Response(status_code=404, content='Screenshot not found')
    etag, content = loaded
    if _not_modified(request, etag):
        return _not_modified_response(etag)
    return Response(
        content=content,
        media_type='image/png',
        headers={'Cache-Control': 'public, max-age=86400', 'ETag': etag},
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


@app.get('/cover.svg')
@app.get('/favicon')
async def cover() -> Response:
    try:
        async with aiofiles.open(COVER_SVG) as f:
            content = await f.read()
    except FileNotFoundError:
        return Response(status_code=404)
    return Response(
        content=content.encode(),
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
    feedback = track.get('Feedback', 0)
    return {
        'Artist': track.get('Artist', 'Unknown Artist'),
        'Album': track.get('Album'),
        'Title': track.get('Title', 'Unknown Title'),
        'Album Art': _served_cover(track),
        'Duration': track.get('Duration'),
        'TrackId': track.get('TrackId'),
        'MBID': track.get('MBID'),
        'Review': track.get('Review'),
        'Biography': track.get('Biography'),
        'Loved': feedback == 1,
        'Hated': feedback == -1,
        'PlayerctlAvailable': PLAYERCTL_AVAILABLE,
    }


_subscribers: set[asyncio.Queue[str]] = set()
_broadcaster_task: asyncio.Task | None = None
_broadcaster_lock = asyncio.Lock()

_last_track_event: str | None = None
_last_position_event: str | None = None
_last_status_event: str | None = None
_last_sleep_event: str | None = None

# Sleep timer state. It lives in the web process (not in the page) so a
# backgrounded phone, a reload or a second client all see the same timer and
# the pause happens even when no tab is awake. `Mode` is the armed mode
# (`15`/`30`/`60`/`end`), `EndsAt` an epoch-ms deadline (None for end-of-song).
_sleep_state: dict[str, object] = {'Mode': None, 'EndsAt': None}
_sleep_task: asyncio.Task[None] | None = None


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
        if not _subscribers:
            # No browser is connected: query nothing and spawn no `playerctl`.
            # A new client gets the cached snapshots on connect and the next
            # iteration refreshes them, so idle time costs one wake-up a second.
            await asyncio.sleep(1.0)
            continue
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
                _last_sleep_event,
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


@app.post('/api/player/{action}', dependencies=[Depends(require_trusted)])
async def player_control(action: str) -> dict:
    if action not in PLAYERCTL_ACTIONS:
        raise HTTPException(status_code=400, detail=f'Invalid action: {action}')
    try:
        await run_playerctl(action)
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail='playerctl not installed') from None
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    return {'status': 'success', 'action': action}


# --- Love / hate feedback ---------------------------------------------------

# The verdicts the dashboard's buttons send (1 = love, -1 = hate). Pressing the
# active thumb again withdraws it (score 0), which is also a valid ListenBrainz
# feedback value.
FEEDBACK_SCORES = (listenbrainz.FEEDBACK_LOVE, listenbrainz.FEEDBACK_HATE)


# Strong refs to in-flight submissions (a bare task can be garbage collected).
_feedback_tasks: set[asyncio.Task[None]] = set()


def _submit_feedback(mbid: str | None, score: int) -> None:
    """Ship a vote to ListenBrainz in the background; never block the request.

    `score` 0 withdraws a previous verdict, matching the dashboard's toggle.
    """

    if not config.LISTENBRAINZ_SUBMIT:
        return
    if not mbid:
        # ListenBrainz feedback is keyed on the recording, so an unresolved
        # track stays local-only (the thumbs still reflect the DB verdict).
        logger.debug('[listenbrainz] no recording MBID; feedback stored locally only')
        return

    async def run() -> None:
        try:
            await listenbrainz.submit_feedback(mbid, score)
        except listenbrainz.ListenBrainzError as exc:
            logger.debug(f'[listenbrainz] feedback not submitted: {exc}')

    task = asyncio.create_task(run())
    _feedback_tasks.add(task)
    task.add_done_callback(_feedback_tasks.discard)


@app.post('/api/feedback/{score}', dependencies=[Depends(require_trusted)])
async def feedback(score: int) -> dict:
    """Love (`1`) or hate (`-1`) the current track; the active thumb toggles off.

    The verdict is one row in `feedback` (1 = loved, -1 = hated, 0 = withdrawn);
    that is what the thumbs render from and what the monitor's auto-skip reads.
    A love replaces a hate and vice versa; a hate on a fresh vote also skips to
    the next track. Pressing the active thumb again withdraws the verdict: the
    score is cleared, nothing is skipped, and ListenBrainz gets score 0.
    """

    if score not in FEEDBACK_SCORES:
        raise HTTPException(status_code=400, detail=f'Invalid feedback: {score}')
    current = await db.aget_current(config.DB_PATH)
    if current is None or not current.get('TrackId'):
        raise HTTPException(status_code=404, detail='No track playing') from None
    track_id = current['TrackId']
    withdrawing = current.get('Feedback') == score
    try:
        await db.aset_feedback(config.DB_PATH, track_id, 0 if withdrawing else score)
    except (OSError, sqlite3.Error) as exc:
        # A vote that could not be stored must not skip the track or leave a
        # remote vote behind: report the failure instead of a false success.
        logger.warning(f'[feedback] could not store the verdict: {exc}')
        raise HTTPException(
            status_code=500, detail='Could not store feedback'
        ) from None

    if score == listenbrainz.FEEDBACK_HATE and not withdrawing:
        try:
            await run_playerctl('next')
        except (FileNotFoundError, RuntimeError) as exc:
            logger.debug(f'[feedback] skip failed: {exc}')

    _submit_feedback(current.get('MBID'), 0 if withdrawing else score)
    # Re-read so the response is the stored state, not the optimistic guess.
    try:
        updated = await db.aget_current(config.DB_PATH)
    except (OSError, sqlite3.Error) as exc:
        logger.debug(f'[feedback] could not re-read the track: {exc}')
        updated = None
    return _payload(updated or current)


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


# --- Sleep timer ------------------------------------------------------------

SLEEP_MINUTES = {'15': 15, '30': 30, '60': 60}
SLEEP_MODES = frozenset({*SLEEP_MINUTES, 'end'})

_SLEEP_POLL_SECONDS = 1.0
# A stored position jumping back by more than this means the track restarted
# (repeat-one writes no new history row). A large manual seek backwards can
# also trip it - the same accepted trade-off as the monitor's play gate.
_SLEEP_RESTART_JUMP_MS = 5000


def _sleep_payload() -> dict[str, object]:
    return dict(_sleep_state)


def _publish_sleep() -> None:
    global _last_sleep_event
    _last_sleep_event = 'event: sleep\ndata: ' + json.dumps(_sleep_payload()) + '\n\n'
    _publish(_last_sleep_event)


def _set_sleep_state(mode: str | None, ends_at: int | None) -> None:
    _sleep_state.update({'Mode': mode, 'EndsAt': ends_at})


def _disarm_sleep() -> None:
    global _sleep_task
    _sleep_task = None
    _set_sleep_state(None, None)
    _publish_sleep()


async def _current_track_id() -> int | None:
    try:
        track = await read_track()
    except (OSError, sqlite3.Error):
        return None
    track_id = track.get('TrackId') if track else None
    return track_id if isinstance(track_id, int) else None


async def _reached_track_end(
    track: dict | None, previous_position: int | None
) -> tuple[bool, int | None]:
    """Whether the stored position reports the current track at its end.

    The position is polled every ~10s, so the near-end check alone rarely
    catches a player that repeats one track (which writes no new `history`
    row); a large backwards jump also counts as the track restarting. Returns
    the current position so the caller can track the previous one.
    """
    if not (duration := track.get('Duration') if track else None):
        return False, None
    try:
        position = await db.aget_position(config.DB_PATH)
    except (OSError, sqlite3.Error):
        return False, None
    if position is None:
        return False, None
    current = position[0]
    ended = current >= int(duration) - 500 or (
        previous_position is not None
        and previous_position - current > _SLEEP_RESTART_JUMP_MS
    )
    return ended, current


async def _wait_for_track_end() -> None:
    """Block until the current track ends (a new track starts, or repeat-one restarts it)."""
    try:
        baseline_history, _, _ = await db.alatest_change_token(config.DB_PATH)
    except (OSError, sqlite3.Error):
        baseline_history = None
    baseline_track = await _current_track_id()
    previous_position: int | None = None
    while True:
        await asyncio.sleep(_SLEEP_POLL_SECONDS)
        try:
            history, _, _ = await db.alatest_change_token(config.DB_PATH)
        except (OSError, sqlite3.Error):
            continue
        if history is not None and history != baseline_history:
            return
        track: dict | None = None
        with suppress(OSError, sqlite3.Error):
            track = await read_track()
        if baseline_track is not None and track and track.get('TrackId') != baseline_track:
            return
        ended, position = await _reached_track_end(track, previous_position)
        if ended:
            return
        if position is not None:
            previous_position = position


async def _sleep_loop(mode: str) -> None:
    try:
        if mode == 'end':
            await _wait_for_track_end()
        else:
            await asyncio.sleep(SLEEP_MINUTES[mode] * 60)
        _disarm_sleep()
        try:
            await run_playerctl('pause')
        except FileNotFoundError:
            logger.warning('[sleep] playerctl not installed; cannot pause playback')
        except (OSError, RuntimeError) as exc:
            logger.debug(f'[sleep] pause failed: {exc}')
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # never leave a stale armed state behind
        logger.debug(f'[sleep] watcher failed: {exc}')
        _disarm_sleep()


async def _arm_sleep(mode: str) -> dict[str, object]:
    """Arm (or replace) the timer; `off` disarms it. Returns the new state."""
    global _sleep_task
    previous, _sleep_task = _sleep_task, None
    if previous is not None:
        previous.cancel()
        with suppress(asyncio.CancelledError):
            await previous
    if mode == 'off':
        _set_sleep_state(None, None)
    else:
        minutes = SLEEP_MINUTES.get(mode)
        ends_at = int(time.time() * 1000) + minutes * 60_000 if minutes else None
        _set_sleep_state(mode, ends_at)
        _sleep_task = asyncio.create_task(_sleep_loop(mode))
    _publish_sleep()
    return _sleep_payload()


@app.get('/api/sleep')
async def sleep_state() -> dict[str, object]:
    """Current sleep timer state (`Mode`/`EndsAt`; both None when disarmed)."""
    return _sleep_payload()


@app.post('/api/sleep/{mode}', dependencies=[Depends(require_trusted)])
async def sleep(mode: str) -> dict[str, object]:
    """Arm (`15`/`30`/`60`/`end`) or cancel (`off`) the sleep timer."""
    if mode not in SLEEP_MODES and mode != 'off':
        raise HTTPException(status_code=400, detail=f'Invalid sleep mode: {mode}')
    return await _arm_sleep(mode)


# --- Quick settings ----------------------------------------------------------

# Behaviour toggles + UI theme the dashboard manages. The `.env` file is the
# ground truth for them (see `_persist_env`), so anything written here survives a
# restart and is also applied to the running config right away.
SETTING_FLAGS = ('AUTOSKIP', 'LISTENBRAINZ_SUBMIT')
SETTING_KEYS = frozenset({*SETTING_FLAGS, 'THEME'})

ENV_PATH = ROOT_DIR / '.env'


def _valid_theme(name: str) -> bool:
    """Whether `name` is an adaptive theme or a static CSS file in web/themes."""
    stripped = name.strip().removesuffix('.css')
    if stripped in {n.removesuffix('.css') for n in ADAPTIVE_THEMES}:
        return True
    return (
        re.fullmatch(r'[a-z0-9-]+', stripped) is not None
        and (THEMES_DIR / f'{stripped}.css').is_file()
    )


def _list_theme_names() -> list[str]:
    """Names of the themes available in web/themes (file stem; e.g. `tokio-night`)."""
    try:
        return sorted(
            path.stem
            for path in THEMES_DIR.iterdir()
            if path.is_file() and path.suffix == '.css'
        )
    except OSError:
        return []


def _persist_env(pairs: list[tuple[str, str]]) -> None:
    """Write `KEY=value` pairs into the project-root `.env` - the ground truth.

    Existing assignments are replaced in place, new keys are appended; comments
    and unrelated keys are left untouched. The file is swapped atomically
    (temp file + `os.replace`) so the monitor's 5s poll never reads a
    truncated file and briefly falls back to the default settings.
    """
    text = ''
    if ENV_PATH.exists():
        text = ENV_PATH.read_text(encoding='utf-8')
    for key, value in pairs:
        pattern = re.compile(rf'(?m)^(\s*(?:export\s+)?){re.escape(key)}=.*$')
        if pattern.search(text):
            text = pattern.sub(fr'\g<1>{key}={value}', text, count=1)
        elif text.strip():
            text = f'{text.rstrip()}\n{key}={value}\n'
        else:
            text = f'{key}={value}\n'
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=ENV_PATH.parent, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, ENV_PATH)
    finally:
        with suppress(OSError):
            os.remove(tmp)


def _settings_payload() -> dict[str, object]:
    """Current quick settings; `LISTENBRAINZ_TOKEN` is read-only (token present?).

    A token is required for submission, so the dashboard disables that switch
    while it is absent; the token value itself is never sent to the browser.
    """
    return {
        'AUTOSKIP': config.AUTOSKIP,
        'LISTENBRAINZ_SUBMIT': config.LISTENBRAINZ_SUBMIT,
        'THEME': THEME,
        'LISTENBRAINZ_TOKEN': bool(config.LISTENBRAINZ_TOKEN),
    }


@app.get('/api/settings')
async def get_settings() -> dict[str, object]:
    """Quick settings snapshot (`Settings`) plus the available themes (`Themes`)."""

    themes = await asyncio.to_thread(_list_theme_names)
    return {'Settings': _settings_payload(), 'Themes': themes}


@app.post('/api/settings', dependencies=[Depends(require_trusted)])
async def update_settings(payload: dict[str, object]) -> dict[str, object]:
    """Update quick settings (`AUTOSKIP` / `LISTENBRAINZ_SUBMIT` / `THEME`).

    Every change is persisted to the project-root `.env` (the ground truth) and
    applied to the running config immediately, so the flags take effect without a
    restart wherever the process reads them at call time.
    """

    global THEME

    updates: list[tuple[str, str]] = []
    for key, value in payload.items():
        name = str(key)
        if name not in SETTING_KEYS:
            raise HTTPException(status_code=400, detail=f'Unknown setting: {name}') from None
        if name == 'THEME':
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail='THEME must be a string') from None
            theme_name = value.strip().removesuffix('.css')
            if not _valid_theme(theme_name):
                raise HTTPException(status_code=400, detail=f'Unknown theme: {value}') from None
            if theme_name != THEME:
                THEME = theme_name
                config.THEME = theme_name
                updates.append(('THEME', theme_name))
        elif name in SETTING_FLAGS:
            if not isinstance(value, bool):
                raise HTTPException(
                    status_code=400, detail=f'{name} must be a boolean'
                ) from None
            if value != getattr(config, name):
                setattr(config, name, value)
                updates.append((name, '1' if value else '0'))

    if updates:
        await asyncio.to_thread(_persist_env, updates)

    return {
        'Settings': _settings_payload(),
        'Themes': await asyncio.to_thread(_list_theme_names),
    }


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
