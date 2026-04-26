"""LutronLights — LightsBackend implementation backed by pylutron / RadioRA 2."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.lights import LightInfo, LightsBackend
from gilbert.interfaces.tools import ToolParameterType

from .bridge import LutronBridge, reset_shared_bridge, shared_bridge

logger = logging.getLogger(__name__)


class LutronLights(LightsBackend):
    """RadioRA 2 / HomeWorks lights backend.

    Reads host/user/password from the lights service's
    ``settings`` config section, connects via pylutron, and surfaces
    every non-shade output as a ``LightInfo``.
    """

    backend_name = "lutron-radiora"
    supports_dimming = True

    def __init__(self) -> None:
        self._host = ""
        self._username = ""
        self._password = ""
        self._cache_dir: Path | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="host",
                type=ToolParameterType.STRING,
                description="Hostname or IP of the RadioRA 2 / HomeWorks main repeater.",
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
                description=(
                    "Connect to the RadioRA repeater and load its area/output "
                    "topology. Reports light + shade counts on success."
                ),
                required_role="admin",
            ),
        ]

    async def initialize(self, config: dict[str, object]) -> None:
        self._host = str(config.get("host", "") or "")
        self._username = str(config.get("username", "") or "")
        self._password = str(config.get("password", "") or "")
        # The bridge will be (re)built lazily on the first call. If
        # credentials changed, ``shared_bridge`` tears down the old one.
        if self._host and self._username and self._password:
            try:
                await shared_bridge(
                    self._host,
                    self._username,
                    self._password,
                    cache_path=self._cache_path(),
                )
            except Exception:
                logger.exception("LutronLights: failed to connect to repeater")
        logger.info(
            "LutronLights initialized (host=%s, configured=%s)",
            self._host,
            bool(self._host and self._username and self._password),
        )

    async def close(self) -> None:
        # The bridge is shared with LutronShades; resetting only on
        # full plugin teardown keeps both backends consistent.
        await reset_shared_bridge()

    def _cache_path(self) -> Path | None:
        return self._cache_dir / "lutron-db.xml" if self._cache_dir else None

    async def _bridge(self) -> LutronBridge:
        if not (self._host and self._username and self._password):
            raise RuntimeError("LutronLights is not configured.")
        return await shared_bridge(
            self._host,
            self._username,
            self._password,
            cache_path=self._cache_path(),
        )

    async def list_lights(self) -> list[LightInfo]:
        try:
            bridge = await self._bridge()
        except Exception:
            return []
        out: list[LightInfo] = []
        for output in bridge.lights:
            out.append(
                LightInfo(
                    light_id=str(output.id),
                    name=output.name,
                    area=bridge.area_of(output),
                    supports_dimming=bool(output.is_dimmable),
                    level=float(output.last_level()),
                )
            )
        return out

    async def get_level(self, light_id: str) -> float:
        bridge = await self._bridge()
        output = bridge.light_by_id(light_id)
        if output is None:
            raise KeyError(f"Unknown Lutron light: {light_id}")
        return await bridge.get_level(output)

    async def set_level(self, light_id: str, level: float) -> None:
        bridge = await self._bridge()
        output = bridge.light_by_id(light_id)
        if output is None:
            raise KeyError(f"Unknown Lutron light: {light_id}")
        await bridge.set_level(output, max(0.0, min(100.0, float(level))))

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

        # Probe with a fresh, short-lived bridge so we exercise
        # credentials without disturbing the running shared bridge.
        probe = LutronBridge(self._host, self._username, self._password)
        try:
            await probe.connect()
        except Exception as exc:
            logger.exception("LutronLights test_connection failed")
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {exc}",
            )
        message = (
            f"Connected to {probe.project_name or self._host} — "
            f"{len(probe.lights)} lights, {len(probe.shades)} shades."
        )
        await probe.disconnect()
        return ConfigActionResult(status="ok", message=message)
