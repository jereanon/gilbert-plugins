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


def _aggregate_speaker_id(msg: dict[str, Any]) -> str:
    """Reduce the per-word ``speaker_id`` fields in a Scribe Realtime
    ``committed_transcript_with_timestamps`` (or
    ``partial_transcript`` when timestamps are enabled) frame into a
    single utterance-level label.

    Scribe Realtime exposes diarization per-WORD inside the
    ``words[]`` array — there's no dedicated diarize flag on the
    realtime endpoint, only the per-word ids you get back when
    ``include_timestamps=true``. For echo suppression we only need to
    know "is this Gilbert or the user?" so a per-utterance label is
    enough. We pick the most-frequent ``speaker_id`` across the
    utterance's words; ties are broken by first appearance order.

    Returns ``""`` when:
      - the frame doesn't carry a ``words[]`` array (untimestamped
        events / older protocol revs / partials without timestamps),
      - the ``words[]`` array is empty,
      - none of the words carry a ``speaker_id`` field.
    """
    words = msg.get("words")
    if not isinstance(words, list) or not words:
        return ""
    counts: dict[str, int] = {}
    order: list[str] = []
    for w in words:
        if not isinstance(w, dict):
            continue
        sid = w.get("speaker_id")
        if sid is None:
            continue
        sid_str = str(sid)
        if sid_str not in counts:
            order.append(sid_str)
            counts[sid_str] = 0
        counts[sid_str] += 1
    if not counts:
        return ""
    # Majority vote; ties broken by first-appearance index so the
    # result is deterministic given the same input.
    return max(order, key=lambda sid: (counts[sid], -order.index(sid)))


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
        # Dedupe ring for FinalTranscript emission — Scribe sends
        # both committed_transcript (fast) and
        # committed_transcript_with_timestamps (delayed 20-30s) for
        # the same utterance. We keep only the first arrival per
        # (text, end_seconds) pair so the engine doesn't dispatch
        # the same user turn twice. See events() for the full story.
        self._recent_final_keys: list[tuple[str, float]] = []

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
            # Log every transcript event (each one has unique content)
            # plus the first sighting of any other discriminator (so
            # we still catch protocol drift without spamming on
            # session_started / heartbeats / etc.).
            is_transcript = kind in (
                "partial_transcript",
                "committed_transcript",
                "committed_transcript_with_timestamps",
                "final",
                "partial",
            )
            if is_transcript or kind not in _SEEN_KINDS:
                _SEEN_KINDS.add(kind)
                # Trim large bodies for non-transcript frames; for
                # transcripts the ``text`` field IS the payload.
                preview = {
                    k: (v if not isinstance(v, str) else v[:200])
                    for k, v in msg.items()
                    if k != "audio_base_64"
                }
                logger.info(
                    "Scribe Live: recv #%d kind=%r msg=%s",
                    recv_count,
                    kind,
                    preview,
                )

            # Synthesize ``SpeechStarted`` events from
            # ``partial_transcript`` frames. The Scribe Realtime API
            # doesn't have a dedicated speech-start signal — it sends
            # ``partial_transcript`` as it transcribes (sometimes
            # in real-time, sometimes batched against its VAD
            # commit cadence) and a ``committed_transcript`` once the
            # silence threshold elapses. Without this synthesis the
            # phone-call brain's barge-in handler never fires: the
            # user can speak DURING Gilbert's TTS, but the events
            # the brain watches for (``SpeechStarted``) simply don't
            # exist on this protocol.
            #
            # Fire on EVERY non-empty partial (no per-utterance latch).
            # The brain's ``speaking.cancelled = True; audio_out.clear()``
            # is idempotent. When Scribe batches partials and dumps
            # six at once, all six firing SpeechStarted is fine — the
            # first one barges-out, the rest are no-ops. The cost of
            # the redundant signal is a few log lines and that's it.
            if kind in ("partial_transcript", "partial"):
                partial_text = str(msg.get("text") or msg.get("transcript") or "").strip()
                if partial_text:
                    yield SpeechStarted(at_seconds=float(msg.get("at", 0.0)))
            if kind == "partial_transcript" or kind == "partial":
                yield PartialTranscript(
                    text=str(msg.get("text") or msg.get("transcript") or ""),
                    speaker_label=_aggregate_speaker_id(msg),
                    start_seconds=float(msg.get("start", 0.0)),
                )
            elif kind in (
                "committed_transcript",
                "committed_transcript_with_timestamps",
                "final",
            ):
                # Dedupe across the plain + timestamped variants of
                # the same utterance. Scribe Realtime emits BOTH:
                # ``committed_transcript`` fires fast at end-of-VAD,
                # then ``committed_transcript_with_timestamps`` for
                # the same text 20-30s later (verified live —
                # observed gap was 27s). Without dedupe the engine
                # dispatches the user's turn TWICE, producing
                # duplicate AI replies / repeated TTS playback.
                #
                # Keep only the first-arriving variant per (text,
                # end_seconds) tuple. The end-time disambiguator
                # protects against the legitimate case where the
                # user said the same words twice in a row.
                final_text = str(
                    msg.get("text") or msg.get("transcript") or ""
                )
                end_seconds = float(msg.get("end", 0.0))
                dedup_key = (final_text, end_seconds)
                if dedup_key in self._recent_final_keys:
                    logger.info(
                        "Scribe Live: suppressing duplicate final "
                        "(%s) for text=%r end=%.2f",
                        kind,
                        final_text[:80],
                        end_seconds,
                    )
                    continue
                self._recent_final_keys.append(dedup_key)
                # Bounded ring so memory stays flat even across long
                # sessions. 32 deep covers every reasonable
                # plain+timestamped pairing while staying tiny.
                if len(self._recent_final_keys) > 32:
                    self._recent_final_keys.pop(0)
                yield FinalTranscript(
                    text=final_text,
                    start_seconds=float(msg.get("start", 0.0)),
                    end_seconds=end_seconds,
                    speaker_label=_aggregate_speaker_id(msg),
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
            # The API default is 1.5s — too slow for a phone-call brain
            # that needs to barge-in on the user's interruption. Diagnostic
            # showed Scribe was holding *all* partials AND the final
            # commit until 1.5s of silence elapsed; during a continuous
            # Gilbert-and-user-overlap window the user's words wouldn't
            # reach the brain until the very end of Gilbert's TTS. 0.3s
            # is still long enough to absorb normal between-word pauses
            # ("um, so, …") without false-committing mid-thought, but
            # fast enough that interruptions land while Gilbert is still
            # mid-utterance and barge-in actually has something to cancel.
            "vad_silence_threshold_secs": "0.3",
            # min_speech_duration_ms — 100ms (the API default) means a
            # brief cough / "uh" can fire a SpeechStarted. Bump to 200
            # so background noises don't keep canceling Gilbert's TTS.
            "min_speech_duration_ms": "200",
        }
        if config.language and config.language != "auto":
            params["language_code"] = config.language
        # Speaker diarization. Scribe Realtime doesn't have a documented
        # ``diarize`` query param; instead it includes per-word
        # ``speaker_id`` fields in ``committed_transcript_with_timestamps``
        # frames when ``include_timestamps=true``. Opt in only when the
        # caller asked for diarize so we don't pay for timestamp metadata
        # we'd otherwise discard.
        if getattr(config, "diarize", False):
            params["include_timestamps"] = "true"
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
