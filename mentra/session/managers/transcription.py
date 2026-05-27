"""Transcription manager — surfaces glasses-microphone STT events.

The cloud streams partial transcripts as the user speaks plus a
final ``isFinal=true`` frame when they pause. App code typically
ignores partials (use them only for live UI feedback) and acts on
finals. The Gilbert integration follows that rule — only finals
make it into ``AIService.chat``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gilbert.interfaces.mentra import TranscriptionData

from ...protocol.streams import StreamType
from .base import ManagerDeps

logger = logging.getLogger(__name__)


# Public callback signature — receives a parsed dataclass, not the
# raw JSON, so app code stays type-safe.
TranscriptionHandler = Callable[[TranscriptionData], Awaitable[None]]


class TranscriptionManager:
    """Subscribe to and surface transcription stream events.

    Call ``on_transcription(handler)`` from app code; the manager
    registers the stream subscription with the cloud and converts
    inbound payloads into ``TranscriptionData`` dataclasses before
    invoking the handler.
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps
        self._handlers: list[TranscriptionHandler] = []
        self._registered = False
        self._cleanup: Callable[[], None] | None = None

    def on_transcription(
        self, handler: TranscriptionHandler
    ) -> Callable[[], None]:
        """Register an async handler for transcription events.

        Returns an unsubscribe callable. Multiple handlers can be
        registered — all fire on each event. The first registration
        also adds the cloud-side subscription; the last unregister
        removes it.
        """
        self._handlers.append(handler)
        self._ensure_subscribed()

        def _unsub() -> None:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass
            if not self._handlers:
                self._unsubscribe()

        return _unsub

    # ── Internal ───────────────────────────────────────────────────

    def _ensure_subscribed(self) -> None:
        if self._registered:
            return
        self._cleanup = self._deps.register_stream_handler(
            StreamType.TRANSCRIPTION.value,
            self._dispatch,
        )
        self._deps.add_subscription(StreamType.TRANSCRIPTION.value)
        self._registered = True

    def _unsubscribe(self) -> None:
        if not self._registered:
            return
        self._deps.remove_subscription(StreamType.TRANSCRIPTION.value)
        if self._cleanup is not None:
            self._cleanup()
        self._cleanup = None
        self._registered = False

    async def _dispatch(self, stream_type: str, data: dict[str, Any]) -> None:
        parsed = TranscriptionData(
            text=str(data.get("text") or ""),
            is_final=bool(data.get("isFinal", False)),
            transcribe_language=str(data.get("transcribeLanguage") or ""),
            confidence=float(data.get("confidence") or 0.0),
            start_time=float(data.get("startTime") or 0.0),
            end_time=float(data.get("endTime") or 0.0),
            speaker_id=str(data.get("speakerId") or ""),
            duration=float(data.get("duration") or 0.0),
        )
        for handler in list(self._handlers):
            try:
                await handler(parsed)
            except Exception:
                logger.exception("Mentra transcription handler raised")
