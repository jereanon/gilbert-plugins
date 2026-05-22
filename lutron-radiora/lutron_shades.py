"""LutronShades — ShadesBackend implementation backed by pylutron / RadioRA 2."""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.shades import ShadeInfo, ShadesBackend
from gilbert.interfaces.tools import ToolParameterType

from .bridge import LutronBridge, reset_shared_bridge, shared_bridge

logger = logging.getLogger(__name__)


class LutronShades(ShadesBackend):
    """RadioRA 2 / HomeWorks shades backend.

    Surfaces every ``SYSTEM_SHADE`` and ``MOTOR`` output as a
    ``ShadeInfo``. Position is set via the same Output level command
    pylutron uses for dimmers (0=closed, 100=open).
    """

    backend_name = "lutron-radiora"
    supports_position = True
    supports_stop = True

    def __init__(self) -> None:
        import asyncio

        self._host = ""
        self._username = ""
        self._password = ""
        # See lutron_lights for the rationale. Awaitable by tests + close.
        self._warmup_task: asyncio.Task | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="host",
                type=ToolParameterType.STRING,
                description=(
                    "Hostname or IP of the RadioRA 2 / HomeWorks main repeater. "
                    "Use the same value as the lights backend if both are "
                    "served by the same repeater."
                ),
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="username",
                type=ToolParameterType.STRING,
                description="Telnet username (RadioRA 2 default: lutron).",
                default="lutron",
                restart_required=True,
            ),
            ConfigParam(
                key="password",
                type=ToolParameterType.STRING,
                description="Telnet password (RadioRA 2 default: integration).",
                default="integration",
                restart_required=True,
                sensitive=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description="Connect to the RadioRA repeater and report shade counts.",
                required_role="admin",
            ),
        ]

    async def initialize(self, config: dict[str, object]) -> None:
        from gilbert.interfaces.service import background_warmup

        self._host = str(config.get("host", "") or "")
        self._username = str(config.get("username", "") or "")
        self._password = str(config.get("password", "") or "")
        # See lutron_lights.initialize() — same rationale: background the
        # ~15 s telnet handshake. The shared lock in shared_bridge means
        # whoever (lights or shades) wins the race does the real connect
        # and the other reuses it.
        if self._host and self._username and self._password:
            self._warmup_task = background_warmup(
                shared_bridge(self._host, self._username, self._password),
                name="lutron-shades-warmup",
                log=logger,
            )
        logger.info(
            "LutronShades initialized (host=%s, configured=%s, warming=%s)",
            self._host,
            bool(self._host and self._username and self._password),
            bool(self._host and self._username and self._password),
        )

    async def close(self) -> None:
        await reset_shared_bridge()

    async def _bridge(self) -> LutronBridge:
        if not (self._host and self._username and self._password):
            raise RuntimeError("LutronShades is not configured.")
        return await shared_bridge(self._host, self._username, self._password)

    async def list_shades(self) -> list[ShadeInfo]:
        try:
            bridge = await self._bridge()
        except Exception:
            return []
        out: list[ShadeInfo] = []
        for shade in bridge.shades:
            out.append(
                ShadeInfo(
                    shade_id=str(shade.id),
                    name=shade.name,
                    area=bridge.area_of(shade),
                    supports_position=True,
                    supports_stop=True,
                    position=float(shade.last_level()),
                )
            )
        return out

    async def get_position(self, shade_id: str) -> float:
        bridge = await self._bridge()
        shade = bridge.shade_by_id(shade_id)
        if shade is None:
            raise KeyError(f"Unknown Lutron shade: {shade_id}")
        return await bridge.get_level(shade)

    async def set_position(self, shade_id: str, position: float) -> None:
        bridge = await self._bridge()
        shade = bridge.shade_by_id(shade_id)
        if shade is None:
            raise KeyError(f"Unknown Lutron shade: {shade_id}")
        await bridge.set_level(shade, max(0.0, min(100.0, float(position))))

    async def stop(self, shade_id: str) -> None:
        bridge = await self._bridge()
        shade = bridge.shade_by_id(shade_id)
        if shade is None:
            raise KeyError(f"Unknown Lutron shade: {shade_id}")
        await bridge.stop_shade(shade)

    # --- BackendActionProvider ---

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict[str, Any],
    ) -> ConfigActionResult:
        if key != "test_connection":
            return ConfigActionResult(
                status="error",
                message=f"Unknown action: {key}",
            )
        if not (self._host and self._username and self._password):
            return ConfigActionResult(
                status="error",
                message="Fill in host, username, and password (and save) before testing.",
            )
        probe = LutronBridge(self._host, self._username, self._password)
        try:
            await probe.connect()
        except Exception as exc:
            logger.exception("LutronShades test_connection failed")
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        message = (
            f"Connected to {probe.project_name or self._host} — "
            f"{len(probe.shades)} shades, {len(probe.lights)} lights."
        )
        await probe.disconnect()
        return ConfigActionResult(status="ok", message=message)
