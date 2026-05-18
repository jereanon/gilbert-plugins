"""openWakeWord — fully local wake-word detection (no API key).

Uses ONNX-based pretrained wake-word models that ship with the
``openwakeword`` package. No API key or internet access required.

Audio must be 16-bit little-endian PCM at 16 kHz mono. openWakeWord
expects 80ms windows (1280 samples per frame). Incoming audio chunks are
buffered until a full frame is available, then passed to
``Model.predict(np.array)``.

NOTE: ``from openwakeword.model import Model`` is deferred to
``open_detector()`` so importing this module does not fail when the
``openwakeword`` package is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)

logger = logging.getLogger(__name__)

# openWakeWord works on 80ms windows at 16 kHz:
# 1280 samples × 2 bytes (int16) = 2560 bytes per frame.
_FRAME_SAMPLES = 1280
_FRAME_BYTES = _FRAME_SAMPLES * 2
_SAMPLE_RATE = 16000


class _OWWDetector(WakeWordDetector):
    """Buffers incoming PCM chunks and feeds openWakeWord fixed-size frames."""

    def __init__(self, model: Any, keywords: list[str], threshold: float) -> None:
        self._model = model
        self._keywords = keywords
        self._threshold = threshold
        self._buf = bytearray()
        self._queue: asyncio.Queue[WakeEvent | None] = asyncio.Queue()
        self._closed = False
        self._sample_count = 0

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        self._buf.extend(chunk)
        while len(self._buf) >= _FRAME_BYTES:
            frame = bytes(self._buf[:_FRAME_BYTES])
            del self._buf[:_FRAME_BYTES]
            self._sample_count += _FRAME_SAMPLES
            arr = np.frombuffer(frame, dtype=np.int16)
            scores: dict[str, float] = self._model.predict(arr)
            for name, score in scores.items():
                if score >= self._threshold and name in self._keywords:
                    at = self._sample_count / _SAMPLE_RATE
                    await self._queue.put(WakeEvent(
                        keyword=name,
                        at_seconds=at,
                        confidence=float(score),
                    ))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)

    async def events(self) -> AsyncIterator[WakeEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class OpenWakeWordBackend(WakeWordBackend):
    """Local wake-word detection via openWakeWord (no API key required).

    Uses pretrained ONNX models bundled with the ``openwakeword`` package
    by default. Custom ``.onnx`` model files can be supplied via the
    ``model_paths`` config (comma-separated paths).
    """

    backend_name = "openwakeword"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="model_paths",
                type=ToolParameterType.STRING,
                description=(
                    "Comma-separated paths to .onnx wake-word models. "
                    "Leave empty to use the bundled pretrained models."
                ),
                default="",
            ),
        ]

    def __init__(self) -> None:
        self._model_paths: list[str] = []

    async def initialize(self, config: dict[str, object]) -> None:
        raw = str(config.get("model_paths", "")).strip()
        self._model_paths = [p.strip() for p in raw.split(",") if p.strip()]

    async def close(self) -> None:
        pass

    async def open_detector(self, config: WakeWordConfig) -> WakeWordDetector:
        from openwakeword.model import Model  # deferred — only at detector-open time

        kwargs: dict[str, Any] = {}
        if self._model_paths:
            kwargs["wakeword_models"] = self._model_paths
        # If no model_paths provided, Model() loads the bundled pretrained set.
        model = Model(**kwargs)
        return _OWWDetector(model, list(config.keywords), config.sensitivity)
