"""Streaming TTS tests for the ElevenLabs backend."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from gilbert.interfaces.tts import (
    AudioFormat,
    StreamingTTSCapability,
    SynthesisRequest,
)
from gilbert_plugin_elevenlabs.elevenlabs_tts import ElevenLabsTTS


def test_elevenlabs_implements_streaming_capability():
    backend = ElevenLabsTTS()
    assert isinstance(backend, StreamingTTSCapability)


@pytest.mark.asyncio
async def test_synthesize_stream_yields_chunks_via_http_streaming():
    backend = ElevenLabsTTS()

    # Skip the real initialize — fake the client directly.
    backend._voice_id = "v1"
    backend._model_id = "eleven_v3"
    backend._client = MagicMock()

    # Fake httpx streaming response.
    class _Resp:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        def raise_for_status(self): pass
        async def aiter_bytes(self, chunk_size=None):
            for c in (b"AAA", b"BBB", b"CCC"):
                yield c

    backend._client.stream = MagicMock(return_value=_Resp())

    req = SynthesisRequest(text="hello", voice_id="v1", output_format=AudioFormat.MP3)
    chunks: list[bytes] = []
    async for c in backend.synthesize_stream(req):
        chunks.append(c)
    assert chunks == [b"AAA", b"BBB", b"CCC"]
    # Streaming endpoint used — not the non-stream POST.
    backend._client.stream.assert_called_once()
    args, kwargs = backend._client.stream.call_args
    assert args[0] == "POST"
    assert "/text-to-speech/v1/stream" in args[1]


@pytest.mark.slow
@pytest.mark.asyncio
async def test_synthesize_stream_real_api():
    if not os.environ.get("RUN_SLOW"):
        pytest.skip("RUN_SLOW=1 required")
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        pytest.skip("ELEVENLABS_API_KEY required")
    voice_id = os.environ.get("ELEVENLABS_TEST_VOICE_ID", "")
    if not voice_id:
        pytest.skip("ELEVENLABS_TEST_VOICE_ID required")
    backend = ElevenLabsTTS()
    await backend.initialize({"api_key": api_key, "voice_id": voice_id, "model_id": "eleven_v3"})
    try:
        req = SynthesisRequest(text="Hello world.", voice_id=voice_id, output_format=AudioFormat.MP3)
        chunks: list[bytes] = []
        async for c in backend.synthesize_stream(req):
            chunks.append(c)
        # At least 2 chunks and total non-trivial size.
        assert len(chunks) >= 2
        assert sum(len(c) for c in chunks) > 1000
    finally:
        await backend.close()
