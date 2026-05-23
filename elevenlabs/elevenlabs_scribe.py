"""ElevenLabs Scribe — speech-to-text (batch and streaming).

Batch uses ``POST /v1/speech-to-text``; streaming uses a WebSocket.

NOTE: The live WebSocket wire shape (URL query params, JSON frame
schema) is a best-guess from ElevenLabs documentation. Live integration
may need adjustment when first exercised against the real endpoint.
The abstraction (open_stream → TranscriptionStream emitting Partial/
FinalTranscript events) is stable; only the parsing inside
``_ScribeLiveStream.events`` and the URL building in ``open_stream``
may need tweaking.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    FinalTranscript,
    PartialTranscript,
    SpeechEnded,
    SpeechStarted,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionError,
    TranscriptionEvent,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionStream,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.elevenlabs.io"
_DEFAULT_BATCH_MODEL = "scribe_v1"
_DEFAULT_LIVE_MODEL = "scribe_v1"
_DEFAULT_LIVE_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/stream"


def _common_config(default_model: str) -> list[ConfigParam]:
    return [
        ConfigParam(
            key="api_key",
            type=ToolParameterType.STRING,
            description="ElevenLabs API key.",
            default="",
            sensitive=True,
        ),
        ConfigParam(
            key="model",
            type=ToolParameterType.STRING,
            description="Scribe model id.",
            default=default_model,
        ),
    ]


class ElevenLabsScribeBackend(BatchTranscriptionBackend):
    """Batch transcription via ElevenLabs /v1/speech-to-text."""

    backend_name = "elevenlabs_scribe"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return _common_config(_DEFAULT_BATCH_MODEL) + [
            ConfigParam(
                key="base_url",
                type=ToolParameterType.STRING,
                description="API base URL (default https://api.elevenlabs.io).",
                default=_DEFAULT_BASE_URL,
            ),
        ]

    def __init__(self) -> None:
        self._api_key = ""
        self._model = _DEFAULT_BATCH_MODEL
        self._base_url = _DEFAULT_BASE_URL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_BATCH_MODEL))
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")

    async def close(self) -> None:
        pass

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        headers = {"xi-api-key": self._api_key}
        files = {"file": ("audio.wav", request.audio, "application/octet-stream")}
        data: dict[str, Any] = {"model_id": self._model}
        if request.language and request.language != "auto":
            data["language_code"] = request.language
        if request.diarize:
            data["diarize"] = "true"

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/v1/speech-to-text",
                    headers=headers,
                    files=files,
                    data=data,
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"elevenlabs_scribe request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise RuntimeError(
                f"elevenlabs_scribe HTTP {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()

        # Scribe returns word-level entries; coalesce into rough sentence
        # segments by splitting on sentence-ending punctuation + length.
        words = payload.get("words", [])
        segments: list[TranscriptSegment] = []
        if words:
            cur_text: list[str] = []
            cur_start = float(words[0].get("start", 0.0))
            cur_end = cur_start
            for w in words:
                t = str(w.get("text", ""))
                cur_text.append(t)
                cur_end = float(w.get("end", cur_end))
                if t.endswith((".", "!", "?")) and len("".join(cur_text)) > 20:
                    segments.append(TranscriptSegment(
                        text="".join(cur_text).strip(),
                        start_seconds=cur_start,
                        end_seconds=cur_end,
                    ))
                    cur_text = []
                    cur_start = cur_end
            if cur_text:
                segments.append(TranscriptSegment(
                    text="".join(cur_text).strip(),
                    start_seconds=cur_start,
                    end_seconds=cur_end,
                ))
        return TranscriptionResult(
            text=str(payload.get("text", "")).strip(),
            segments=segments,
            language=str(payload.get("language_code", "")),
            duration_seconds=None,
            audio_seconds_used=None,
        )

    async def list_languages(self) -> list[str]:
        return ["auto", "en", "es", "fr", "de", "it", "pt", "nl", "ru",
                "zh", "ja", "ko", "ar", "hi", "tr", "pl"]


class _ScribeLiveStream(TranscriptionStream):
    """A live streaming session backed by an ElevenLabs WebSocket."""

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
                msg = json.loads(raw) if isinstance(raw, str | bytes | bytearray) else raw
            except Exception:  # noqa: BLE001
                continue
            kind = msg.get("type", "")
            if kind == "partial":
                yield PartialTranscript(
                    text=str(msg.get("text", "")),
                    start_seconds=float(msg.get("start", 0.0)),
                )
            elif kind == "final":
                yield FinalTranscript(
                    text=str(msg.get("text", "")),
                    start_seconds=float(msg.get("start", 0.0)),
                    end_seconds=float(msg.get("end", 0.0)),
                )
            elif kind == "speech_started":
                yield SpeechStarted(at_seconds=float(msg.get("at", 0.0)))
            elif kind == "speech_ended":
                yield SpeechEnded(at_seconds=float(msg.get("at", 0.0)))
            elif kind == "error":
                yield TranscriptionError(
                    message=str(msg.get("message", "scribe error")),
                    recoverable=False,
                )


class ElevenLabsScribeLiveBackend(StreamingTranscriptionBackend):
    """Streaming Scribe via WebSocket."""

    backend_name = "elevenlabs_scribe_live"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return _common_config(_DEFAULT_LIVE_MODEL) + [
            ConfigParam(
                key="ws_url",
                type=ToolParameterType.STRING,
                description="WebSocket URL for the Scribe live endpoint.",
                default=_DEFAULT_LIVE_WS_URL,
            ),
        ]

    def __init__(self) -> None:
        self._api_key = ""
        self._model = _DEFAULT_LIVE_MODEL
        self._ws_url = _DEFAULT_LIVE_WS_URL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._model = str(config.get("model", _DEFAULT_LIVE_MODEL))
        self._ws_url = str(config.get("ws_url", _DEFAULT_LIVE_WS_URL))

    async def close(self) -> None:
        pass

    async def open_stream(self, config: StreamConfig) -> TranscriptionStream:
        import ssl

        import certifi
        import websockets  # deferred — only needed for streaming

        url = (
            f"{self._ws_url}?model_id={self._model}"
            f"&language_code={config.language or 'auto'}"
        )
        # Pass an explicit SSL context using certifi's CA bundle. On
        # NixOS (and other distros where the system CA path isn't
        # ``/etc/ssl/cert.pem``) Python's default SSL context can't
        # find the trust store and the WSS handshake fails with
        # ``unable to get local issuer certificate``. httpx and the
        # rest of Gilbert's HTTP stack avoid this by going through
        # certifi internally; the websockets library uses Python's
        # default context unless we tell it otherwise.
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        ws = await websockets.connect(
            url,
            additional_headers={"xi-api-key": self._api_key},
            max_size=None,
            ssl=ssl_context,
        )
        return _ScribeLiveStream(ws)
