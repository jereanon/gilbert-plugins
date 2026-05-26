"""Camera manager — photo capture + managed video streaming.

Two interaction patterns:

1. **Photo capture** (``take_photo()``) — sends a ``photo_request``
   with a correlation id. The cloud schedules the capture, the
   glasses snap the photo, and the cloud responds with
   ``photo_response`` carrying a URL the file is hosted at. We
   return a ``PhotoData`` dataclass; the caller can ``httpx.get``
   the URL to download the bytes themselves.

2. **Managed video stream** (``start_managed_stream()``) — cloud
   relays the glasses' camera feed and gives us HLS / DASH /
   WebRTC URLs for viewers. Returns a ``StreamResult`` once the
   stream is active. ``stop_stream()`` tears it down.

Direct streaming (``startDirectStream`` in the TS SDK — glasses
connect straight to a user-supplied SRT / RTMP URL) is omitted
from v1; Gilbert has no use case for it yet.

Photo download (``GET <photoUrl>``) is intentionally NOT done here
— the manager surfaces the URL and lets the calling service decide
whether to download, where to persist, and what auth headers (if
any) to attach. Each ``request_id`` matches request → response so
concurrent ``take_photo()`` calls don't collide.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from gilbert.interfaces.mentra import PhotoData, StreamResult

from ...protocol.message_types import (
    AppToCloudMessageType,
    CloudToAppMessageType,
)
from ...protocol.streams import StreamType
from .base import ManagerDeps

logger = logging.getLogger(__name__)


__all__ = ["CameraManager", "PhotoSize", "StreamQuality"]


class PhotoSize(StrEnum):
    """Cloud-side resize target. The glasses always shoot at native
    resolution; the cloud resamples for delivery."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    FULL = "full"


class StreamQuality(StrEnum):
    """Managed-stream resolution tier."""

    HD = "720p"
    FULL_HD = "1080p"


_PHOTO_DEFAULT_TIMEOUT_S = 30.0
_STREAM_START_TIMEOUT_S = 30.0


class CameraManager:
    """Photo + livestream control. All methods are async — they
    issue the wire request and (for request/response patterns) await
    the matching cloud event.

    Only works on glasses where ``session.capabilities.has_camera``
    is True (Mentra Live and similar). On display-only glasses the
    cloud responds with a permission-denied error, which surfaces
    as a raised ``RuntimeError`` from these methods.
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps
        # Pending photo requests keyed by request_id — resolved when
        # the matching ``photo_response`` arrives.
        self._pending_photos: dict[str, asyncio.Future[PhotoData]] = {}
        # Pending stream start. Mentra answers via
        # ``managed_stream_status`` events; we resolve when one
        # arrives with status="active" + URLs populated.
        self._pending_stream: asyncio.Future[StreamResult] | None = None
        # Register the response handlers — these stay live for the
        # session's lifetime since photo / stream requests can fire
        # at any time.
        self._cleanups: list[Callable[[], None]] = []
        self._cleanups.append(
            self._deps.register_message_handler(
                CloudToAppMessageType.PHOTO_RESPONSE.value,
                self._on_photo_response,
            )
        )
        self._cleanups.append(
            self._deps.register_stream_handler(
                StreamType.MANAGED_STREAM_STATUS.value,
                self._on_managed_stream_status,
            )
        )

    # ── Photo capture ──────────────────────────────────────────────

    async def take_photo(
        self,
        *,
        size: PhotoSize | str = PhotoSize.MEDIUM,
        save_to_gallery: bool = False,
        sound: bool | None = None,
        timeout: float = _PHOTO_DEFAULT_TIMEOUT_S,
    ) -> PhotoData:
        """Request a photo from the glasses' camera.

        Returns the resolved ``PhotoData`` once the cloud responds
        with the matching ``photo_response``. ``sound=True`` plays
        the shutter sound on the glasses (useful for user feedback
        when the request is initiated by the AI, not the user
        pressing a button)."""
        request_id = f"photo_req_{uuid.uuid4().hex[:16]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PhotoData] = loop.create_future()
        self._pending_photos[request_id] = future

        payload: dict[str, Any] = {
            "type": AppToCloudMessageType.PHOTO_REQUEST.value,
            "packageName": self._deps.package_name,
            "sessionId": self._deps.get_session_id(),
            "requestId": request_id,
            "saveToGallery": save_to_gallery,
            "size": str(size),
        }
        if sound is not None:
            payload["sound"] = bool(sound)

        try:
            await self._deps.send_frame(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_photos.pop(request_id, None)

    # ── Managed livestream ─────────────────────────────────────────

    async def start_managed_stream(
        self,
        *,
        quality: StreamQuality | str | None = None,
        enable_webrtc: bool = True,
        sound: bool | None = None,
        timeout: float = _STREAM_START_TIMEOUT_S,
    ) -> StreamResult:
        """Start a managed livestream — cloud relays the glasses'
        feed and gives us HLS / DASH / WebRTC URLs. Returns the
        ``StreamResult`` once the cloud emits the "active" status
        event with URLs populated.

        Raises ``RuntimeError`` if another start is already in
        flight on this session (the cloud doesn't multiplex
        streams)."""
        if self._pending_stream is not None and not self._pending_stream.done():
            raise RuntimeError(
                "Mentra managed stream start already in flight"
            )
        loop = asyncio.get_running_loop()
        self._pending_stream = loop.create_future()
        payload: dict[str, Any] = {
            "type": AppToCloudMessageType.MANAGED_STREAM_REQUEST.value,
            "packageName": self._deps.package_name,
            "sessionId": self._deps.get_session_id(),
            "enableWebRTC": bool(enable_webrtc),
        }
        if quality is not None:
            payload["quality"] = str(quality)
        if sound is not None:
            payload["sound"] = bool(sound)
        # Subscribe so the cloud forwards status events to us.
        self._deps.add_subscription(StreamType.MANAGED_STREAM_STATUS.value)
        try:
            await self._deps.send_frame(payload)
            return await asyncio.wait_for(self._pending_stream, timeout=timeout)
        finally:
            self._pending_stream = None

    async def stop_stream(self) -> None:
        """Tear down whatever stream is active. Cloud may or may not
        respond with a final ``managed_stream_status`` event — we
        don't wait for it; callers that need the confirmation
        should subscribe to ``managed_stream_status`` directly."""
        await self._deps.send_frame(
            {
                "type": AppToCloudMessageType.MANAGED_STREAM_STOP.value,
                "packageName": self._deps.package_name,
                "sessionId": self._deps.get_session_id(),
            }
        )

    # ── Internal handlers ──────────────────────────────────────────

    async def _on_photo_response(self, message: dict[str, Any]) -> None:
        request_id = str(message.get("requestId") or "")
        future = self._pending_photos.get(request_id)
        if future is None or future.done():
            return
        if message.get("success") is False:
            err = message.get("error") or {}
            msg = str(err.get("message") or err.get("code") or "photo failed")
            future.set_exception(RuntimeError(msg))
            return
        future.set_result(
            PhotoData(
                url=str(message.get("photoUrl") or ""),
                width=int(message.get("width") or 0),
                height=int(message.get("height") or 0),
                timestamp_ms=float(message.get("timestamp") or 0.0),
                saved_to_gallery=bool(message.get("savedToGallery", False)),
                request_id=request_id,
            )
        )

    async def _on_managed_stream_status(
        self, stream_type: str, data: dict[str, Any]
    ) -> None:
        status = str(data.get("status") or "")
        if self._pending_stream is None:
            return
        if self._pending_stream.done():
            return
        if status == "active":
            hls = str(data.get("hlsUrl") or "")
            dash = str(data.get("dashUrl") or "")
            # Only resolve once the URLs have been populated — earlier
            # ``initializing`` events arrive without them.
            if hls and dash:
                self._pending_stream.set_result(
                    StreamResult(
                        hls_url=hls,
                        dash_url=dash,
                        webrtc_url=str(data.get("webrtcUrl") or ""),
                        preview_url=str(data.get("previewUrl") or ""),
                        thumbnail_url=str(data.get("thumbnailUrl") or ""),
                        stream_id=str(data.get("streamId") or ""),
                    )
                )
        elif status in ("error", "stopped"):
            msg = str(data.get("message") or f"stream {status}")
            self._pending_stream.set_exception(RuntimeError(msg))

    def stop(self) -> None:
        """Release everything — called by the session's teardown
        path."""
        for c in self._cleanups:
            c()
        self._cleanups.clear()
        for fut in list(self._pending_photos.values()):
            if not fut.done():
                fut.set_exception(
                    RuntimeError("CameraManager stopped before response")
                )
        self._pending_photos.clear()
        if self._pending_stream is not None and not self._pending_stream.done():
            self._pending_stream.set_exception(
                RuntimeError("CameraManager stopped before stream start")
            )
        self._pending_stream = None
