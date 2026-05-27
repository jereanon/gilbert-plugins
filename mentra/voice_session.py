"""``ConversationSession`` + ``AudioSink`` impls for Mentra glasses.

The Mentra plugin is a transport — Gilbert's core ``voice_brain``
``ConversationEngine`` runs the actual conversation loop (echo
suppression, local VAD, AI dispatch, TTS pacing, barge-in). This
module provides the two adapters that bridge the Mentra session
(WebSocket to Mentra Cloud + mic + speaker managers) to the
engine's abstractions:

- ``_MentraConversationSession`` — wraps a live ``MentraSession``
  as a ``ConversationSession`` (the engine's audio-I/O + events
  surface). Inbound mic chunks land in an ``asyncio.Queue`` the
  engine pulls from; outbound writes go to the ``_MentraAudioSink``.

- ``_MentraAudioSink`` — buffers engine-produced MP3 chunks until
  ``flush()``; on flush, registers the assembled clip with the
  core ``audio_blob_store`` capability, then calls
  ``SpeakerManager.play_url`` with a public
  ``/api/audio-blob/<blob_id>`` URL. Mentra Cloud fetches that URL
  server-side and streams the bytes to the glasses speaker.

Two design choices worth highlighting:

1. **Why a blob store instead of /api/tts?** /api/tts re-synthesizes
   every fetch (engine synthesizes ONCE for its barge-in / pacing
   logic; the cloud fetch would synthesize AGAIN). 2× ElevenLabs
   cost. The blob route serves the engine's exact bytes — single
   synth, no waste.

2. **Why an ``audio_in`` queue if MicManager already dispatches?**
   The engine pulls from an ``AsyncIterator[bytes]``. MicManager
   pushes via callback. The queue is the buffer that bridges the
   two paradigms without dropping chunks under load.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from gilbert.interfaces.audio_blob import AudioBlobStore
from gilbert.interfaces.conversation import (
    ConversationSession,
    ConversationStatusEvent,
)

logger = logging.getLogger(__name__)


__all__ = ["_MentraAudioSink", "_MentraConversationSession"]


class _MentraAudioSink:
    """``AudioSink`` impl that ferries engine-synthesized audio to
    Mentra Cloud via a blob-URL handoff.

    The engine writes chunks via ``write()``; we buffer them. On
    ``flush()`` (end-of-utterance) we hand the assembled bytes to
    the blob store and tell the speaker manager to play the URL.
    ``clear()`` discards the buffer + sends an explicit stop to the
    cloud so an in-flight playback is interrupted.

    State per instance is per-session — never shared across users.
    """

    def __init__(
        self,
        *,
        blob_store: AudioBlobStore,
        speaker: Any,            # SpeakerManager (avoid circular import)
        public_base_url: str,
        mime: str = "audio/mpeg",
        session_id_for_log: str = "",
    ) -> None:
        self._blob_store = blob_store
        self._speaker = speaker
        self._public_base_url = (public_base_url or "").rstrip("/")
        self._mime = mime
        self._session_id = session_id_for_log
        self._buffer = bytearray()
        self._utterances_sent = 0

    async def write(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def clear(self) -> None:
        """Drop the unflushed buffer AND tell Mentra Cloud to stop any
        in-flight playback. The engine fires this on barge-in — if we
        only cleared the local buffer, the cloud would already have
        fetched the previous URL and the user would hear Gilbert
        finish a sentence after they interrupted."""
        self._buffer.clear()
        try:
            await self._speaker.stop()
        except Exception:
            logger.debug(
                "MentraAudioSink.clear: speaker.stop raised "
                "(session=%s)",
                self._session_id,
                exc_info=True,
            )

    async def flush(self) -> None:
        """End-of-utterance signal — register the buffered bytes as a
        blob, then play the URL.

        Empty buffer is a no-op (the engine occasionally calls flush
        on tool-only turns where no audio was written)."""
        if not self._buffer:
            return
        if not self._public_base_url:
            logger.warning(
                "MentraAudioSink.flush: public_base_url is empty — "
                "Mentra Cloud has no host to fetch from. Dropping "
                "this utterance (%d bytes, session=%s).",
                len(self._buffer),
                self._session_id,
            )
            self._buffer.clear()
            return

        payload = bytes(self._buffer)
        self._buffer.clear()
        try:
            blob_id = self._blob_store.register(
                payload, self._mime, ttl_seconds=120.0
            )
        except Exception:
            logger.exception(
                "MentraAudioSink.flush: blob_store.register failed "
                "(session=%s)",
                self._session_id,
            )
            return

        url = f"{self._public_base_url}/api/audio-blob/{blob_id}"
        self._utterances_sent += 1
        logger.info(
            "MentraAudioSink flush — session=%s utterance=%d bytes=%d "
            "url=%s",
            self._session_id,
            self._utterances_sent,
            len(payload),
            url,
        )
        try:
            # Track 2 is the dedicated TTS track per the upstream SDK
            # convention — keeps speech from preempting music on
            # track 0.
            await self._speaker.play_url(
                url,
                track_id=2,
                stop_other_audio=False,
            )
        except Exception:
            logger.exception(
                "MentraAudioSink.flush: speaker.play_url failed "
                "(session=%s url=%s)",
                self._session_id,
                url,
            )


@dataclass
class _MentraConversationSession(ConversationSession):
    """Concrete ``ConversationSession`` for a Mentra glasses session.

    The engine reads inbound mic PCM from ``audio_in``, writes
    synthesized audio bytes to ``audio_out``, and watches ``events``
    for lifecycle transitions (PENDING → ACTIVE → ENDED).

    Wiring is done by ``MentraService._wire_voice_session``:
    ``MicManager.on_audio_chunk`` → ``push_audio_chunk`` → queue →
    engine. The session's WebSocket dropping flips ENDED.
    """

    _audio_in_queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=500)
    )
    _events_queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(maxsize=200)
    )
    closed: bool = False

    async def push_audio_chunk(self, chunk: bytes) -> None:
        """Forward one mic frame to the engine's audio iterator.
        Drops the oldest frame on queue overflow rather than blocking
        — under load, losing a 20ms chunk is better than stalling the
        whole WS reader. Same overflow policy as voice-agent."""
        try:
            self._audio_in_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                self._audio_in_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._audio_in_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass

    async def push_event(self, ev: Any) -> None:
        try:
            self._events_queue.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    async def _audio_in_iter(self) -> AsyncIterator[bytes]:
        """Iterator the engine pulls from. Wakes every second so
        ``self.closed`` flipping mid-await isn't a deadlock."""
        while not self.closed:
            try:
                chunk = await asyncio.wait_for(
                    self._audio_in_queue.get(), timeout=1.0
                )
                yield chunk
            except TimeoutError:
                continue

    async def _events_iter(self) -> AsyncIterator[Any]:
        """Iterator for status events. Exits on terminal status so
        the engine's status loop unblocks cleanly."""
        from gilbert.interfaces.conversation import ConversationStatus

        while not self.closed:
            try:
                ev = await asyncio.wait_for(
                    self._events_queue.get(), timeout=1.0
                )
                yield ev
                if (
                    isinstance(ev, ConversationStatusEvent)
                    and ev.status
                    in (ConversationStatus.ENDED, ConversationStatus.FAILED)
                ):
                    return
            except TimeoutError:
                continue

    async def end_session(self) -> None:
        """Push a terminal event. The events iterator notices the
        ENDED discriminator and exits naturally; we don't flip
        ``self.closed`` directly here to avoid racing the iterator's
        ``while not self.closed`` check + swallowing the event
        before it's yielded."""
        from gilbert.interfaces.conversation import ConversationStatus

        try:
            self._events_queue.put_nowait(
                ConversationStatusEvent(status=ConversationStatus.ENDED)
            )
        except asyncio.QueueFull:
            pass
