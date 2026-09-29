"""D-Bus track source for Linux/Raspberry Pi (BlueZ MediaPlayer1)."""

import asyncio
from contextlib import suppress

from dbus_next import Message
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, MessageType
from dbus_next.errors import DBusError
from loguru import logger

_MATCH_RULE = (
    "type='signal',interface='org.freedesktop.DBus.Properties',"
    "member='PropertiesChanged',arg0='org.bluez.MediaPlayer1'"
)

_SIGNAL_KEYS = ('Artist', 'Album', 'Title', 'Duration')

_POSITION_POLL_INTERVAL = 10.0


def _extract_track(meta: dict) -> dict[str, str]:
    """Extract our fields from a BlueZ MediaPlayer1 `Track` dict (a{sv})."""

    fields = {}
    for key in _SIGNAL_KEYS:
        value = meta.get(key)
        if value is not None:
            fields[key] = str(value.value)
    return fields


class DbusSource:
    """Emit track updates from BlueZ D-Bus signals."""

    def __init__(self, monitor, loop: asyncio.AbstractEventLoop) -> None:
        self.monitor = monitor
        self.loop = loop
        self.bus: MessageBus | None = None

    async def run(self) -> None:
        """Connect to the system bus and run until disconnected."""

        self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        logger.debug('Monitoring D-Bus passively for track metadata...')
        try:
            await self._run()
        finally:
            self.bus.disconnect()

    async def _on_track_signal(self, msg: Message) -> None:
        """Fetch the MediaPlayer1 `Track` property and apply its metadata."""

        assert self.bus is not None
        try:
            reply = await self.bus.call(
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
                self.monitor.submit(self.monitor.apply, fields)
            else:
                logger.debug('[dbus] Track metadata incomplete; nothing to apply')
        except Exception:
            logger.exception('[Error] failed to fetch track properties; continuing')

    async def _poll_position(self) -> None:
        """Poll MediaPlayer1 `Position` every 10s.

        Some devices (e.g. phones over AVRCP) never emit Position signals, so the
        stored position would otherwise go stale. The property itself is live.
        Writes go through set_position(), which skips unchanged values.
        """

        assert self.bus is not None
        while True:
            await asyncio.sleep(_POSITION_POLL_INTERVAL)
            monitor = self.monitor
            if monitor.player_path is None or monitor.player_service is None:
                continue
            try:
                reply = await self.bus.call(
                    Message(
                        destination=monitor.player_service,
                        path=monitor.player_path,
                        interface='org.freedesktop.DBus.Properties',
                        member='Get',
                        signature='ss',
                        body=['org.bluez.MediaPlayer1', 'Position'],
                    )
                )
            except Exception:
                logger.debug('[dbus] poll Position call failed; retrying next interval')
                continue
            if reply is None or reply.message_type != MessageType.METHOD_RETURN:
                continue
            try:
                position = int(reply.body[0].value)
            except (TypeError, ValueError, IndexError, AttributeError):
                continue
            monitor.submit(monitor.set_position, position)

    async def _run(self) -> None:
        """Register the signal match and run until the bus disconnects."""

        assert self.bus is not None

        def handler(msg: Message) -> None:
            if (
                msg.message_type != MessageType.SIGNAL
                or msg.interface != 'org.freedesktop.DBus.Properties'
                or msg.member != 'PropertiesChanged'
            ):
                return
            if not msg.body or msg.body[0] != 'org.bluez.MediaPlayer1':
                return
            self.monitor.player_path = msg.path
            self.monitor.player_service = msg.sender
            changed = msg.body[1]
            invalidated = msg.body[2] if len(msg.body) > 2 else []
            if 'Track' not in changed and 'Track' not in invalidated:
                if 'Position' in changed:
                    try:
                        position = int(changed['Position'].value)
                    except (TypeError, ValueError):
                        logger.debug(f'[dbus] unparseable Position: {changed["Position"]!r}')
                        return
                    self.monitor.submit(self.monitor.set_position, position)
                return
            if 'Track' in changed:
                fields = _extract_track(changed['Track'].value)
                logger.debug(f'[dbus] Track changed -> {fields}')
                if any(key in fields for key in _SIGNAL_KEYS[:3]):
                    self.monitor.submit(self.monitor.apply, fields)
                    return
            logger.debug('[dbus] Track invalidated; fetching via Get')
            asyncio.create_task(self._on_track_signal(msg))

        self.bus.add_message_handler(handler)
        try:
            await self.bus.call(
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
            logger.error(f'[dbus] AddMatch failed: {e} - no signals will be delivered')
        else:
            logger.debug(f'[dbus] AddMatch registered: {_MATCH_RULE}')
        poll_task = asyncio.create_task(self._poll_position())
        try:
            await self.bus.wait_for_disconnect()
        finally:
            poll_task.cancel()
            with suppress(asyncio.CancelledError):
                await poll_task
