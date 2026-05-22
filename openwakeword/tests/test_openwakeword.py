"""Tests for the openWakeWord wake-word backend."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# Skip entire file if openwakeword can't be installed (large model deps).
openwakeword = pytest.importorskip("openwakeword")

from gilbert.interfaces.transcription import (  # noqa: E402
    AudioEncoding,
    AudioFormat,
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)


def test_backend_is_registered():
    from gilbert_plugin_openwakeword import oww_backend  # noqa: F401

    assert "openwakeword" in WakeWordBackend.registered_backends()


@pytest.fixture
def backend():
    from gilbert_plugin_openwakeword.oww_backend import OpenWakeWordBackend

    return OpenWakeWordBackend()


def test_config_params_include_model_paths(backend):
    keys = {p.key for p in backend.backend_config_params()}
    assert "model_paths" in keys
    assert "inference_framework" in keys


def test_bundled_hey_gilbert_model_ships_with_plugin():
    """The custom 'hey_gilbert' ONNX model must live alongside the backend."""
    from gilbert_plugin_openwakeword.oww_backend import _BUNDLED_HEY_GILBERT

    assert _BUNDLED_HEY_GILBERT.exists(), (
        f"bundled hey_gilbert.onnx is missing at {_BUNDLED_HEY_GILBERT}"
    )
    # Sanity check it isn't an empty placeholder.
    assert _BUNDLED_HEY_GILBERT.stat().st_size > 1024


def test_default_model_paths_points_at_bundled_model(backend):
    """When no model_paths is configured, the bundled model is the default."""
    from gilbert_plugin_openwakeword.oww_backend import _BUNDLED_HEY_GILBERT

    param = next(p for p in backend.backend_config_params() if p.key == "model_paths")
    assert str(_BUNDLED_HEY_GILBERT) == param.default


@pytest.mark.asyncio
async def test_initialize_with_default_model_paths_loads_bundled(backend):
    """Initializing without specifying model_paths should pick up the bundled model."""
    from gilbert_plugin_openwakeword.oww_backend import _BUNDLED_HEY_GILBERT, _default_model_paths

    # Simulate the framework calling initialize with the param's default value.
    await backend.initialize({"model_paths": _default_model_paths()})

    assert backend._model_paths == [str(_BUNDLED_HEY_GILBERT)]


def _make_fake_model_module(fake_model_instance: MagicMock) -> ModuleType:
    """Return a fake ``openwakeword.model`` module whose ``Model`` is a mock."""
    fake_module = ModuleType("openwakeword.model")
    fake_module.Model = MagicMock(return_value=fake_model_instance)  # type: ignore[attr-defined]
    return fake_module


@pytest.mark.asyncio
async def test_open_detector_returns_detector(backend):
    await backend.initialize({"model_paths": ""})

    fake_model = MagicMock()
    fake_model.predict = MagicMock(return_value={"hey_jarvis": 0.1})

    fake_mod = _make_fake_model_module(fake_model)
    saved = sys.modules.get("openwakeword.model")
    sys.modules["openwakeword.model"] = fake_mod
    try:
        detector = await backend.open_detector(WakeWordConfig(
            keywords=["hey_jarvis"],
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
            sensitivity=0.5,
        ))
        assert isinstance(detector, WakeWordDetector)
        await detector.close()
    finally:
        if saved is None:
            sys.modules.pop("openwakeword.model", None)
        else:
            sys.modules["openwakeword.model"] = saved


@pytest.mark.asyncio
async def test_detector_emits_wake_event_when_score_exceeds_threshold(backend):
    await backend.initialize({"model_paths": ""})

    fake_model = MagicMock()
    # First frame: below threshold. Second frame: above threshold.
    fake_model.predict = MagicMock(side_effect=[
        {"hey_jarvis": 0.1},
        {"hey_jarvis": 0.9},
    ])

    fake_mod = _make_fake_model_module(fake_model)
    saved = sys.modules.get("openwakeword.model")
    sys.modules["openwakeword.model"] = fake_mod
    try:
        detector = await backend.open_detector(WakeWordConfig(
            keywords=["hey_jarvis"],
            format=AudioFormat(AudioEncoding.PCM_S16LE),
            sensitivity=0.5,
        ))

        # 1280 samples * 2 bytes = 2560 bytes per frame.
        frame = b"\x00\x00" * 1280
        await detector.send(frame)  # first frame: 0.1 < 0.5, no wake
        await detector.send(frame)  # second frame: 0.9 >= 0.5, wake

        events: list = []

        async def _drain():
            async for ev in detector.events():
                events.append(ev)
                if len(events) >= 1:
                    return

        await asyncio.wait_for(_drain(), timeout=1.0)

        assert len(events) == 1
        assert isinstance(events[0], WakeEvent)
        assert events[0].keyword == "hey_jarvis"
        assert events[0].confidence is not None and events[0].confidence >= 0.5

        await detector.close()
    finally:
        if saved is None:
            sys.modules.pop("openwakeword.model", None)
        else:
            sys.modules["openwakeword.model"] = saved


@pytest.mark.asyncio
async def test_detector_buffers_partial_frames(backend):
    await backend.initialize({"model_paths": ""})

    fake_model = MagicMock()
    fake_model.predict = MagicMock(return_value={"hey_jarvis": 0.1})

    fake_mod = _make_fake_model_module(fake_model)
    saved = sys.modules.get("openwakeword.model")
    sys.modules["openwakeword.model"] = fake_mod
    try:
        detector = await backend.open_detector(WakeWordConfig(
            keywords=["hey_jarvis"],
            format=AudioFormat(AudioEncoding.PCM_S16LE),
            sensitivity=0.5,
        ))

        # Send half a frame.
        await detector.send(b"\x00" * 1280)
        assert fake_model.predict.call_count == 0

        # Send the other half — now a full frame is available.
        await detector.send(b"\x00" * 1280)
        assert fake_model.predict.call_count == 1

        await detector.close()
    finally:
        if saved is None:
            sys.modules.pop("openwakeword.model", None)
        else:
            sys.modules["openwakeword.model"] = saved
