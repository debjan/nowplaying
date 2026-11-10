"""Windows Media Session control via winrt (SMTC).

Shared by the monitor (auto-skip) and the web server (playback control).
All errors are caught and logged, never raised.
"""

import asyncio
import time

from loguru import logger
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)

_session_manager: SessionManager | None = None
_session_manager_ts = 0.0
_session_lock = asyncio.Lock()
# A `SessionManager` is effectively a snapshot: Windows does not refresh the
# GetCurrentSession()/GetSessions() results of a long-lived instance (see
# microsoft/cppwinrt#1310), so one created while nothing was playing keeps
# reporting no sessions forever. Re-request it (briefly cached so one button
# press doesn't request it twice).
_MANAGER_TTL_SECONDS = 1.0
# AUMID of the session the dashboard last controlled. Windows stops reporting a
# "current" session when an app stops at the start of a playlist (or is
# backgrounded), but the session is still listed by `get_sessions()`: keeping
# the last one lets the dashboard's play button still resume playback.
_last_session_aumid: str | None = None


def _aumid(session) -> str | None:
    try:
        return session.source_app_user_model_id
    except Exception:
        return None


def _remember(session):
    global _last_session_aumid
    if aumid := _aumid(session):
        _last_session_aumid = aumid
    return session


async def _manager() -> SessionManager:
    """Return a fresh session manager (cached for `_MANAGER_TTL_SECONDS`)."""

    global _session_manager, _session_manager_ts
    async with _session_lock:
        now = time.monotonic()
        if (
            _session_manager is None
            or now - _session_manager_ts >= _MANAGER_TTL_SECONDS
        ):
            _session_manager = await SessionManager.request_async()
            _session_manager_ts = now
        return _session_manager


async def _get_session():
    """Return the media session to control: current, else the last one used.

    `get_current_session()` goes `None` when nothing is actively playing (e.g.
    after `previous` at the first playlist entry), even though the app's
    session still exists and can be resumed. Fall back to the session this
    process last controlled, then to the only session that is left.
    """

    manager = await _manager()
    try:
        current = manager.get_current_session()
    except Exception:
        logger.debug('[windows_control] get_current_session failed')
        current = None
    if current is not None:
        return _remember(current)
    try:
        sessions = list(manager.get_sessions())
    except Exception:
        logger.debug('[windows_control] get_sessions failed')
        return None
    if _last_session_aumid is not None:
        for session in sessions:
            if _aumid(session) == _last_session_aumid:
                return session
    if len(sessions) == 1:
        return _remember(sessions[0])
    return None


async def control(action: str) -> None:
    """Control the current media session: play-pause, pause, next, or previous."""

    session = await _get_session()
    if session is None:
        logger.debug('[windows_control] no active session')
        return
    try:
        if action == 'play-pause':
            await session.try_toggle_play_pause_async()
        elif action == 'pause':
            await session.try_pause_async()
        elif action == 'next':
            await session.try_skip_next_async()
        elif action == 'previous':
            await session.try_skip_previous_async()
    except Exception as e:
        logger.debug(f'[windows_control] control failed: {e}')


async def get_position() -> int | None:
    """Return the current playback position in ms, or None if unavailable."""

    session = await _get_session()
    if session is None:
        return None
    try:
        position = session.get_timeline_properties().position
    except Exception:
        return None
    try:
        return int(position.total_seconds() * 1000)
    except (AttributeError, TypeError):
        return None


async def seek_start() -> bool:
    """Move the current session back to the start of the current track."""
    session = await _get_session()
    if session is None:
        logger.debug('[windows_control] no active session')
        return False
    try:
        return bool(await session.try_change_playback_position_async(0))
    except Exception as e:
        logger.debug(f'[windows_control] seek failed: {e}')
        return False


async def get_status() -> str | None:
    """Return 'Playing', 'Paused', 'Stopped', or None if no session."""

    session = await _get_session()
    if session is None:
        return None
    try:
        info = session.get_playback_info()
        status = info.playback_status
    except Exception:
        return None
    # 4=Playing, 5=Paused; anything else that indicates an active
    # session maps to Stopped, unknown values map to None.
    if status == 4:
        return 'Playing'
    if status == 5:
        return 'Paused'
    if status in (0, 1, 2, 3, 6, 7):
        return 'Stopped'
    return None
