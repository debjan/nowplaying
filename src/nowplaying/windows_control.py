"""Windows Media Session control via winrt (SMTC).

Shared by the monitor (auto-skip) and the web server (playback control).
All errors are caught and logged, never raised.
"""

import asyncio

from loguru import logger
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)

_session_manager: SessionManager | None = None
_session_lock = asyncio.Lock()


async def _get_session():
    """Return the current media session, or None if no session is active."""

    global _session_manager
    async with _session_lock:
        if _session_manager is None:
            _session_manager = await SessionManager.request_async()
        manager = _session_manager
    try:
        return manager.get_current_session()
    except Exception:
        logger.debug('[windows_control] get_current_session failed')
        return None


async def control(action: str) -> None:
    """Control the current media session: play-pause, next, or previous."""

    session = await _get_session()
    if session is None:
        logger.debug('[windows_control] no active session')
        return
    try:
        if action == 'play-pause':
            await session.try_toggle_play_pause_async()
        elif action == 'next':
            await session.try_skip_next_async()
        elif action == 'previous':
            await session.try_skip_previous_async()
    except Exception as e:
        logger.debug(f'[windows_control] control failed: {e}')


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
