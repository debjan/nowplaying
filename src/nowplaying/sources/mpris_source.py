"""MPRIS track source for Linux/Raspberry Pi (local media players).

Listens on the session bus for `org.mpris.MediaPlayer2.Player`
`PropertiesChanged` signals (Spotify, VLC, mpv, browsers, ...). MPRIS
carries metadata in a flat `Metadata` dict (`xesam:title`,
`xesam:artist`, `xesam:album`, `mpris:length` in microseconds) -
different shape from BlueZ's nested `Track` dict, hence the separate
extraction. Runs concurrently with the BlueZ source; last-change-wins
via the shared TrackMonitor.
"""

import asyncio
from contextlib import suppress

from dbus_next import Message
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, MessageType
from dbus_next.errors import DBusError
from loguru import logger

_MPRIS_PREFIX = 'org.mpris.MediaPlayer2'
_MPRIS_PLAYER_IFACE = 'org.mpris.MediaPlayer2.Player'
_MPRIS_PATH = '/org/mpris/MediaPlayer2'

_MATCH_RULE = (
    "type='signal',interface='org.freedesktop.DBus.Properties',"
    "member='PropertiesChanged',arg0namespace='org.mpris.MediaPlayer2'"
)
_SEEKED_MATCH_RULE = (
    "type='signal',interface='org.mpris.MediaPlayer2.Player',member='Seeked'"
)

_POSITION_POLL_INTERVAL = 10.0


def _unwrap(value):
    """Return the plain value, unwrapping a dbus-next Variant if needed."""

    return getattr(value, 'value', value)


def _extract_mpris_metadata(meta: dict) -> dict[str, str]:
    """Extract our fields from an MPRIS `Metadata` dict (a{sv})."""

    fields = {}
    title = meta.get('xesam:title')
    if title is not None:
        fields['Title'] = str(_unwrap(title))
    artist = meta.get('xesam:artist')
    if artist is not None:
        artists = _unwrap(artist)
        if isinstance(artists, (list, tuple)):
            artists = ', '.join(str(a) for a in artists)
        fields['Artist'] = str(artists)
    album = meta.get('xesam:album')
    if album is not None:
        fields['Album'] = str(_unwrap(album))
    length = meta.get('mpris:length')
    if length is not None:
        try:
            # mpris:length is microseconds; our Duration is milliseconds.
            fields['Duration'] = str(int(_unwrap(length)) // 1000)
        except (TypeError, ValueError):
            logger.debug(f'[mpris] unparseable mpris:length: {length!r}')
    return fields


class MprisSource:
    """Emit track updates from MPRIS D-Bus signals on the session bus."""

    def __init__(self, monitor, loop: asyncio.AbstractEventLoop) -> None:
        self.monitor = monitor
        self.loop = loop
        self.bus: MessageBus | None = None
        self.player_service: str | None = None

    async def run(self) -> None:
        """Connect to the session bus and run until disconnected.

        A missing session bus (e.g. headless Pi with no user session) is
        not fatal - BlueZ monitoring continues without local players.
        """

        try:
            self.bus = await MessageBus(bus_type=BusType.SESSION).connect()
        except Exception:
            logger.warning('[mpris] no session bus; skipping local media players')
            return
        logger.debug('Monitoring session bus for MPRIS track metadata...')
        try:
            await self._run()
        finally:
            self.bus.disconnect()

    async def _get_prop(self, service: str, iface: str, prop: str):
        """Fetch one property via Properties.Get; None on any failure."""

        assert self.bus is not None
        try:
            reply = await self.bus.call(
                Message(
                    destination=service,
                    path=_MPRIS_PATH,
                    interface='org.freedesktop.DBus.Properties',
                    member='Get',
                    signature='ss',
                    body=[iface, prop],
                )
            )
        except Exception:
            logger.debug(f'[mpris] Get {prop} call failed for {service}')
            return None
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return None
        return _unwrap(reply.body[0])

    async def _snapshot_current_players(self) -> None:
        """Apply the current track of an already-playing MPRIS player.

        Without this the dashboard stays empty until the next track
        change when the monitor starts mid-playback.
        """

        assert self.bus is not None
        try:
            reply = await self.bus.call(
                Message(
                    destination='org.freedesktop.DBus',
                    path='/org/freedesktop/DBus',
                    interface='org.freedesktop.DBus',
                    member='ListNames',
                )
            )
        except Exception:
            logger.debug('[mpris] ListNames failed; skipping startup snapshot')
            return
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            return
        players = sorted(
            n for n in reply.body[0]
            if n.startswith(_MPRIS_PREFIX) and n != _MPRIS_PLAYER_IFACE
        )
        if not players:
            return
        fallback: str | None = None
        for player in players:
            status = await self._get_prop(player, _MPRIS_PLAYER_IFACE, 'PlaybackStatus')
            meta = await self._get_prop(player, _MPRIS_PLAYER_IFACE, 'Metadata')
            if not isinstance(meta, dict):
                continue
            fields = _extract_mpris_metadata(meta)
            if not any(key in fields for key in ('Artist', 'Album', 'Title')):
                continue
            self.player_service = player
            if status == 'Playing':
                logger.debug(f'[mpris] startup snapshot from playing player {player}')
                self.monitor.submit(self.monitor.apply, fields)
                return
            if fallback is None:
                fallback = player
        if fallback is not None:
            meta = await self._get_prop(fallback, _MPRIS_PLAYER_IFACE, 'Metadata')
            if isinstance(meta, dict):
                fields = _extract_mpris_metadata(meta)
                if any(key in fields for key in ('Artist', 'Album', 'Title')):
                    logger.debug(f'[mpris] startup snapshot from {fallback}')
                    self.monitor.submit(self.monitor.apply, fields)

    async def _on_metadata_signal(self, sender: str) -> None:
        """Fetch the MPRIS `Metadata` property and apply it."""

        meta = await self._get_prop(sender, _MPRIS_PLAYER_IFACE, 'Metadata')
        if not isinstance(meta, dict):
            logger.debug('[mpris] Get Metadata returned nothing usable')
            return
        fields = _extract_mpris_metadata(meta)
        logger.debug(f'[mpris] Get Metadata -> {fields}')
        if any(key in fields for key in ('Artist', 'Album', 'Title')):
            self.monitor.submit(self.monitor.apply, fields)
        else:
            logger.debug('[mpris] Metadata incomplete; nothing to apply')

    async def _poll_position(self) -> None:
        """Poll MPRIS `Position` every 10s (MPRIS never signals it)."""

        assert self.bus is not None
        while True:
            await asyncio.sleep(_POSITION_POLL_INTERVAL)
            if self.player_service is None:
                continue
            position = await self._get_prop(
                self.player_service, _MPRIS_PLAYER_IFACE, 'Position'
            )
            if position is None:
                continue
            try:
                position_ms = int(position) // 1000
            except (TypeError, ValueError):
                continue
            self.monitor.submit(self.monitor.set_position, position_ms)

    async def _run(self) -> None:
        """Register signal matches, snapshot, and run until disconnect."""

        assert self.bus is not None

        def handler(msg: Message) -> None:
            if msg.message_type != MessageType.SIGNAL:
                return
            if (
                msg.interface == 'org.freedesktop.DBus.Properties'
                and msg.member == 'PropertiesChanged'
                and msg.body
                and isinstance(msg.body[0], str)
                and msg.body[0].startswith(_MPRIS_PREFIX)
            ):
                self.player_service = msg.sender
                changed = msg.body[1]
                invalidated = msg.body[2] if len(msg.body) > 2 else []
                if 'Metadata' not in changed and 'Metadata' not in invalidated:
                    return
                if 'Metadata' in changed:
                    meta = _unwrap(changed['Metadata'])
                    if not isinstance(meta, dict):
                        return
                    fields = _extract_mpris_metadata(meta)
                    logger.debug(f'[mpris] Metadata changed -> {fields}')
                    if any(key in fields for key in ('Artist', 'Album', 'Title')):
                        self.monitor.submit(self.monitor.apply, fields)
                        return
                logger.debug('[mpris] Metadata invalidated; fetching via Get')
                asyncio.create_task(self._on_metadata_signal(msg.sender))
            elif (
                msg.interface == _MPRIS_PLAYER_IFACE
                and msg.member == 'Seeked'
                and msg.body
            ):
                # Seeked carries the new position in microseconds.
                try:
                    position_ms = int(_unwrap(msg.body[0])) // 1000
                except (TypeError, ValueError, IndexError):
                    logger.debug(f'[mpris] unparseable Seeked: {msg.body!r}')
                    return
                self.monitor.submit(self.monitor.set_position, position_ms)

        self.bus.add_message_handler(handler)
        for rule in (_MATCH_RULE, _SEEKED_MATCH_RULE):
            try:
                await self.bus.call(
                    Message(
                        destination='org.freedesktop.DBus',
                        path='/org/freedesktop/DBus',
                        interface='org.freedesktop.DBus',
                        member='AddMatch',
                        signature='s',
                        body=[rule],
                    )
                )
            except DBusError as e:
                logger.error(f'[mpris] AddMatch failed: {e} - no signals will match {rule}')
            else:
                logger.debug(f'[mpris] AddMatch registered: {rule}')
        await self._snapshot_current_players()
        poll_task = asyncio.create_task(self._poll_position())
        try:
            await self.bus.wait_for_disconnect()
        finally:
            poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await poll_task
