"""Track source protocol."""

from typing import Protocol


class TrackSource(Protocol):
    """A backend that emits track field updates to a TrackMonitor."""

    async def run(self) -> None:
        """Emit track updates until cancelled or disconnected."""
        ...
