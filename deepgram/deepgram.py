"""Deepgram streaming speech-to-text backend.

Uses raw ``websockets`` rather than the ``deepgram-sdk`` package — fewer
deps and the WebSocket protocol is straightforward.

NOTE: The WebSocket URL, query parameter names, and JSON frame schema
follow Deepgram's published API documentation (2024-01 revision).
If Deepgram updates their wire format, adjust the URL building in
``open_stream`` and the frame parsing in ``_DeepgramStream.events``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlencode

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    FinalTranscript,
    PartialTranscript,
    SpeechEnded,
    SpeechStarted,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptionStream,
)

logger = logging.getLogger(__name__)

_DEFAULT_WS_URL = "wss://api.deepgram.com/v1/listen"
_DEFAULT_MODEL = "nova-3"


class _DeepgramStream(TranscriptionStream):
    """A live streaming session backed by a Deepgram WebSocket."""

    def __init__(self, ws: Any) -> None:
        self._ws = ws
        self._closed = False

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        await self._ws.send(chunk)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            # Deepgram expects an empty binary frame to signal end-of-stream.
            await self._ws.send(b"")
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def events(self) -> AsyncIterator[TranscriptionEvent]:
        while True:
            try:
                raw = await self._ws.recv()
            except Exception as exc:  # noqa: BLE001
                if not self._closed:
                    yield TranscriptionError(message=str(exc))
                return
            if raw is None:
                return
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            kind = msg.get("type", "")
            if kind == "Results":
                channel = msg.get("channel", {})
                alternatives = channel.get("alternatives", [])
                if not alternatives:
                    continue
                alt = alternatives[0]
                text = str(alt.get("transcript", ""))
                if not text:
                    continue
                is_final = bool(msg.get("is_final", False))
                start = float(msg.get("start", 0.0))
                dur = float(msg.get("duration", 0.0))
                if is_final:
                    yield FinalTranscript(
                        text=text,
                        start_seconds=start,
                        end_seconds=start + dur,
                        confidence=float(alt.get("confidence", 0.0)) or None,
                    )
                else:
                    yield PartialTranscript(text=text, start_seconds=start)
            elif kind == "SpeechStarted":
                yield SpeechStarted(at_seconds=float(msg.get("timestamp", 0.0)))
            elif kind == "UtteranceEnd":
                yield SpeechEnded(at_seconds=float(msg.get("last_word_end", 0.0)))


class DeepgramBackend(StreamingTranscriptionBackend):
    """Streaming transcription via Deepgram's WebSocket API.

    Auth is via ``Authorization: Token <api_key>`` header.
    Audio is sent as binary frames (PCM16LE, 16 kHz mono by default).
    An empty binary frame signals end-of-stream to the Deepgram server.
    """

    backend_name = "deepgram"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="Deepgram API key.",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description="Deepgram model id.",
                default=_DEFAULT_MODEL,
                choices=("nova-3", "nova-2", "enhanced", "base"),
            ),
            ConfigParam(
                key="ws_url",
                type=ToolParameterType.STRING,
                description="WebSocket URL (default wss://api.deepgram.com/v1/listen).",
                default=_DEFAULT_WS_URL,
            ),
        ]

    def __init__(self) -> None:
        self._api_key = ""
        self._model = _DEFAULT_MODEL
        self._ws_url = _DEFAULT_WS_URL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_MODEL))
        self._ws_url = str(config.get("ws_url", _DEFAULT_WS_URL))
        if not self._api_key:
            logger.warning("deepgram initialized without api_key — calls will fail")

    async def close(self) -> None:
        pass

    async def open_stream(self, config: StreamConfig) -> TranscriptionStream:
        import ssl

        import certifi
        import websockets  # deferred — only needed at stream-open time

        params: dict[str, Any] = {
            "model": self._model,
            "encoding": "linear16",
            "sample_rate": str(config.format.sample_rate),
            "channels": str(config.format.channels),
            "interim_results": str(config.interim_results).lower(),
            "vad_events": str(config.vad_events).lower(),
        }
        if config.language and config.language != "auto":
            params["language"] = config.language
        if config.diarize:
            params["diarize"] = "true"

        url = f"{self._ws_url}?{urlencode(params)}"
        # Explicit SSL context using certifi's CA bundle — Python's
        # default trust store path doesn't exist on NixOS, so
        # ``websockets.connect`` fails the WSS handshake with
        # ``unable to get local issuer certificate``. certifi ships
        # with the Mozilla bundle and is already a transitive dep.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        ws = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Token {self._api_key}"},
            max_size=None,
            ssl=ssl_context,
        )
        return _DeepgramStream(ws)
