#!/usr/bin/env python3

import asyncio
import json
import re
import sqlite3
import time
from contextlib import suppress
from pathlib import Path

import aiofiles
import aiofiles.os
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

from nowplaying import config, db

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
INDEX = ROOT_DIR / 'static' / 'index.html'
CSS = ROOT_DIR / 'static' / 'style.css'
COVER_SVG = ROOT_DIR / 'static' / 'cover.svg'
THEMES_DIR = ROOT_DIR / 'static' / 'themes'

PLAYERCTL_ACTIONS = frozenset({'play-pause', 'next', 'previous'})

_ACCENT_RE = re.compile(r'--accent\s*:\s*(#[0-9a-fA-F]{3,8})')

app = FastAPI(title='nowplaying')


def _playerctl_cmd(action: str) -> list[str]:
    cmd = ['playerctl']
    if config.PLAYERCTL_PLAYER:
        cmd += ['--player', config.PLAYERCTL_PLAYER]
    return cmd + [action]


async def run_playerctl(action: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        *_playerctl_cmd(action),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode('utf-8', 'replace').strip() or 'playerctl failed')


async def _player_status() -> str | None:
    """Return the playerctl status string, or None if no MPRIS player is active."""

    proc = await asyncio.create_subprocess_exec(
        *_playerctl_cmd('status'),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return stdout.decode('utf-8', 'replace').strip()


async def read_track() -> dict | None:
    """Return the latest history row joined with artist/album/track, or None."""

    return await db.aget_current(config.DB_PATH)


@app.get('/')
async def index() -> HTMLResponse:
    try:
        async with aiofiles.open(INDEX, 'r', encoding='utf-8') as f:
            content = await f.read()
        return HTMLResponse(content, headers={'Cache-Control': 'no-store'})
    except FileNotFoundError:
        return HTMLResponse('<h1>Home page not found</h1>', status_code=404)


@app.get('/style.css')
async def css() -> Response:
    try:
        async with aiofiles.open(CSS, 'r', encoding='utf-8') as f:
            content = await f.read()
        return Response(content=content, media_type='text/css', headers={'Cache-Control': 'no-store'})
    except FileNotFoundError:
        return Response('/* stylesheet not found */', status_code=404, media_type='text/css')


@app.get('/themes/{filename}')
async def theme(filename: str) -> Response:
    if '/' in filename or filename.startswith('.'):
        raise HTTPException(status_code=400, detail='Invalid theme filename')
    path = THEMES_DIR / filename
    try:
        async with aiofiles.open(path, 'r', encoding='utf-8') as f:
            content = await f.read()
        return Response(content=content, media_type='text/css', headers={'Cache-Control': 'no-store'})
    except FileNotFoundError:
        return Response('/* theme not found */', status_code=404, media_type='text/css')


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
        return Response(content=content, media_type='image/svg+xml', headers={'Cache-Control': 'no-store'})
    except FileNotFoundError:
        return Response(status_code=404)


@app.get(config.COVER_URL)
async def cover_png() -> Response:
    try:
        async with aiofiles.open(Path(config.COVER_FILE), 'rb') as f:
            content = await f.read()
        return Response(content=content, media_type='image/png', headers={'Cache-Control': 'no-store'})
    except FileNotFoundError:
        return Response(status_code=404)


@app.get('/favicon')
async def favicon() -> Response:
    try:
        content = '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" viewBox="0 0 256 256"><rect width="256" height="256" fill="none"/><line x1="48" y1="96" x2="48" y2="160" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="88" y1="32" x2="88" y2="224" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="128" y1="64" x2="128" y2="192" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="168" y1="96" x2="168" y2="160" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/><line x1="208" y1="80" x2="208" y2="176" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" stroke-width="24"/></svg>'
        return Response(content=content, media_type='image/svg+xml', headers={'Cache-Control': 'no-store'})
    except FileNotFoundError:
        return Response(status_code=404)


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
        'Loved': bool(
            config.PREVIOUS_LOVE and track.get('FavouriteCount', 0) >= 3
        ),
    }


@app.get('/api/track/stream')
async def track_stream() -> StreamingResponse:
    async def event_stream():
        last: int | None = None
        first = True
        last_ping = 0.0
        while True:
            try:
                key = await db.alatest_history_id(config.DB_PATH)
            except (OSError, sqlite3.Error):
                key = None
            now = time.monotonic()
            if first or key != last:
                first = False
                last = key
                try:
                    data = await read_track()
                except (OSError, sqlite3.Error):
                    data = None
                if data is None:
                    payload = json.dumps({'error': 'No track info yet'})
                else:
                    payload = json.dumps(_payload(data))
                yield 'data: ' + payload + '\n\n'
                last_ping = now
            elif now - last_ping >= 15:
                yield ': ping\n\n'
                last_ping = now
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type='text/event-stream')


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


if __name__ == '__main__':
    uvicorn.run(app, host=config.HOST, port=config.PORT)
