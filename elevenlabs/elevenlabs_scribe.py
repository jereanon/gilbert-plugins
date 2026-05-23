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

import base64
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    AudioEncoding,
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


# Process-wide set of message_type values we've already logged in
# ``Scribe Live: recv …`` lines. Each new value gets one INFO log;
# subsequent occurrences are quiet. Used as a tripwire for protocol
# drift — if ElevenLabs introduces a new message kind we don't yet
# handle, we'll see it once in the journal instead of silently
# dropping it forever.
_SEEN_KINDS: set[str] = set()

_DEFAULT_BASE_URL = "https://api.elevenlabs.io"
_DEFAULT_BATCH_MODEL = "scribe_v1"
# Realtime Scribe lives on a separate endpoint with its own model
# id. The realtime endpoint rejects the batch ``scribe_v1`` model
# (used to fail silently — the WS handshake passed, then every
# session got dropped before transcribing anything). The current
# realtime model is ``scribe_v2_realtime`` — note the suffix order
# (the prefix is ``scribe_v2_`` then ``realtime``, not
# ``scribe_realtime_v2``). ElevenLabs' own ``invalid_request``
# error message dictated the exact spelling.
_DEFAULT_LIVE_MODEL = "scribe_v2_realtime"
_DEFAULT_LIVE_WS_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"


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
    """A live streaming session backed by an ElevenLabs WebSocket.

    Implements ElevenLabs' Scribe Realtime protocol (as documented at
    https://elevenlabs.io/docs/api-reference/speech-to-text/v-1-speech-to-text-realtime).

    Client → server frames are JSON ``input_audio_chunk`` messages
    carrying base64-encoded audio. Server → client frames are JSON
    with ``message_type`` ∈ {``session_started``, ``partial_transcript``,
    ``committed_transcript``, …} plus various error variants. With
    ``commit_strategy=vad`` selected at session creation the server
    automatically commits a transcript when it detects end of speech,
    so we don't need a manual commit signal.
    """

    def __init__(self, ws: Any, sample_rate: int) -> None:
        self._ws = ws
        self._closed = False
        self._sample_rate = sample_rate
        # Telemetry — first send + first recv are the diagnostic
        # gold for "is the pipe actually moving."
        self._sent_count = 0

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        payload = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(chunk).decode("ascii"),
            "sample_rate": self._sample_rate,
        }
        await self._ws.send(json.dumps(payload))
        self._sent_count += 1
        if self._sent_count == 1:
            logger.info(
                "Scribe Live: first audio chunk sent — bytes=%d sample_rate=%d",
                len(chunk),
                self._sample_rate,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._ws.close()

    async def events(self) -> AsyncIterator[TranscriptionEvent]:
        recv_count = 0
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
                logger.warning(
                    "Scribe Live: non-JSON frame from server: %r",
                    raw[:200] if isinstance(raw, (bytes, str)) else raw,
                )
                continue
            # The realtime API uses ``message_type`` as the discriminator
            # (older code looked for ``type`` — that field never exists
            # on the live endpoint and made every message a silent
            # no-op).
            kind = msg.get("message_type") or msg.get("type") or ""
            recv_count += 1
            # Log every distinct message_type the first time we see it
            # so we can spot protocol drift. Also log the first frame
            # unconditionally — its shape tells us whether the API is
            # actually talking to us or we're just sending into a void.
            if recv_count <= 5 or kind not in _SEEN_KINDS:
                _SEEN_KINDS.add(kind)
                # Trim large transcript bodies so the log stays
                # readable; the discriminator + length is enough.
                preview = {
                    k: (v if not isinstance(v, str) else v[:120])
                    for k, v in msg.items()
                    if k != "audio_base_64"
                }
                logger.info(
                    "Scribe Live: recv #%d kind=%r msg=%s",
                    recv_count,
                    kind,
                    preview,
                )
            if kind == "partial_transcript" or kind == "partial":
                yield PartialTranscript(
                    text=str(msg.get("text") or msg.get("transcript") or ""),
                    start_seconds=float(msg.get("start", 0.0)),
                )
            elif kind in (
                "committed_transcript",
                "committed_transcript_with_timestamps",
                "final",
            ):
                yield FinalTranscript(
                    text=str(msg.get("text") or msg.get("transcript") or ""),
                    start_seconds=float(msg.get("start", 0.0)),
                    end_seconds=float(msg.get("end", 0.0)),
                )
            elif kind in ("speech_started", "vad_speech_start"):
                yield SpeechStarted(at_seconds=float(msg.get("at", 0.0)))
            elif kind in ("speech_ended", "vad_speech_end"):
                yield SpeechEnded(at_seconds=float(msg.get("at", 0.0)))
            elif kind == "session_started":
                # Handshake-complete signal; no event for the brain to
                # consume, but useful in logs if we ever wire up
                # session-level diagnostics here.
                continue
            elif kind in (
                "auth_error",
                "quota_exceeded",
                "rate_limited",
                "session_error",
                "invalid_request",
                "error",
            ):
                # ``invalid_request`` is the API's discriminator for
                # bad query params / bad model_id / bad audio_format
                # values. Surface as a non-recoverable error so the
                # listen_loop knows to degrade gracefully — previously
                # this fell through to the silent ``else`` branch and
                # the brain kept happily streaming audio at a server
                # that had already given up on the session.
                yield TranscriptionError(
                    message=(
                        f"{kind}: "
                        f"{msg.get('message') or msg.get('error') or msg.get('detail') or '?'}"
                    ),
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
        from urllib.parse import urlencode

        import certifi
        import websockets  # deferred — only needed for streaming

        # Pick an ``audio_format`` query value that matches what we'll
        # be sending. The realtime endpoint accepts a fixed set of
        # encodings; pick the closest match to the StreamConfig the
        # caller asked for. Falls back to ``pcm_16000`` (the API
        # default) when we can't tell.
        audio_format = _audio_format_for(config.format.encoding, config.format.sample_rate)

        params = {
            "model_id": self._model,
            "audio_format": audio_format,
            # ``commit_strategy=vad`` makes ElevenLabs run server-side
            # voice-activity detection and emit ``committed_transcript``
            # frames at end-of-speech automatically. Without it the
            # client must manually commit (we'd have to bolt VAD on
            # locally), and the brain wouldn't see FinalTranscript
            # events without an explicit signal — which is exactly the
            # symptom the user reported ("Gilbert said his opening line
            # but didn't respond to my question").
            "commit_strategy": "vad",
        }
        if config.language and config.language != "auto":
            params["language_code"] = config.language
        url = f"{self._ws_url}?{urlencode(params)}"

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
        logger.info(
            "ElevenLabs Scribe Live: connected — model=%s audio_format=%s",
            self._model,
            audio_format,
        )
        return _ScribeLiveStream(ws, sample_rate=config.format.sample_rate)


def _audio_format_for(encoding: AudioEncoding, sample_rate: int) -> str:
    """Map our internal ``AudioEncoding`` + sample rate to the value the
    ElevenLabs realtime endpoint expects in its ``audio_format`` query
    param. The API supports a fixed list (``pcm_8000``, ``pcm_16000``,
    ``pcm_22050``, ``pcm_24000``, ``pcm_44100``, ``pcm_48000``,
    ``ulaw_8000``); anything else gets clamped to the closest PCM rate.
    """
    if encoding == AudioEncoding.PCM_S16LE:
        # ElevenLabs only accepts a discrete set of PCM rates.
        for rate in (8000, 16000, 22050, 24000, 44100, 48000):
            if rate >= sample_rate:
                return f"pcm_{rate}"
        return "pcm_48000"
    # Caller asked for something exotic (opus, mp3, mulaw via a custom
    # encoding) — best-effort fall through to mulaw 8k since that's
    # what the phone-call path actually sends (the brain converts to
    # PCM upstream, but a future caller might bypass that).
    return "ulaw_8000"
