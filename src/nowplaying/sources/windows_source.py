"""Windows track source using the Media Session API (SMTC) via winrt."""

import asyncio
from contextlib import suppress

from loguru import logger
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as SessionManager,
)
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)


class WindowsSource:
    """Emit track updates from Windows Media Session events.

    Event-driven: uses winrt `add_*_changed` callbacks bridged to asyncio
    via `run_coroutine_threadsafe`. Re-registers session-level events when
    the current session changes (e.g. user switches from Spotify to browser).
    """

    def __init__(self, monitor, loop: asyncio.AbstractEventLoop) -> None:
        self.monitor = monitor
        self.loop = loop
        self.manager: SessionManager | None = None
        self.session = None
        self._session_tokens: list[tuple] = []
        self._manager_token = None
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Request the session manager and run until cancelled or closed."""

        self._stop.clear()
        self.manager = await SessionManager.request_async()
        self._manager_token = self.manager.add_current_session_changed(
            self._on_current_session_changed
        )
        try:
            await self._refresh_session()
            await self._stop.wait()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        """Unregister all WinRT handlers; safe to call twice."""
        self._stop.set()
        for remove_fn, token in self._session_tokens:
            with suppress(Exception):
                remove_fn(token)
        self._session_tokens = []
        self.session = None
        if self.manager is not None and self._manager_token is not None:
            with suppress(Exception):
                self.manager.remove_current_session_changed(self._manager_token)
            self._manager_token = None

    @staticmethod
    def _log_bridge(fut) -> None:
        try:
            fut.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception('[winrt] bridged callback failed')

    def _bridge(self, coro) -> None:
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        except RuntimeError:
            logger.debug('[winrt] loop closed; dropping event')
            return
        fut.add_done_callback(self._log_bridge)

    def _on_current_session_changed(self, sender, args) -> None:
        self._bridge(self._refresh_session())

    @staticmethod
    def _is_playing(session) -> bool:
        """True when the session is actively playing (best-effort)."""
        try:
            info = session.get_playback_info()
        except Exception:
            return False
        return (
            info is not None
            and info.playback_status == PlaybackStatus.PLAYING
        )

    def _pick_session(self, current):
        """Choose which SMTC session to follow.

        Windows briefly promotes another app's (often paused) session to
        "current" while the active player transitions between tracks. Following
        it blindly writes a spurious track (wrong art) until the real player
        becomes current again. Prefer: the current session if it is playing,
        else the session we were already following if it is still playing,
        else the only playing session, else whatever Windows reports.
        """

        if current is not None and self._is_playing(current):
            return current
        if self.session is not None and self._is_playing(self.session):
            return self.session
        try:
            sessions = list(self.manager.get_sessions()) if self.manager else []
        except Exception:
            sessions = []
        playing = [s for s in sessions if self._is_playing(s)]
        if len(playing) == 1:
            return playing[0]
        return current

    async def _refresh_session(self) -> None:
        """Re-register session event handlers on the current session."""

        if self.session is not None:
            for remove_fn, token in self._session_tokens:
                with suppress(Exception):
                    remove_fn(token)
            self._session_tokens = []

        assert self.manager is not None
        self.session = self._pick_session(self.manager.get_current_session())
        if self.session is None:
            return

        self._session_tokens.append((
            self.session.remove_media_properties_changed,
            self.session.add_media_properties_changed(self._on_media_properties_changed),
        ))
        self._session_tokens.append((
            self.session.remove_timeline_properties_changed,
            self.session.add_timeline_properties_changed(self._on_timeline_properties_changed),
        ))

        await self._fetch_media_properties()

    def _on_media_properties_changed(self, sender, args) -> None:
        self._bridge(self._fetch_media_properties())

    async def _fetch_media_properties(self) -> None:
        if self.session is None:
            return
        try:
            props = await self.session.try_get_media_properties_async()
            timeline = self.session.get_timeline_properties()
        except Exception:
            logger.debug('[winrt] media properties not ready')
            return
        if not props:
            return
        duration_ms = 0
        if timeline is not None:
            duration_ms = int(timeline.end_time.total_seconds() * 1000)
        fields = {
            'Title': props.title or '',
            'Artist': props.artist or '',
            'Album': props.album_title or '',
            'Duration': str(duration_ms),
        }
        self.monitor.submit(self.monitor.apply, fields)

    def _on_timeline_properties_changed(self, sender, args) -> None:
        self._bridge(self._fetch_timeline())

    async def _fetch_timeline(self) -> None:
        if self.session is None:
            return
        try:
            timeline = self.session.get_timeline_properties()
        except Exception:
            logger.debug('[winrt] timeline not ready')
            return
        if timeline is None:
            return
        position_ms = int(timeline.position.total_seconds() * 1000)
        self.monitor.submit(self.monitor.set_position, position_ms)
