"""Streaming TTS tests for the ElevenLabs backend."""
from __future__ import annotations

import asyncio
import json as _json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from gilbert_plugin_elevenlabs.elevenlabs_tts import ElevenLabsTTS

from gilbert.interfaces.tts import (
    AudioFormat,
    BidirectionalTTSCapability,
    StreamingTTSCapability,
    SynthesisRequest,
    TTSAudioChunk,
    TTSStream,
    TTSStreamConfig,
    TTSWordTiming,
)


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


def test_elevenlabs_implements_bidirectional_capability():
    backend = ElevenLabsTTS()
    assert isinstance(backend, BidirectionalTTSCapability)


class _FakeWS:
    """Minimal fake of a websockets connection."""

    def __init__(self, scripted_recv: list[str]):
        self.sent: list[str] = []
        self._recv = list(scripted_recv)
        self.closed = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def recv(self) -> str:
        if not self._recv:
            await asyncio.sleep(0.005)
            raise StopAsyncIteration
        return self._recv.pop(0)

    async def close(self, *args, **kwargs) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_open_stream_returns_tts_stream(monkeypatch):
    backend = ElevenLabsTTS()
    backend._api_key = "test-key"
    backend._model_id = "eleven_v3"

    # Fake out the websocket connection.
    fake_ws = _FakeWS(scripted_recv=[])
    import gilbert_plugin_elevenlabs.elevenlabs_tts as mod
    monkeypatch.setattr(mod, "_open_stream_input_ws", AsyncMock(return_value=fake_ws))

    cfg = TTSStreamConfig(voice_id="v1", output_format=AudioFormat.MP3)
    stream = await backend.open_stream(cfg)
    assert isinstance(stream, TTSStream)
    # The priming frame must be the FIRST send and have the exact
    # shape ElevenLabs' stream-input API requires.
    assert fake_ws.sent, "no frames were sent — priming frame is missing"
    priming = _json.loads(fake_ws.sent[0])
    assert priming["text"] == " "
    assert "voice_settings" in priming
    assert "stability" in priming["voice_settings"]
    assert "similarity_boost" in priming["voice_settings"]
    await stream.close()


@pytest.mark.asyncio
async def test_stream_send_text_and_flush_send_correct_frames(monkeypatch):
    backend = ElevenLabsTTS()
    backend._api_key = "test-key"
    backend._model_id = "eleven_v3"

    fake_ws = _FakeWS(scripted_recv=[])
    import gilbert_plugin_elevenlabs.elevenlabs_tts as mod
    monkeypatch.setattr(mod, "_open_stream_input_ws", AsyncMock(return_value=fake_ws))

    cfg = TTSStreamConfig(voice_id="v1")
    stream = await backend.open_stream(cfg)
    await stream.send_text("hello")
    await stream.flush()
    # Last two frames: one with the text, one with the flush marker.
    payloads = [_json.loads(s) for s in fake_ws.sent[-2:]]
    assert payloads[0].get("text") == "hello"
    # ElevenLabs' stream-input WS treats an empty-string text as the flush marker.
    assert payloads[1].get("text") == ""
    assert payloads[1].get("flush") is True
    await stream.close()


@pytest.mark.asyncio
async def test_stream_events_decodes_audio_and_alignment_frames(monkeypatch):
    import base64 as _b64
    backend = ElevenLabsTTS()
    backend._api_key = "test-key"
    backend._model_id = "eleven_v3"

    # Scripted server frames: one audio, one alignment, then close marker.
    fake_ws = _FakeWS(scripted_recv=[
        _json.dumps({"audio": _b64.b64encode(b"BYTES1").decode()}),
        _json.dumps({"normalizedAlignment": {
            "chars": ["h", "i"],
            "charStartTimesMs": [0, 100],
            "charDurationsMs": [80, 200],
        }}),
        _json.dumps({"isFinal": True}),
    ])
    import gilbert_plugin_elevenlabs.elevenlabs_tts as mod
    monkeypatch.setattr(mod, "_open_stream_input_ws", AsyncMock(return_value=fake_ws))

    cfg = TTSStreamConfig(voice_id="v1")
    stream = await backend.open_stream(cfg)
    collected: list = []
    async for ev in stream.events():
        collected.append(ev)
    assert any(isinstance(e, TTSAudioChunk) and e.audio == b"BYTES1" for e in collected)
    assert any(isinstance(e, TTSWordTiming) for e in collected)
    await stream.close()


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
