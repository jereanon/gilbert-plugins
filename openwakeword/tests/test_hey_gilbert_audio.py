"""Audio-driven smoke test for the bundled ``hey_gilbert.onnx`` model.

Runs the real wake-word detector against pre-generated TTS clips —
``fixtures/hey_gilbert.pcm16_16k.bin`` must fire the wake-word, and
``fixtures/hey_ballsack.pcm16_16k.bin`` must not. The fixtures live in
``tests/fixtures/`` and are committed alongside this test; regenerate
them via::

    uv run python std-plugins/openwakeword/tests/regenerate_fixtures.py

(That script hits ElevenLabs once using the api_key already configured
in ``.gilbert/gilbert.db``.) The test path itself never calls any API
— it just reads the bytes and feeds them through the backend.

Unlike the mocked tests in ``test_openwakeword.py``, this exercises the
actual ONNX model so the bundled wake-word can be verified end-to-end.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gilbert.interfaces.transcription import (
    AudioEncoding,
    AudioFormat,
    WakeEvent,
    WakeWordConfig,
)

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_POSITIVE_FIXTURE = _FIXTURE_DIR / "hey_gilbert.pcm16_16k.bin"
_NEGATIVE_FIXTURE = _FIXTURE_DIR / "hey_ballsack.pcm16_16k.bin"

_REGEN_HINT = (
    "Fixture missing. Run `uv run python "
    "std-plugins/openwakeword/tests/regenerate_fixtures.py` "
    "to (re)generate the audio clips via the configured ElevenLabs voice."
)


@pytest.fixture
def real_openwakeword(monkeypatch):
    """Expose the installed ``openwakeword`` library by hiding the
    std-plugins shadow.

    ``std-plugins/openwakeword/`` is on ``sys.path`` (pytest ``testpaths``)
    so a bare ``import openwakeword`` resolves to the plugin directory's
    empty ``__init__.py`` instead of the installed package — see
    ``conftest.py`` for the full explanation. The mocked tests in
    ``test_openwakeword.py`` work around this by injecting fakes into
    ``sys.modules['openwakeword.model']``; these audio tests need the
    REAL library and so must hide the shadow first.
    """
    monkeypatch.setattr(
        sys,
        "path",
        [p for p in sys.path if "std-plugins" not in p.replace("\\", "/")],
    )
    monkeypatch.delitem(sys.modules, "openwakeword", raising=False)
    monkeypatch.delitem(sys.modules, "openwakeword.model", raising=False)
    monkeypatch.delitem(sys.modules, "openwakeword.utils", raising=False)
    yield
    # monkeypatch restores sys.path and re-installs the shadowed modules.


# Skip the file entirely if the real openwakeword library isn't installed
# (e.g. a minimal CI image). We have to dodge the directory shadow here too.
_saved_path = sys.path[:]
sys.path = [p for p in sys.path if "std-plugins" not in p.replace("\\", "/")]
_saved_oww = sys.modules.pop("openwakeword", None)
try:
    pytest.importorskip("openwakeword")
finally:
    sys.path = _saved_path
    if _saved_oww is not None:
        sys.modules["openwakeword"] = _saved_oww


def _load_fixture(path: Path) -> bytes:
    if not path.exists() or path.stat().st_size == 0:
        pytest.skip(_REGEN_HINT + f" (missing: {path.name})")
    return path.read_bytes()


async def _detect_wakes(audio_pcm16_16k: bytes) -> list[WakeEvent]:
    """Feed PCM through the backend; return any wake events that fired."""
    from gilbert_plugin_openwakeword.oww_backend import (
        OpenWakeWordBackend,
        _default_model_paths,
    )

    backend = OpenWakeWordBackend()
    await backend.initialize({"model_paths": _default_model_paths()})
    detector = await backend.open_detector(
        WakeWordConfig(
            keywords=["hey_gilbert"],
            format=AudioFormat(
                AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1
            ),
            sensitivity=0.5,
        )
    )
    # openwakeword frame = 80 ms at 16 kHz = 1280 samples * 2 bytes
    frame_bytes = 1280 * 2
    for offset in range(0, len(audio_pcm16_16k) - frame_bytes + 1, frame_bytes):
        await detector.send(audio_pcm16_16k[offset : offset + frame_bytes])

    # close() drops a None sentinel onto the queue, terminating events().
    await detector.close()
    events: list[WakeEvent] = []
    async for ev in detector.events():
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_hey_gilbert_audio_fires_wake_event(real_openwakeword):
    """Real TTS for 'hey gilbert' must produce at least one wake hit."""
    audio = _load_fixture(_POSITIVE_FIXTURE)
    wakes = await _detect_wakes(audio)
    assert any(w.keyword == "hey_gilbert" for w in wakes), (
        f"expected the bundled hey_gilbert model to wake on real TTS audio; "
        f"got {wakes}"
    )


@pytest.mark.asyncio
async def test_hey_ballsack_audio_does_not_fire_wake_event(real_openwakeword):
    """Real TTS for an unrelated phrase must not trigger the wake-word."""
    audio = _load_fixture(_NEGATIVE_FIXTURE)
    wakes = await _detect_wakes(audio)
    assert not wakes, (
        f"bundled hey_gilbert model should not wake on 'hey ballsack'; got {wakes}"
    )
