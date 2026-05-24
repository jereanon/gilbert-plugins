"""Real-model integration tests for KokoroTTSBackend.

Marked ``slow`` because they download (~327MB on first run) and execute
the actual Kokoro pipeline on CPU. Run with:

    uv run pytest std-plugins/kokoro/tests/test_kokoro_integration.py -v -m slow

Skip slow tests with: ``uv run pytest -m "not slow"``.
"""

from __future__ import annotations

import pytest

from gilbert.interfaces.tts import AudioFormat, SynthesisRequest


pytestmark = pytest.mark.slow


async def test_real_synth_mp3() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    await backend.initialize({})
    try:
        result = await backend.synthesize(
            SynthesisRequest(text="Hello.", voice_id="af_heart", output_format=AudioFormat.MP3)
        )
    finally:
        await backend.close()

    # MP3 magic bytes (ID3 tag or MPEG frame sync).
    assert result.audio[:3] == b"ID3" or result.audio[0] == 0xFF
    assert result.format == AudioFormat.MP3
    # "Hello." is short — expect somewhere between 200ms and 3s of audio.
    assert result.duration_seconds is not None
    assert 0.2 <= result.duration_seconds <= 3.0
    assert result.characters_used == len("Hello.")


async def test_real_synth_wav() -> None:
    from gilbert_plugin_kokoro.kokoro_tts import KokoroTTSBackend

    backend = KokoroTTSBackend()
    await backend.initialize({})
    try:
        result = await backend.synthesize(
            SynthesisRequest(text="One two three.", voice_id="bm_george",
                             output_format=AudioFormat.WAV)
        )
    finally:
        await backend.close()

    assert result.audio[:4] == b"RIFF"
    assert result.format == AudioFormat.WAV
