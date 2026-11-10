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

# A `SessionManager` is effectively a snapshot: Windows does not refresh the
# GetCurrentSession()/GetSessions() results of a long-lived instance (see
# microsoft/cppwinrt#1310), so a manager requested while nothing was playing
# never reports the app you start later. Re-request it on a timer instead of
# relying on `CurrentSessionChanged` (which does not fire reliably either).
_SESSION_SCAN_SECONDS = 3.0


class WindowsSource:
    """Emit track updates from Windows Media Session events.

    Event-driven: uses winrt `add_*_changed` callbacks bridged to asyncio
    via `run_coroutine_threadsafe`, and re-scans the session list every few
    seconds (with a fresh manager) to pick up apps started or switched after
    the monitor launched.
    """

    def __init__(self, monitor, loop: asyncio.AbstractEventLoop) -> None:
        self.monitor = monitor
        self.loop = loop
        self.manager: SessionManager | None = None
        self.session = None
        self._session_tokens: list[tuple] = []
        self._stop = asyncio.Event()
        # Last timeline duration seen, used to apply a late non-zero value once.
        self._timeline_duration_ms = 0

    async def run(self) -> None:
        """Follow the best media session until cancelled or closed."""

        self._stop.clear()
        try:
            try:
                await self._refresh_session()
            except Exception:
                logger.exception('[winrt] initial session refresh failed')
            await self._watch_sessions()
        finally:
            await self.aclose()

    async def _watch_sessions(self) -> None:
        """Re-scan the session list until `aclose()` is called."""

        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=_SESSION_SCAN_SECONDS
                )
            except TimeoutError:
                try:
                    await self._refresh_session()
                except Exception:
                    logger.exception('[winrt] session refresh failed')

    async def aclose(self) -> None:
        """Unregister all WinRT handlers; safe to call twice."""
        self._stop.set()
        self._unbind_session()
        self.session = None

    def _unbind_session(self) -> None:
        """Remove the handlers registered on the current session."""
        for remove_fn, token in self._session_tokens:
            with suppress(Exception):
                remove_fn(token)
        self._session_tokens = []

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

    def _pick_session(self, current, manager: SessionManager | None):
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
            sessions = list(manager.get_sessions()) if manager else []
        except Exception:
            sessions = []
        playing = [s for s in sessions if self._is_playing(s)]
        if len(playing) == 1:
            return playing[0]
        return current

    async def _request_manager(self) -> SessionManager | None:
        """Request a fresh session manager; None when SMTC is unavailable."""
        try:
            return await SessionManager.request_async()
        except Exception as e:
            logger.debug(f'[winrt] session manager unavailable: {e}')
            return None

    async def _refresh_session(self) -> None:
        """Re-request the manager and follow the best available session.

        Re-requesting (rather than holding the startup manager) is what makes
        an app started after the monitor visible: the old manager is a
        snapshot and keeps answering `None`.
        """

        manager = await self._request_manager()
        if manager is None or self._stop.is_set():
            return
        self.manager = manager
        try:
            current = manager.get_current_session()
        except Exception:
            logger.debug('[winrt] get_current_session failed')
            current = None

        session = self._pick_session(current, manager)
        if session is self.session:
            return
        self._unbind_session()
        if session is None:
            self.session = None
            logger.debug('[winrt] no media session')
            return
        logger.debug(f'[winrt] following session: {self._session_aumid(session)}')

        # Register both handlers before committing the candidate: a failure
        # (e.g. the session dying between pick and register) must leave no
        # half-bound state that later scans would never repair.
        tokens: list[tuple] = []
        try:
            tokens.append((
                session.remove_media_properties_changed,
                session.add_media_properties_changed(self._on_media_properties_changed),
            ))
            tokens.append((
                session.remove_timeline_properties_changed,
                session.add_timeline_properties_changed(self._on_timeline_properties_changed),
            ))
        except Exception:
            # Roll back anything already registered and stay detached so the
            # next scan retries the bind from scratch.
            for remove_fn, token in tokens:
                with suppress(Exception):
                    remove_fn(token)
            self.session = None
            raise
        self._session_tokens = tokens
        self.session = session

        await self._fetch_media_properties()

    @staticmethod
    def _session_aumid(session) -> str:
        try:
            return session.source_app_user_model_id or '<unknown>'
        except Exception:
            return '<unknown>'

    def _on_media_properties_changed(self, sender, args) -> None:
        self._bridge(self._fetch_media_properties())

    async def _fetch_media_properties(self, timeline=None) -> None:
        if self.session is None:
            return
        try:
            props = await self.session.try_get_media_properties_async()
            if timeline is None:
                timeline = self.session.get_timeline_properties()
        except Exception:
            logger.debug('[winrt] media properties not ready')
            return
        if not props:
            return
        duration_ms = 0
        if timeline is not None:
            duration_ms = int(timeline.end_time.total_seconds() * 1000)
        self._timeline_duration_ms = duration_ms
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
        duration_ms = int(timeline.end_time.total_seconds() * 1000)
        if duration_ms <= 0:
            self._timeline_duration_ms = 0
        elif duration_ms != self._timeline_duration_ms:
            await self._fetch_media_properties(timeline)
