"""Streaming TTS tests for the Kokoro backend.

Unit tests patch the pipeline; the integration test (gated) actually
loads the model and synthesizes audio. Run gated tests with:

    RUN_SLOW=1 uv run pytest std-plugins/kokoro/tests/test_kokoro_streaming.py -m slow
"""
from __future__ import annotations

import os
from unittest.mock import patch

import numpy as np
import pytest
from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

from gilbert.interfaces.tts import (
    AudioFormat,
    StreamingTTSCapability,
    SynthesisRequest,
)


def test_kokoro_implements_streaming_capability():
    backend = KokoroTTSBackend()
    assert isinstance(backend, StreamingTTSCapability)


@pytest.mark.asyncio
async def test_synthesize_stream_yields_one_chunk_per_sentence():
    """Three sentences in → three chunks out."""
    backend = KokoroTTSBackend()
    await backend.initialize({"device": "cpu", "default_voice": "af_heart"})

    # Patch the pipeline builder to return a stub.
    class _StubPipeline:
        def __call__(self, text, voice, speed):
            # Yield one fake audio sample per call; the count differs
            # per text so we can assert ordering.
            yield (None, None, np.full(int(len(text) * 10), 0.5, dtype=np.float32))

    with patch("gilbert_plugin_kokoro.kokoro_tts._build_pipeline", return_value=_StubPipeline()):
        req = SynthesisRequest(
            text="First sentence. Second sentence. Third!",
            voice_id="af_heart",
            output_format=AudioFormat.PCM,
        )
        chunks: list[bytes] = []
        async for c in backend.synthesize_stream(req):
            chunks.append(c)
    # Sentence-splitter must produce exactly three non-empty chunks.
    assert len(chunks) == 3
    assert all(len(c) > 0 for c in chunks)


@pytest.mark.asyncio
async def test_synthesize_stream_handles_single_sentence():
    backend = KokoroTTSBackend()
    await backend.initialize({"device": "cpu", "default_voice": "af_heart"})

    class _StubPipeline:
        def __call__(self, text, voice, speed):
            yield (None, None, np.full(100, 0.5, dtype=np.float32))

    with patch("gilbert_plugin_kokoro.kokoro_tts._build_pipeline", return_value=_StubPipeline()):
        req = SynthesisRequest(
            text="Only one sentence",  # no terminal punctuation
            voice_id="af_heart",
            output_format=AudioFormat.PCM,
        )
        chunks: list[bytes] = []
        async for c in backend.synthesize_stream(req):
            chunks.append(c)
    assert len(chunks) == 1


@pytest.mark.slow
@pytest.mark.asyncio
async def test_synthesize_stream_real_model():
    if not os.environ.get("RUN_SLOW"):
        pytest.skip("RUN_SLOW=1 required")
    backend = KokoroTTSBackend()
    await backend.initialize({"device": "cpu", "default_voice": "af_heart"})
    req = SynthesisRequest(
        text="Hello. How are you? I am fine.",
        voice_id="af_heart",
        output_format=AudioFormat.MP3,
    )
    chunks: list[bytes] = []
    async for c in backend.synthesize_stream(req):
        chunks.append(c)
    assert len(chunks) == 3
    assert all(len(c) > 100 for c in chunks)  # each MP3-encoded chunk has a header at minimum
