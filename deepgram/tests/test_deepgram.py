"""Tests for the Deepgram streaming transcription backend."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    FinalTranscript,
    PartialTranscript,
    StreamConfig,
    StreamingTranscriptionBackend,
    TranscriptionStream,
)


def test_backend_is_registered():
    from gilbert_plugin_deepgram import deepgram  # noqa: F401

    assert "deepgram" in StreamingTranscriptionBackend.registered_backends()


@pytest.fixture
def backend():
    from gilbert_plugin_deepgram.deepgram import DeepgramBackend

    return DeepgramBackend()


def test_config_params_include_api_key(backend):
    keys = {p.key for p in backend.backend_config_params()}
    assert "api_key" in keys
    assert "model" in keys
    api_key = next(p for p in backend.backend_config_params() if p.key == "api_key")
    assert api_key.sensitive is True


@pytest.mark.asyncio
async def test_open_stream_returns_stream(backend):
    await backend.initialize({"api_key": "dg-test", "model": "nova-3"})

    class _FakeWs:
        def __init__(self):
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
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)) as mock_connect:
        stream = await backend.open_stream(StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
            language="en",
            interim_results=True,
            vad_events=True,
        ))
        assert isinstance(stream, TranscriptionStream)

        # Verify the URL was built with the expected query params and auth header.
        call = mock_connect.call_args
        url = call.args[0]
        assert "encoding=linear16" in url
        assert "sample_rate=16000" in url
        assert "language=en" in url
        assert call.kwargs["additional_headers"]["Authorization"] == "Token dg-test"

        await stream.close()


@pytest.mark.asyncio
async def test_stream_emits_partial_then_final(backend):
    await backend.initialize({"api_key": "dg-test"})

    class _FakeWs:
        def __init__(self):
            self._queue: asyncio.Queue = asyncio.Queue()
            self.sent: list = []

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
        stream = await backend.open_stream(StreamConfig(
            format=AudioFormat(AudioEncoding.PCM_S16LE),
        ))

        # Push a partial then a final from the vendor (Deepgram "Results" frame shape).
        fake_ws.push(json.dumps({
            "type": "Results",
            "is_final": False,
            "start": 0.0,
            "duration": 0.3,
            "channel": {"alternatives": [{"transcript": "hel", "confidence": 0.5}]},
        }))
        fake_ws.push(json.dumps({
            "type": "Results",
            "is_final": True,
            "start": 0.0,
            "duration": 0.5,
            "channel": {"alternatives": [{"transcript": "hello", "confidence": 0.97}]},
        }))

        events: list = []

        async def _drain():
            count = 0
            async for ev in stream.events():
                events.append(ev)
                count += 1
                if count >= 2:
                    break

        await asyncio.wait_for(_drain(), timeout=1.0)

        assert any(isinstance(e, PartialTranscript) and e.text == "hel" for e in events)
        assert any(isinstance(e, FinalTranscript) and e.text == "hello" for e in events)

        await stream.close()


@pytest.mark.asyncio
async def test_stream_send_forwards_to_ws(backend):
    await backend.initialize({"api_key": "dg-test"})

    class _FakeWs:
        def __init__(self):
            self.sent: list = []
            self._queue: asyncio.Queue = asyncio.Queue()

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            return await self._queue.get()

        async def close(self):
            pass

    fake_ws = _FakeWs()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        stream = await backend.open_stream(StreamConfig(format=AudioFormat(AudioEncoding.PCM_S16LE)))
        await stream.send(b"\x00\x00audio1")
        await stream.send(b"\x00\x00audio2")
        await stream.close()

    assert fake_ws.sent[0] == b"\x00\x00audio1"
    assert fake_ws.sent[1] == b"\x00\x00audio2"
    # close() sends an empty binary frame as end-of-stream
    assert b"" in fake_ws.sent
