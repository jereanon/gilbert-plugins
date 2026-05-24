"""openWakeWord — fully local wake-word detection (no API key).

Ships a custom ``hey_gilbert`` ONNX model under ``models/hey_gilbert.onnx``,
which is the default when no ``model_paths`` are configured. Audio must be
16-bit little-endian PCM at 16 kHz mono. openWakeWord expects 80 ms windows
(1280 samples per frame); incoming audio chunks are buffered until a full
frame is available, then passed to ``Model.predict(np.array)``.

The feature-extraction models (``melspectrogram.onnx``, ``embedding_model.onnx``,
``silero_vad.onnx``) are downloaded by the ``openwakeword`` library on first
use into its own cache directory. They are not bundled here.

NOTE: ``from openwakeword.model import Model`` is deferred to
``open_detector()`` so importing this module does not fail when the
``openwakeword`` package is unavailable.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
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

# openWakeWord works on 80 ms windows at 16 kHz:
# 1280 samples × 2 bytes (int16) = 2560 bytes per frame.
_FRAME_SAMPLES = 1280
_FRAME_BYTES = _FRAME_SAMPLES * 2
_SAMPLE_RATE = 16000

# Path to the bundled "hey gilbert" model that ships with this plugin.
# Resolved relative to this module so it works regardless of how the
# plugin was loaded.
_BUNDLED_MODELS_DIR = Path(__file__).parent / "models"
_BUNDLED_HEY_GILBERT = _BUNDLED_MODELS_DIR / "hey_gilbert.onnx"

# Score-key the bundled model produces (filename stem with underscore).
# Documented here so callers know what to put in ``WakeWordConfig.keywords``.
BUNDLED_KEYWORD = "hey_gilbert"


def _default_model_paths() -> str:
    """Comma-separated default for the ``model_paths`` config field.

    Points at the bundled ``hey_gilbert.onnx`` so the backend works out of
    the box. Returns an empty string if the file is missing (lets the user
    fall back to openwakeword's bundled pretrained set).
    """
    if _BUNDLED_HEY_GILBERT.exists():
        return str(_BUNDLED_HEY_GILBERT)
    return ""


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

    Ships a custom ``hey_gilbert`` model — enable the backend and the
    default ``model_paths`` points at ``models/hey_gilbert.onnx`` inside
    this plugin. Callers pass ``"hey_gilbert"`` in ``WakeWordConfig.keywords``
    to receive ``WakeEvent`` notifications when it fires.

    To use additional or alternative wake-word models, set the
    ``model_paths`` config field to a comma-separated list of absolute
    ``.onnx`` paths. Setting it to an empty string falls back to
    openwakeword's bundled pretrained set (``hey_jarvis``, ``alexa``, etc.).
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
                    "Defaults to the bundled 'hey_gilbert' model that ships "
                    "with this plugin. Set to empty to fall back to the "
                    "openwakeword library's bundled pretrained set."
                ),
                default=_default_model_paths(),
            ),
            ConfigParam(
                key="inference_framework",
                type=ToolParameterType.STRING,
                description=(
                    "Inference runtime for the wake-word model. 'onnx' uses "
                    "onnxruntime (works on Python 3.12+); 'tflite' uses "
                    "tflite-runtime (faster on some hardware but no wheels "
                    "for Python 3.12+ yet)."
                ),
                default="onnx",
                choices=("onnx", "tflite"),
            ),
        ]

    def __init__(self) -> None:
        self._model_paths: list[str] = []
        self._inference_framework: str = "onnx"

    async def initialize(self, config: dict[str, object]) -> None:
        raw = str(config.get("model_paths", _default_model_paths())).strip()
        self._model_paths = [p.strip() for p in raw.split(",") if p.strip()]
        self._inference_framework = str(config.get("inference_framework", "onnx"))

    async def close(self) -> None:
        pass

    async def open_detector(self, config: WakeWordConfig) -> WakeWordDetector:
        from openwakeword.model import Model  # deferred — only at detector-open time
        from openwakeword.utils import download_models

        # The library ships its custom wake-word ONNX models but NOT
        # the feature-extraction ones it needs to actually run
        # (``melspectrogram.onnx``, ``embedding_model.onnx``,
        # ``silero_vad.onnx``). They're downloaded on demand into the
        # package's resources/models directory. On a fresh deploy
        # that directory is empty, so ``Model(...)`` crashes with
        # ``NoSuchFile``. Trigger the download up front if any of
        # the expected files are missing. ``download_models`` is
        # idempotent and a no-op when everything's already present,
        # so it's cheap to call every time.
        import openwakeword

        resources_dir = (
            Path(openwakeword.__file__).parent / "resources" / "models"
        )
        feature_files = (
            "melspectrogram.onnx",
            "embedding_model.onnx",
            "silero_vad.onnx",
        )
        missing = [
            f for f in feature_files if not (resources_dir / f).exists()
        ]
        if missing:
            logger.info(
                "openWakeWord feature models missing %s — running "
                "download_models() (one-time, ~10 MB)",
                missing,
            )
            await asyncio.to_thread(download_models)
            logger.info("openWakeWord feature models downloaded")

        kwargs: dict[str, Any] = {"inference_framework": self._inference_framework}
        if self._model_paths:
            kwargs["wakeword_models"] = self._model_paths
        # If no model_paths provided, Model() loads the library's bundled set.
        model = Model(**kwargs)
        return _OWWDetector(model, list(config.keywords), config.sensitivity)
