"""Tests for the OpenAI Whisper batch transcription backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    BatchTranscriptionBackend,
    TranscriptionRequest,
)


@pytest.fixture
def backend():
    """Import inside the fixture so the registry is populated lazily."""
    from gilbert_plugin_openai import openai_whisper

    return openai_whisper.OpenAIWhisperBackend()


def test_backend_is_registered():
    from gilbert_plugin_openai import openai_whisper  # noqa: F401

    assert "openai_whisper" in BatchTranscriptionBackend.registered_backends()


def test_config_params_include_api_key_and_model(backend):
    keys = {p.key for p in backend.backend_config_params()}
    assert "api_key" in keys
    assert "model" in keys
    assert "base_url" in keys
    api_key_param = next(p for p in backend.backend_config_params() if p.key == "api_key")
    assert api_key_param.sensitive is True


@pytest.mark.asyncio
async def test_transcribe_sends_audio_and_returns_text(backend):
    await backend.initialize({
        "api_key": "sk-test",
        "model": "whisper-1",
        "base_url": "https://api.openai.com/v1",
    })

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "text": "hello world",
        "language": "english",
        "duration": 1.5,
        "segments": [
            {"start": 0.0, "end": 0.7, "text": "hello"},
            {"start": 0.7, "end": 1.5, "text": "world"},
        ],
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)) as mock_post:
        result = await backend.transcribe(TranscriptionRequest(
            audio=b"\x00\x00" * 1000,
            format=AudioFormat(AudioEncoding.WAV),
            language="en",
        ))

    assert result.text == "hello world"
    assert len(result.segments) == 2
    assert result.segments[0].text == "hello"
    assert result.language == "english"
    assert result.duration_seconds == 1.5

    call = mock_post.call_args
    assert "/audio/transcriptions" in call.args[0]
    assert "files" in call.kwargs
    assert call.kwargs["headers"]["Authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
async def test_transcribe_4xx_raises_runtime_error_with_message(backend):
    await backend.initialize({"api_key": "sk-test"})

    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = '{"error": {"message": "Invalid API key"}}'

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)):
        with pytest.raises(RuntimeError, match="(?i)401|invalid api key"):
            await backend.transcribe(TranscriptionRequest(
                audio=b"\x00",
                format=AudioFormat(AudioEncoding.WAV),
            ))


@pytest.mark.asyncio
async def test_list_languages_returns_iso_codes(backend):
    langs = await backend.list_languages()
    assert "en" in langs
    assert "auto" in langs
    assert isinstance(langs, list)
