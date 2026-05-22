"""Groq Whisper batch transcription backend."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    BatchTranscriptionBackend,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptSegment,
)

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "whisper-large-v3"

_SUPPORTED_LANGUAGES = [
    "auto", "en", "es", "fr", "de", "it", "pt", "nl", "ru",
    "zh", "ja", "ko", "ar", "hi", "tr", "pl", "uk", "sv",
]


class GroqWhisperBackend(BatchTranscriptionBackend):
    """One-shot transcription via Groq's /audio/transcriptions endpoint.

    Groq's transcription API is OpenAI-compatible. Supports
    ``whisper-large-v3``, ``whisper-large-v3-turbo``, and
    ``distil-whisper-large-v3-en``.

    Auth: ``Authorization: Bearer <api_key>``. The api_key here is
    SEPARATE from the sibling ``groq_ai`` backend's key — each STT
    backend has its own config block under
    ``transcription.batch.backends.groq_whisper.settings.*``.
    """

    backend_name = "groq_whisper"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="Groq API key.",
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="base_url",
                type=ToolParameterType.STRING,
                description="API base URL. Override for compatible providers.",
                default=_DEFAULT_BASE_URL,
            ),
            ConfigParam(
                key="model",
                type=ToolParameterType.STRING,
                description="Model id.",
                default=_DEFAULT_MODEL,
                choices=("whisper-large-v3", "whisper-large-v3-turbo", "distil-whisper-large-v3-en"),
            ),
        ]

    def __init__(self) -> None:
        self._api_key: str = ""
        self._base_url: str = _DEFAULT_BASE_URL
        self._model: str = _DEFAULT_MODEL

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key", ""))
        self._base_url = str(config.get("base_url", _DEFAULT_BASE_URL)).rstrip("/")
        self._model = str(config.get("model", _DEFAULT_MODEL))
        if not self._api_key:
            logger.warning("groq_whisper initialized without api_key — calls will fail")

    async def close(self) -> None:
        pass

    async def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        # Pick a filename extension that maps to the encoding we have —
        # Groq sniffs the file but a hint helps when format is AUTO.
        ext_map = {
            "wav": "wav", "mp3": "mp3", "m4a": "m4a",
            "ogg": "ogg", "webm": "webm", "opus": "opus",
            "pcm_s16le": "wav",
            "auto": "wav",
        }
        filename = f"audio.{ext_map.get(request.format.encoding.value, 'wav')}"
        files = {"file": (filename, request.audio, "application/octet-stream")}
        data: dict[str, Any] = {
            "model": self._model,
            "response_format": "verbose_json",
        }
        if request.language and request.language != "auto":
            data["language"] = request.language
        if request.prompt:
            data["prompt"] = request.prompt

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/audio/transcriptions",
                    headers=headers,
                    files=files,
                    data=data,
                )
            except httpx.HTTPError as exc:
                raise RuntimeError(f"groq_whisper request failed: {exc}") from exc

        if resp.status_code >= 400:
            raise RuntimeError(
                f"groq_whisper HTTP {resp.status_code}: {resp.text[:500]}"
            )
        payload = resp.json()
        segments = [
            TranscriptSegment(
                text=str(s.get("text", "")).strip(),
                start_seconds=float(s.get("start", 0.0)),
                end_seconds=float(s.get("end", 0.0)),
                speaker_label="",
                confidence=None,
            )
            for s in payload.get("segments", [])
        ]
        return TranscriptionResult(
            text=str(payload.get("text", "")).strip(),
            segments=segments,
            language=str(payload.get("language", "")),
            duration_seconds=float(payload.get("duration", 0.0)) if payload.get("duration") is not None else None,
            audio_seconds_used=float(payload.get("duration", 0.0)) if payload.get("duration") is not None else None,
        )

    async def list_languages(self) -> list[str]:
        return list(_SUPPORTED_LANGUAGES)
