"""Tests for the Porcupine wake-word backend."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

pvporcupine = pytest.importorskip("pvporcupine")

from gilbert.interfaces.transcription import (  # noqa: E402
    AudioEncoding,
    AudioFormat,
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)


def test_backend_is_registered():
    from gilbert_plugin_porcupine import porcupine  # noqa: F401

    assert "porcupine" in WakeWordBackend.registered_backends()


@pytest.fixture
def backend():
    from gilbert_plugin_porcupine.porcupine import PorcupineBackend

    return PorcupineBackend()


def test_config_params_include_access_key(backend):
    keys = {p.key for p in backend.backend_config_params()}
    assert "access_key" in keys
    access_key = next(p for p in backend.backend_config_params() if p.key == "access_key")
    assert access_key.sensitive is True


@pytest.mark.asyncio
async def test_open_detector_returns_detector(backend):
    await backend.initialize({"access_key": "pv-test"})

    # Mock pvporcupine to return a fake porcupine instance.
    fake_p = MagicMock()
    fake_p.frame_length = 512
    fake_p.sample_rate = 16000
    fake_p.process = MagicMock(return_value=-1)  # no wake

    with patch("pvporcupine.create", return_value=fake_p):
        detector = await backend.open_detector(WakeWordConfig(
            keywords=["computer"],
            format=AudioFormat(AudioEncoding.PCM_S16LE, sample_rate=16000, channels=1),
        ))
        assert isinstance(detector, WakeWordDetector)
        await detector.close()


@pytest.mark.asyncio
async def test_detector_emits_wake_event_on_positive_match(backend):
    await backend.initialize({"access_key": "pv-test"})

    fake_p = MagicMock()
    fake_p.frame_length = 512
    fake_p.sample_rate = 16000
    # Return -1 on first frame, 0 (keyword index) on second.
    fake_p.process = MagicMock(side_effect=[-1, 0])

    with patch("pvporcupine.create", return_value=fake_p):
        detector = await backend.open_detector(WakeWordConfig(
            keywords=["computer"],
            format=AudioFormat(AudioEncoding.PCM_S16LE),
        ))

        # 512 samples * 2 bytes = 1024 bytes per frame; send 2 frames worth.
        chunk = b"\x00\x00" * 512
        await detector.send(chunk)  # first frame: no wake
        await detector.send(chunk)  # second frame: keyword index 0

        events: list = []

        async def _drain():
            async for ev in detector.events():
                events.append(ev)
                if len(events) >= 1:
                    return

        await asyncio.wait_for(_drain(), timeout=1.0)

        assert len(events) == 1
        assert isinstance(events[0], WakeEvent)
        assert events[0].keyword == "computer"

        await detector.close()


@pytest.mark.asyncio
async def test_detector_buffers_partial_chunks(backend):
    """If a caller sends a partial frame, the detector should buffer
    and only call porcupine.process when a full frame is available."""
    await backend.initialize({"access_key": "pv-test"})

    fake_p = MagicMock()
    fake_p.frame_length = 512
    fake_p.sample_rate = 16000
    fake_p.process = MagicMock(return_value=-1)

    with patch("pvporcupine.create", return_value=fake_p):
        detector = await backend.open_detector(WakeWordConfig(
            keywords=["computer"],
            format=AudioFormat(AudioEncoding.PCM_S16LE),
        ))

        # Send half a frame's worth of bytes (256 samples * 2 = 512 bytes).
        await detector.send(b"\x00" * 512)
        assert fake_p.process.call_count == 0  # not enough for a frame

        # Send the other half.
        await detector.send(b"\x00" * 512)
        assert fake_p.process.call_count == 1  # now we have a full frame

        await detector.close()
