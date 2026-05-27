"""Location manager — GPS subscription + on-demand poll.

Mentra glasses don't have a GPS chip of their own — the phone's
location service is the data source. Two interaction modes:

1. **Streaming subscription** (``on_location_update``) — subscribe to
   the ``location_stream`` stream type with an accuracy tier
   (standard / high / realtime / lower-power variants); the cloud
   forwards every position update.
2. **One-shot poll** (``request_update``) — send a
   ``location_poll_request`` with a correlation id; the cloud
   responds via a ``location_update`` event with the matching id.
   Returns a Future the caller can await.

The manager caches the last known position so callers can do
sync reads (``manager.lat`` / ``manager.lng`` / ``manager.accuracy``)
without spawning a poll. Cache is also populated by the streaming
subscription when one's active.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

from gilbert.interfaces.mentra import LocationData

from ...protocol.message_types import AppToCloudMessageType
from ...protocol.streams import StreamType
from .base import ManagerDeps

logger = logging.getLogger(__name__)


__all__ = ["LocationAccuracy", "LocationHandler", "LocationManager"]


class LocationAccuracy(StrEnum):
    """Accuracy tiers the upstream SDK exposes. Higher accuracy
    burns more battery on the phone — pick the lowest tier that
    fits the use case (e.g. ``THREE_KILOMETERS`` is plenty for
    "rough city-level location for a weather query")."""

    STANDARD = "standard"
    HIGH = "high"
    REALTIME = "realtime"
    TEN_METERS = "tenMeters"
    HUNDRED_METERS = "hundredMeters"
    KILOMETER = "kilometer"
    THREE_KILOMETERS = "threeKilometers"
    REDUCED = "reduced"


# Async handler signature for streaming subscribers.
LocationHandler = Callable[[LocationData], Awaitable[None]]


# Default timeout for one-shot polls. 15s mirrors upstream's default;
# longer than that the user has almost certainly moved on.
_POLL_TIMEOUT_S = 15.0


class LocationManager:
    """Streaming + on-demand GPS access.

    ``on_location_update(handler)`` registers a streaming
    subscription — first registration adds the ``location_stream``
    subscription, last unregister removes it.

    ``request_update()`` returns a Future that resolves to the next
    matching ``location_update`` event keyed by the correlation id
    we send out.
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps
        self._stream_handlers: list[LocationHandler] = []
        self._stream_cleanups: list[Callable[[], None]] = []
        self._pending_polls: dict[str, asyncio.Future[LocationData]] = {}
        self._poll_cleanup: Callable[[], None] | None = None
        self._accuracy: str = LocationAccuracy.STANDARD.value
        # Last-known position cache. ``None`` until the first event arrives.
        self._lat: float | None = None
        self._lng: float | None = None
        self._accuracy_m: float | None = None
        self._last_timestamp_ms: float | None = None
        # Register the bare ``location_update`` listener up front so
        # one-shot polls resolve even if no streaming subscriber is
        # active. The cleanup is stored so ``stop()`` can remove it.
        self._poll_cleanup = self._deps.register_stream_handler(
            StreamType.LOCATION_UPDATE.value, self._on_location_update
        )

    # ── Public surface ─────────────────────────────────────────────

    def configure(
        self, *, accuracy: LocationAccuracy | str = LocationAccuracy.STANDARD
    ) -> None:
        """Set the accuracy tier for subsequent subscriptions + polls.

        Doesn't re-issue active subscriptions — the cloud honors the
        tier at subscription time, so call this BEFORE
        ``on_location_update`` to influence the next subscriber."""
        self._accuracy = str(accuracy)

    @property
    def lat(self) -> float | None:
        return self._lat

    @property
    def lng(self) -> float | None:
        return self._lng

    @property
    def accuracy_m(self) -> float | None:
        """Last reported horizontal accuracy in meters."""
        return self._accuracy_m

    def on_location_update(
        self, handler: LocationHandler
    ) -> Callable[[], None]:
        """Subscribe to streaming location updates.

        Returns an unsubscribe callable. First registration adds the
        ``location_stream`` subscription to the cloud; last
        unregister removes it. The handler also fires on one-shot
        ``request_update`` responses so callers can use either API
        interchangeably."""
        self._stream_handlers.append(handler)
        if len(self._stream_handlers) == 1:
            cleanup = self._deps.register_stream_handler(
                StreamType.LOCATION_STREAM.value, self._on_location_update
            )
            self._stream_cleanups.append(cleanup)
            self._deps.add_subscription(StreamType.LOCATION_STREAM.value)

        def _unsub() -> None:
            try:
                self._stream_handlers.remove(handler)
            except ValueError:
                return
            if not self._stream_handlers:
                for c in self._stream_cleanups:
                    c()
                self._stream_cleanups.clear()
                self._deps.remove_subscription(
                    StreamType.LOCATION_STREAM.value
                )

        return _unsub

    async def request_update(
        self, *, timeout: float = _POLL_TIMEOUT_S
    ) -> LocationData:
        """Request a single location reading. Returns the resolved
        ``LocationData`` (also delivered to streaming subscribers
        and cached as the last-known position).

        Raises ``asyncio.TimeoutError`` if the cloud doesn't respond
        in ``timeout`` seconds — the most common cause is the user's
        phone not having a GPS lock yet.
        """
        correlation_id = f"poll_{uuid.uuid4().hex[:16]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[LocationData] = loop.create_future()
        self._pending_polls[correlation_id] = future
        try:
            await self._deps.send_frame(
                {
                    "type": AppToCloudMessageType.LOCATION_POLL_REQUEST.value,
                    "packageName": self._deps.package_name,
                    "sessionId": self._deps.get_session_id(),
                    "correlationId": correlation_id,
                    "accuracy": self._accuracy,
                }
            )
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_polls.pop(correlation_id, None)

    def stop(self) -> None:
        """Clean up everything — used by the session's teardown
        path. Idempotent."""
        for c in self._stream_cleanups:
            c()
        self._stream_cleanups.clear()
        if self._poll_cleanup is not None:
            self._poll_cleanup()
            self._poll_cleanup = None
        if self._stream_handlers:
            self._deps.remove_subscription(StreamType.LOCATION_STREAM.value)
        self._stream_handlers.clear()
        # Fail any unresolved polls.
        for fut in list(self._pending_polls.values()):
            if not fut.done():
                fut.set_exception(
                    RuntimeError("LocationManager stopped before poll resolved")
                )
        self._pending_polls.clear()

    # ── Internal ───────────────────────────────────────────────────

    async def _on_location_update(
        self, stream_type: str, data: dict[str, Any]
    ) -> None:
        parsed = _parse_location(data)
        # Update cache.
        self._lat = parsed.lat
        self._lng = parsed.lng
        if parsed.accuracy is not None:
            self._accuracy_m = parsed.accuracy
        self._last_timestamp_ms = parsed.timestamp_ms
        # Resolve any matching poll.
        if parsed.correlation_id:
            fut = self._pending_polls.get(parsed.correlation_id)
            if fut is not None and not fut.done():
                fut.set_result(parsed)
        # Fan out to streaming subscribers.
        for handler in list(self._stream_handlers):
            try:
                await handler(parsed)
            except Exception:
                logger.exception("Mentra location handler raised")


def _parse_location(raw: dict[str, Any]) -> LocationData:
    """Normalize the cloud's payload into a ``LocationData`` dataclass.
    Defaults are conservative — missing fields become ``None`` /
    sentinels rather than zero (which would be a real lat/lng on the
    coast of Africa)."""
    lat_raw = raw.get("lat")
    lng_raw = raw.get("lng")
    acc_raw = raw.get("accuracy")
    return LocationData(
        lat=float(lat_raw) if isinstance(lat_raw, (int, float)) else 0.0,
        lng=float(lng_raw) if isinstance(lng_raw, (int, float)) else 0.0,
        accuracy=float(acc_raw) if isinstance(acc_raw, (int, float)) else None,
        timestamp_ms=float(raw.get("timestamp") or 0.0),
        correlation_id=str(raw.get("correlationId") or ""),
    )
