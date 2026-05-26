"""Mic manager — inbound mic audio + voice-activity events.

Two data streams arrive from the glasses' microphone:

1. **Raw PCM audio chunks** — binary WebSocket frames at 16 kHz
   mono 16-bit signed PCM. The session layer routes binary frames
   through ``handle_binary_audio()``; the manager wraps each frame
   in an ``AudioChunk`` dataclass with metadata and dispatches.
2. **VAD events** — JSON ``data_stream`` messages with
   ``streamType: "VAD"``. The glasses run on-device VAD and emit
   start/stop transitions. The wire ``status`` is sometimes a
   boolean and sometimes a string (``"true"`` / ``"false"``) — the
   manager normalizes both shapes.

This manager is the v1 inbound-audio surface. Outbound binary audio
(streaming TTS chunks back to the speaker) is the ``SpeakerManager``
job and the wire format is documented in upstream
``SpeakerManager.ts`` — we ship a stub here and finish it when we
have a real device to test against.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from gilbert.interfaces.mentra import AudioChunk, VadEvent

from ...protocol.streams import StreamType
from .base import ManagerDeps

logger = logging.getLogger(__name__)


__all__ = ["AudioChunkHandler", "MicManager", "VadHandler"]


AudioChunkHandler = Callable[[AudioChunk], Awaitable[None]]
VadHandler = Callable[[VadEvent], Awaitable[None]]


class MicManager:
    """Inbound microphone subscriptions.

    ``on_audio_chunk(handler)`` — every binary frame the session
    forwards becomes an ``AudioChunk``. Subscribing adds the
    ``audio_chunk`` stream to the cloud-side filter so we don't
    pay bandwidth when no consumer is listening.

    ``on_voice_activity(handler)`` — JSON VAD events. Cached state
    via ``is_speaking``.
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps
        self._chunk_handlers: list[AudioChunkHandler] = []
        self._vad_handlers: list[VadHandler] = []
        self._vad_cleanup: Callable[[], None] | None = None
        self._is_speaking = False

    # ── Public surface ─────────────────────────────────────────────

    @property
    def is_speaking(self) -> bool:
        """Last-known VAD state. Updates only while there's an active
        ``on_voice_activity`` subscriber (the cloud doesn't push VAD
        otherwise)."""
        return self._is_speaking

    @property
    def is_active(self) -> bool:
        """True when at least one audio-chunk handler is registered.
        The microphone stream is subscribed exactly in this case."""
        return bool(self._chunk_handlers)

    def on_audio_chunk(
        self, handler: AudioChunkHandler
    ) -> Callable[[], None]:
        """Subscribe to inbound raw-PCM frames. Returns an
        unsubscribe callable. First subscriber adds the
        ``audio_chunk`` stream subscription; last unsubscribe removes
        it. Cloud doesn't ship binary frames until subscribed."""
        first = not self._chunk_handlers
        self._chunk_handlers.append(handler)
        if first:
            self._deps.add_subscription(StreamType.AUDIO_CHUNK.value)

        def _unsub() -> None:
            try:
                self._chunk_handlers.remove(handler)
            except ValueError:
                return
            if not self._chunk_handlers:
                self._deps.remove_subscription(StreamType.AUDIO_CHUNK.value)

        return _unsub

    def on_voice_activity(
        self, handler: VadHandler
    ) -> Callable[[], None]:
        """Subscribe to VAD start/stop events."""
        first = not self._vad_handlers
        self._vad_handlers.append(handler)
        if first:
            self._vad_cleanup = self._deps.register_stream_handler(
                StreamType.VAD.value, self._on_vad
            )
            self._deps.add_subscription(StreamType.VAD.value)

        def _unsub() -> None:
            try:
                self._vad_handlers.remove(handler)
            except ValueError:
                return
            if not self._vad_handlers:
                self._deps.remove_subscription(StreamType.VAD.value)
                if self._vad_cleanup is not None:
                    self._vad_cleanup()
                    self._vad_cleanup = None

        return _unsub

    def stop(self) -> None:
        """Clear all subscriptions + reset cached state."""
        if self._chunk_handlers:
            self._deps.remove_subscription(StreamType.AUDIO_CHUNK.value)
            self._chunk_handlers.clear()
        if self._vad_handlers:
            self._deps.remove_subscription(StreamType.VAD.value)
            self._vad_handlers.clear()
        if self._vad_cleanup is not None:
            self._vad_cleanup()
            self._vad_cleanup = None
        self._is_speaking = False

    # ── Internal — called by the session ──────────────────────────

    async def handle_binary_audio(self, data: bytes) -> None:
        """Session calls this for every binary WS frame. We treat
        every binary frame as mic audio — Mentra's protocol doesn't
        currently multiplex multiple binary streams on one
        connection. Skip if no subscriber to save the dispatch
        overhead."""
        if not self._chunk_handlers:
            return
        chunk = AudioChunk(
            data=data,
            sample_rate=16000,
            channels=1,
            timestamp_ms=time.time() * 1000.0,
        )
        for handler in list(self._chunk_handlers):
            try:
                await handler(chunk)
            except Exception:
                logger.exception("Mentra audio chunk handler raised")

    async def _on_vad(self, stream_type: str, data: dict[str, Any]) -> None:
        raw_status = data.get("status")
        if isinstance(raw_status, bool):
            is_speaking = raw_status
        elif isinstance(raw_status, str):
            is_speaking = raw_status.strip().lower() == "true"
        else:
            logger.debug(
                "Mentra VAD event with unexpected status type: %r", raw_status
            )
            return
        self._is_speaking = is_speaking
        event = VadEvent(
            is_speaking=is_speaking, timestamp_ms=time.time() * 1000.0
        )
        for handler in list(self._vad_handlers):
            try:
                await handler(event)
            except Exception:
                logger.exception("Mentra VAD handler raised")
