"""LutronBridge — async wrapper around pylutron's sync, threaded API.

pylutron drives a telnet connection from a daemon thread and exposes a
synchronous API (``Output.set_level``, ``Output.level``, etc.) that
internally blocks on threading events. This module wraps it in
``asyncio.to_thread`` so the lights and shades backends can ``await``
everything.

Both ``LutronLights`` and ``LutronShades`` share a single bridge
instance via the module-level ``shared_bridge()`` factory — one telnet
connection per repeater, regardless of how many backends consume it.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pylutron

logger = logging.getLogger(__name__)


class LutronBridge:
    """Owns a single pylutron connection and the discovered topology."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        cache_path: Path | None = None,
    ) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._cache_path = cache_path
        self._lutron: pylutron.Lutron | None = None
        self._lights: list[pylutron.Output] = []
        self._shades: list[pylutron.Shade] = []
        self._area_by_output: dict[int, str] = {}
        self._lights_by_id: dict[str, pylutron.Output] = {}
        self._shades_by_id: dict[str, pylutron.Shade] = {}

    @property
    def connected(self) -> bool:
        return self._lutron is not None

    @property
    def host(self) -> str:
        return self._host

    @property
    def lights(self) -> list[pylutron.Output]:
        return list(self._lights)

    @property
    def shades(self) -> list[pylutron.Shade]:
        return list(self._shades)

    @property
    def project_name(self) -> str:
        return self._lutron.name if self._lutron else ""

    def area_of(self, output: pylutron.Output) -> str:
        return self._area_by_output.get(id(output), "")

    def light_by_id(self, light_id: str) -> pylutron.Output | None:
        return self._lights_by_id.get(light_id)

    def shade_by_id(self, shade_id: str) -> pylutron.Shade | None:
        return self._shades_by_id.get(shade_id)

    async def connect(self) -> None:
        """Connect to the repeater and load the area/output topology."""
        if self._lutron is not None:
            return
        await asyncio.to_thread(self._sync_connect)

    def _sync_connect(self) -> None:
        import pylutron  # imported lazily so tests can patch the module

        lutron = pylutron.Lutron(self._host, self._username, self._password)
        cache = str(self._cache_path) if self._cache_path else None
        lutron.load_xml_db(cache)
        lutron.connect()

        lights: list[pylutron.Output] = []
        shades: list[pylutron.Shade] = []
        area_by_output: dict[int, str] = {}
        lights_by_id: dict[str, pylutron.Output] = {}
        shades_by_id: dict[str, pylutron.Shade] = {}

        for area in lutron.areas:
            for output in area.outputs:
                area_by_output[id(output)] = area.name
                key = str(output.id)
                if isinstance(output, pylutron.Shade):
                    shades.append(output)
                    shades_by_id[key] = output
                else:
                    lights.append(output)
                    lights_by_id[key] = output

        self._lutron = lutron
        self._lights = lights
        self._shades = shades
        self._area_by_output = area_by_output
        self._lights_by_id = lights_by_id
        self._shades_by_id = shades_by_id
        logger.info(
            "lutron connected host=%s project=%r lights=%d shades=%d",
            self._host,
            lutron.name,
            len(lights),
            len(shades),
        )

    async def disconnect(self) -> None:
        """Drop our reference to the pylutron connection.

        pylutron has no public disconnect; its connection thread is a
        daemon and dies at process exit. Releasing the reference is the
        best we can do mid-process.
        """
        self._lutron = None
        self._lights = []
        self._shades = []
        self._area_by_output = {}
        self._lights_by_id = {}
        self._shades_by_id = {}

    async def set_level(self, output: pylutron.Output, level: float) -> None:
        await asyncio.to_thread(output.set_level, level)

    async def get_level(self, output: pylutron.Output) -> float:
        # ``Output.level`` blocks up to ~1s on a query response; keep it
        # off the event loop. Falls back to the cached level on timeout,
        # which is the behavior we want.
        return await asyncio.to_thread(lambda: output.level)

    async def stop_shade(self, shade: pylutron.Shade) -> None:
        await asyncio.to_thread(shade.stop)


# ── Module-level shared bridge ────────────────────────────────────────
#
# Both backends in this plugin connect to the same physical repeater,
# so we keep a single LutronBridge instance at module scope. The first
# backend to ``initialize()`` builds and connects it; the second reuses
# it. ``shared_bridge()`` also handles the (host, user, password)
# changing on a config edit by tearing down and rebuilding.

_BRIDGE: LutronBridge | None = None
_BRIDGE_KEY: tuple[str, str, str] | None = None
_BRIDGE_LOCK: asyncio.Lock | None = None


def _lock() -> asyncio.Lock:
    global _BRIDGE_LOCK
    if _BRIDGE_LOCK is None:
        _BRIDGE_LOCK = asyncio.Lock()
    return _BRIDGE_LOCK


async def shared_bridge(
    host: str,
    username: str,
    password: str,
    cache_path: Path | None = None,
) -> LutronBridge:
    """Return the plugin-shared bridge, building or rebuilding as needed."""
    global _BRIDGE, _BRIDGE_KEY
    new_key = (host, username, password)
    async with _lock():
        if _BRIDGE is not None and new_key != _BRIDGE_KEY:
            await _BRIDGE.disconnect()
            _BRIDGE = None
            _BRIDGE_KEY = None
        if _BRIDGE is None:
            bridge = LutronBridge(host, username, password, cache_path)
            await bridge.connect()
            _BRIDGE = bridge
            _BRIDGE_KEY = new_key
        return _BRIDGE


async def reset_shared_bridge() -> None:
    """Drop the shared bridge — used by tests and on credential changes."""
    global _BRIDGE, _BRIDGE_KEY
    async with _lock():
        if _BRIDGE is not None:
            await _BRIDGE.disconnect()
        _BRIDGE = None
        _BRIDGE_KEY = None
