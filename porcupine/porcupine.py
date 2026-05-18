"""Porcupine wake-word detection backend.

Uses the ``pvporcupine`` SDK (Picovoice). Audio must be 16-bit little-endian
PCM at 16 kHz mono. Porcupine works on fixed-size frames (typically 512 samples).
Incoming chunks are buffered until a full frame is available, then passed to
``porcupine.process()`` (a sync C-extension call).

NOTE: The ``pvporcupine`` import is deferred to ``open_detector`` so that
importing this module does not fail when the package is unavailable.
"""

from __future__ import annotations

import array
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.transcription import (
    WakeEvent,
    WakeWordBackend,
    WakeWordConfig,
    WakeWordDetector,
)

logger = logging.getLogger(__name__)


class _PorcupineDetector(WakeWordDetector):
    """Buffers incoming PCM chunks and feeds Porcupine fixed-size frames."""

    def __init__(self, porcupine: Any, keywords: list[str]) -> None:
        self._p = porcupine
        self._keywords = keywords
        self._buf = bytearray()
        self._frame_bytes = porcupine.frame_length * 2  # 16-bit samples → 2 bytes each
        self._queue: asyncio.Queue[WakeEvent | None] = asyncio.Queue()
        self._closed = False
        self._sample_count = 0

    async def send(self, chunk: bytes) -> None:
        if self._closed:
            return
        self._buf.extend(chunk)
        while len(self._buf) >= self._frame_bytes:
            frame_bytes = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            self._sample_count += self._p.frame_length
            # porcupine.process is a sync call — returns keyword index or -1
            frame = array.array("h", frame_bytes)
            idx = self._p.process(frame)
            if idx is not None and idx >= 0:
                kw = self._keywords[idx] if idx < len(self._keywords) else f"kw{idx}"
                at = self._sample_count / self._p.sample_rate
                await self._queue.put(WakeEvent(keyword=kw, at_seconds=at))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)
        with contextlib.suppress(Exception):
            self._p.delete()

    async def events(self) -> AsyncIterator[WakeEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item


class PorcupineBackend(WakeWordBackend):
    """Wake-word detection via Picovoice Porcupine.

    Auth is via a Picovoice access key (https://console.picovoice.ai).
    Free for personal use; commercial use requires a paid licence.
    """

    backend_name = "porcupine"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="access_key",
                type=ToolParameterType.STRING,
                description=(
                    "Picovoice access key. Get one free at https://console.picovoice.ai."
                ),
                default="",
                sensitive=True,
            ),
        ]

    def __init__(self) -> None:
        self._access_key = ""

    async def initialize(self, config: dict[str, object]) -> None:
        self._access_key = str(config.get("access_key", ""))
        if not self._access_key:
            logger.warning("porcupine initialized without access_key — calls will fail")

    async def close(self) -> None:
        pass

    async def open_detector(self, config: WakeWordConfig) -> WakeWordDetector:
        import pvporcupine  # deferred — only needed at detector-open time

        # pvporcupine.create() accepts built-in keyword names (e.g. "computer",
        # "hey google") or paths to custom .ppn model files. We pass whatever
        # strings the caller supplied as keyword names.
        p = pvporcupine.create(
            access_key=self._access_key,
            keywords=list(config.keywords),
            sensitivities=[config.sensitivity] * len(config.keywords),
        )
        return _PorcupineDetector(p, list(config.keywords))
