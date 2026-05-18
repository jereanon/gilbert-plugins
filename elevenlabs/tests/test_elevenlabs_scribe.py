"""Tests for ElevenLabs Scribe batch + streaming transcription backends."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    BatchTranscriptionBackend,
    FinalTranscript,
    PartialTranscript,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionRequest,
    TranscriptionStream,
)


def test_scribe_batch_registered():
    from gilbert_plugin_elevenlabs import elevenlabs_scribe  # noqa: F401

    assert "elevenlabs_scribe" in BatchTranscriptionBackend.registered_backends()


def test_scribe_live_registered():
    from gilbert_plugin_elevenlabs import elevenlabs_scribe  # noqa: F401

    assert "elevenlabs_scribe_live" in StreamingTranscriptionBackend.registered_backends()


@pytest.fixture
def batch():
    from gilbert_plugin_elevenlabs.elevenlabs_scribe import ElevenLabsScribeBackend

    return ElevenLabsScribeBackend()


@pytest.fixture
def live():
    from gilbert_plugin_elevenlabs.elevenlabs_scribe import ElevenLabsScribeLiveBackend

    return ElevenLabsScribeLiveBackend()


def test_batch_config_params_include_api_key(batch):
    keys = {p.key for p in batch.backend_config_params()}
    assert "api_key" in keys
    assert "model" in keys
    api_key = next(p for p in batch.backend_config_params() if p.key == "api_key")
    assert api_key.sensitive is True


@pytest.mark.asyncio
async def test_batch_transcribe_returns_text(batch):
    await batch.initialize({"api_key": "el-test", "model": "scribe_v1"})

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "text": "hello world",
        "language_code": "en",
        "words": [
            {"text": "hello", "start": 0.0, "end": 0.7, "type": "word"},
            {"text": " ", "start": 0.7, "end": 0.75, "type": "spacing"},
            {"text": "world.", "start": 0.75, "end": 1.5, "type": "word"},
        ],
    }

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)) as mock_post:
        result = await batch.transcribe(TranscriptionRequest(
            audio=b"\x00" * 100,
            format=AudioFormat(AudioEncoding.WAV),
            language="en",
        ))

    assert "hello" in result.text.lower()
    assert "world" in result.text.lower()
    assert result.language == "en"
    call = mock_post.call_args
    assert call.kwargs["headers"]["xi-api-key"] == "el-test"


@pytest.mark.asyncio
async def test_batch_4xx_raises_runtime_error(batch):
    await batch.initialize({"api_key": "el-test"})
    fake_response = MagicMock()
    fake_response.status_code = 401
    fake_response.text = '{"detail": "invalid api key"}'

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_response)):
        with pytest.raises(RuntimeError, match="(?i)401|invalid"):
            await batch.transcribe(TranscriptionRequest(
                audio=b"\x00", format=AudioFormat(AudioEncoding.WAV),
            ))


@pytest.mark.asyncio
async def test_streaming_open_returns_stream_and_drains_events(live):
    await live.initialize({"api_key": "el-test"})

    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list = []
            self._queue: asyncio.Queue = asyncio.Queue()

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            item = await self._queue.get()
            if item is None:
                raise ConnectionError("closed")
            return item

        async def close(self):
            pass

        def push(self, frame):
            self._queue.put_nowait(frame)

    fake_ws = _FakeWs()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        stream = await live.open_stream(StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE),
            language="en",
        ))
        assert isinstance(stream, TranscriptionStream)

        fake_ws.push(json.dumps({"type": "partial", "text": "hel"}))
        fake_ws.push(json.dumps({"type": "final", "text": "hello", "start": 0.0, "end": 0.5}))

        events: list = []

        async def _drain():
            count = 0
            async for ev in stream.events():
                events.append(ev)
                count += 1
                if count >= 2:
                    break

        await asyncio.wait_for(_drain(), timeout=1.0)

        assert any(isinstance(e, PartialTranscript) for e in events)
        assert any(isinstance(e, FinalTranscript) for e in events)

        await stream.close()


@pytest.mark.asyncio
async def test_streaming_send_forwards_to_ws(live):
    await live.initialize({"api_key": "el-test"})

    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list = []
            self._queue: asyncio.Queue = asyncio.Queue()
        async def send(self, data): self.sent.append(data)
        async def recv(self): return await self._queue.get()
        async def close(self): pass

    fake_ws = _FakeWs()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        stream = await live.open_stream(StreamConfig(format=AudioFormat(AudioEncoding.PCM_S16LE)))
        await stream.send(b"\x00\x00chunk1")
        await stream.send(b"\x00\x00chunk2")
        await stream.close()

    assert fake_ws.sent[0] == b"\x00\x00chunk1"
    assert fake_ws.sent[1] == b"\x00\x00chunk2"
