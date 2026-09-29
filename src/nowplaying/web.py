#!/usr/bin/env python3

import asyncio
import json
import re
import shutil
import sqlite3
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

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

from nowplaying import config, db

WEB_DIR = Path(__file__).resolve().parent.parent.parent / 'web'
INDEX = WEB_DIR / 'index.html'
CSS = WEB_DIR / 'style.css'
COVER_SVG = WEB_DIR / 'cover.svg'
THEMES_DIR = WEB_DIR / 'themes'

PLAYERCTL_ACTIONS = frozenset({'play-pause', 'next', 'previous'})

if config.SOURCE == 'windows':
    from nowplaying import windows_control
    PLAYERCTL_AVAILABLE = True
else:
    PLAYERCTL_AVAILABLE = shutil.which('playerctl') is not None

_ACCENT_RE = re.compile(r'--accent\s*:\s*(#[0-9a-fA-F]{3,8})')

_asset_cache: dict[Path, tuple[str, bytes]] = {}


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    if _broadcaster_task is not None:
        _broadcaster_task.cancel()
        with suppress(asyncio.CancelledError):
            await _broadcaster_task
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
    loaded = await _load_asset(THEMES_DIR / filename)
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
    result = []
    for name in sorted(names):
        if not name.endswith('.css') or name.startswith('.'):
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


def _served_cover(raw: str | None) -> str | None:
    """Map a stored raw cover URL to the served `/cover.png?v=` URL for the frontend."""

    return config.cover_url(raw) if raw else None


def _payload(track: dict) -> dict:
    return {
        'Artist': track.get('Artist', 'Unknown Artist'),
        'Album': track.get('Album'),
        'Title': track.get('Title', 'Unknown Title'),
        'Album Art': _served_cover(track.get('CoverUrl')),
        'Duration': track.get('Duration'),
        'TrackId': track.get('TrackId'),
        'Review': track.get('Review'),
        'Description': track.get('Description'),
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
    last_id: object = _UNSET
    last_position_key: tuple[int, str] | None = None
    last_status: object = _UNSET
    status_ts = 0.0
    while True:
        now = time.monotonic()
        try:
            hist_id = await db.alatest_history_id(config.DB_PATH)
        except (OSError, sqlite3.Error):
            hist_id = None
        if hist_id != last_id:
            last_id = hist_id
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

    uvicorn.run(app, host=config.HOST, port=config.PORT)


if __name__ == '__main__':
    main()
