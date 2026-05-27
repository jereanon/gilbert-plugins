"""Tests for ElevenLabs Scribe batch + streaming transcription backends."""

from __future__ import annotations

import asyncio
import base64
import contextlib
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
            # The parser yields SpeechStarted + PartialTranscript per
            # non-empty partial frame, plus FinalTranscript for the
            # final frame — three events total for this fixture.
            count = 0
            async for ev in stream.events():
                events.append(ev)
                count += 1
                if count >= 3:
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

    # Audio chunks are wrapped in a Scribe Realtime
    # ``input_audio_chunk`` JSON envelope with base64 payload.
    msg0 = json.loads(fake_ws.sent[0])
    msg1 = json.loads(fake_ws.sent[1])
    assert msg0["message_type"] == "input_audio_chunk"
    assert base64.b64decode(msg0["audio_base_64"]) == b"\x00\x00chunk1"
    assert base64.b64decode(msg1["audio_base_64"]) == b"\x00\x00chunk2"


# ── Diarization (speaker_id) ────────────────────────────────────────


def test_aggregate_speaker_id_majority_vote() -> None:
    """Most-frequent speaker_id across the words[] array wins —
    that's the per-utterance label the engine's classifier filters
    on for echo suppression."""
    from gilbert_plugin_elevenlabs.elevenlabs_scribe import _aggregate_speaker_id

    msg = {
        "words": [
            {"text": "hello", "speaker_id": "1"},
            {"text": "there", "speaker_id": "1"},
            {"text": "friend", "speaker_id": "0"},
        ],
    }
    assert _aggregate_speaker_id(msg) == "1"


def test_aggregate_speaker_id_tie_broken_by_first_appearance() -> None:
    """When two speakers contribute the same number of words to an
    utterance, the one that spoke first wins. Deterministic so a
    classifier downstream can't oscillate between labels."""
    from gilbert_plugin_elevenlabs.elevenlabs_scribe import _aggregate_speaker_id

    msg = {
        "words": [
            {"text": "a", "speaker_id": "2"},
            {"text": "b", "speaker_id": "1"},
            {"text": "c", "speaker_id": "2"},
            {"text": "d", "speaker_id": "1"},
        ],
    }
    assert _aggregate_speaker_id(msg) == "2"


def test_aggregate_speaker_id_returns_empty_when_no_words() -> None:
    """Untimestamped frames don't carry a words[] array — the
    function must return ``""`` so the engine knows not to apply
    its classifier (rather than misclassifying on a missing
    signal)."""
    from gilbert_plugin_elevenlabs.elevenlabs_scribe import _aggregate_speaker_id

    assert _aggregate_speaker_id({"text": "hi"}) == ""
    assert _aggregate_speaker_id({"words": []}) == ""
    # words[] present but no speaker_id fields — same outcome.
    assert (
        _aggregate_speaker_id(
            {"words": [{"text": "hi"}, {"text": "there"}]}
        )
        == ""
    )


@pytest.mark.asyncio
async def test_diarize_true_adds_include_timestamps_query_param(
    live,
) -> None:
    """The realtime endpoint has no documented ``diarize`` param;
    speaker_id only appears in word-level fields when
    ``include_timestamps=true`` is set. The backend must enable
    that automatically when the caller opts into diarization."""
    await live.initialize({"api_key": "el-test"})

    class _FakeWs:
        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()

        async def send(self, _data): pass

        async def recv(self):
            return await self._queue.get()

        async def close(self): pass

    fake_ws = _FakeWs()
    captured: dict = {}

    async def _capture_connect(url, **_kwargs):
        captured["url"] = url
        return fake_ws

    with patch("websockets.connect", new=AsyncMock(side_effect=_capture_connect)):
        stream = await live.open_stream(
            StreamConfig(
                format=AudioFormat(AudioEncoding.PCM_S16LE),
                diarize=True,
            )
        )
        await stream.close()

    assert "include_timestamps=true" in captured["url"]


@pytest.mark.asyncio
async def test_diarize_false_omits_include_timestamps_query_param(
    live,
) -> None:
    """When the caller doesn't ask for diarization we shouldn't pay
    for timestamp metadata we'd throw away."""
    await live.initialize({"api_key": "el-test"})

    class _FakeWs:
        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()

        async def send(self, _data): pass

        async def recv(self):
            return await self._queue.get()

        async def close(self): pass

    fake_ws = _FakeWs()
    captured: dict = {}

    async def _capture_connect(url, **_kwargs):
        captured["url"] = url
        return fake_ws

    with patch("websockets.connect", new=AsyncMock(side_effect=_capture_connect)):
        stream = await live.open_stream(
            StreamConfig(format=AudioFormat(AudioEncoding.PCM_S16LE))
        )
        await stream.close()

    assert "include_timestamps" not in captured["url"]


@pytest.mark.asyncio
async def test_committed_transcript_with_timestamps_populates_speaker_label(
    live,
) -> None:
    """When a ``committed_transcript_with_timestamps`` arrives
    standalone (no preceding plain commit for the same text), it
    must yield a FinalTranscript with the aggregated speaker_label
    set. This is the path that would matter if ElevenLabs ever
    populates per-word speaker_id in realtime."""
    await live.initialize({"api_key": "el-test"})

    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list = []
            self._queue: asyncio.Queue = asyncio.Queue()

        async def send(self, data): self.sent.append(data)

        async def recv(self):
            item = await self._queue.get()
            if item is None:
                raise ConnectionError("closed")
            return item

        async def close(self): pass

        def push(self, frame): self._queue.put_nowait(frame)

    fake_ws = _FakeWs()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        stream = await live.open_stream(
            StreamConfig(
                format=AudioFormat(AudioEncoding.PCM_S16LE),
                diarize=True,
            )
        )

        fake_ws.push(
            json.dumps(
                {
                    "message_type": "committed_transcript_with_timestamps",
                    "text": "hello there",
                    "start": 0.0,
                    "end": 0.7,
                    "words": [
                        {"text": "hello", "speaker_id": "1"},
                        {"text": "there", "speaker_id": "1"},
                    ],
                }
            )
        )

        finals: list = []
        async def _drain():
            async for ev in stream.events():
                if isinstance(ev, FinalTranscript):
                    finals.append(ev)
                    break

        await asyncio.wait_for(_drain(), timeout=1.0)
        assert len(finals) == 1
        assert finals[0].text == "hello there"
        assert finals[0].speaker_label == "1"

        await stream.close()


@pytest.mark.asyncio
async def test_duplicate_committed_transcript_is_suppressed(
    live,
) -> None:
    """Live observation: Scribe Realtime sends BOTH
    ``committed_transcript`` (fast) and
    ``committed_transcript_with_timestamps`` (delayed 20-30s) for the
    same utterance when ``include_timestamps=true``. Without dedupe
    the parser yields two FinalTranscript events and the engine
    dispatches the user's turn twice — Gilbert repeats himself.

    Regression test: a plain commit followed by a timestamped commit
    with the same text + end_seconds must only emit ONE
    FinalTranscript."""
    await live.initialize({"api_key": "el-test"})

    class _FakeWs:
        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()

        async def send(self, data): pass

        async def recv(self):
            item = await self._queue.get()
            if item is None:
                raise ConnectionError("closed")
            return item

        async def close(self): pass

        def push(self, frame): self._queue.put_nowait(frame)

    fake_ws = _FakeWs()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        stream = await live.open_stream(
            StreamConfig(
                format=AudioFormat(AudioEncoding.PCM_S16LE),
                diarize=True,
            )
        )

        # Plain commit first (fast path).
        fake_ws.push(
            json.dumps(
                {
                    "message_type": "committed_transcript",
                    "text": "what time is it",
                    "start": 1.0,
                    "end": 2.5,
                }
            )
        )
        # Same text, same end time, timestamped variant — typical
        # Scribe pattern. Engine MUST NOT see this as a fresh turn.
        fake_ws.push(
            json.dumps(
                {
                    "message_type": "committed_transcript_with_timestamps",
                    "text": "what time is it",
                    "start": 1.0,
                    "end": 2.5,
                    "words": [
                        {"text": "what", "speaker_id": None},
                        {"text": "time", "speaker_id": None},
                        {"text": "is", "speaker_id": None},
                        {"text": "it", "speaker_id": None},
                    ],
                }
            )
        )

        finals: list = []
        async def _drain():
            # Wait long enough to be sure no second final ever fires.
            with contextlib.suppress(asyncio.TimeoutError):
                async with asyncio.timeout(0.3):
                    async for ev in stream.events():
                        if isinstance(ev, FinalTranscript):
                            finals.append(ev)

        await _drain()
        assert len(finals) == 1
        assert finals[0].text == "what time is it"

        await stream.close()


@pytest.mark.asyncio
async def test_same_text_different_end_time_is_not_deduped(
    live,
) -> None:
    """The dedupe key includes ``end_seconds`` so a user who says
    the same words twice in a row (legitimately) still gets two
    dispatches — only the plain+timestamped pairing collapses."""
    await live.initialize({"api_key": "el-test"})

    class _FakeWs:
        def __init__(self) -> None:
            self._queue: asyncio.Queue = asyncio.Queue()

        async def send(self, data): pass

        async def recv(self):
            item = await self._queue.get()
            if item is None:
                raise ConnectionError("closed")
            return item

        async def close(self): pass

        def push(self, frame): self._queue.put_nowait(frame)

    fake_ws = _FakeWs()
    with patch("websockets.connect", new=AsyncMock(return_value=fake_ws)):
        stream = await live.open_stream(
            StreamConfig(format=AudioFormat(AudioEncoding.PCM_S16LE))
        )

        fake_ws.push(
            json.dumps(
                {
                    "message_type": "committed_transcript",
                    "text": "hello",
                    "start": 0.0,
                    "end": 0.5,
                }
            )
        )
        fake_ws.push(
            json.dumps(
                {
                    "message_type": "committed_transcript",
                    "text": "hello",
                    "start": 1.0,
                    "end": 1.5,  # different end → different utterance
                }
            )
        )

        finals: list = []
        async def _drain():
            with contextlib.suppress(asyncio.TimeoutError):
                async with asyncio.timeout(0.3):
                    async for ev in stream.events():
                        if isinstance(ev, FinalTranscript):
                            finals.append(ev)

        await _drain()
        assert len(finals) == 2

        await stream.close()
