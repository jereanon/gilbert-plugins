"""UniFi doorbell backend — Protect doorbell rings + Access intercom presses."""

import asyncio
import logging

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.doorbell import DoorbellBackend, RingEvent
from gilbert.interfaces.tools import ToolParameterType

from .access import UniFiAccess
from .client import (
    UniFiAPIError,
    UniFiAuthError,
    UniFiClient,
    UniFiConnectionError,
)
from .protect import UniFiProtect

logger = logging.getLogger(__name__)


class UniFiProtectDoorbellBackend(DoorbellBackend):
    """Detects entry events from UniFi Protect doorbells and Access readers."""

    backend_name = "unifi"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="host",
                type=ToolParameterType.STRING,
                description="UniFi Protect controller URL.",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="username",
                type=ToolParameterType.STRING,
                description="UniFi Protect username.",
                default="",
                restart_required=True,
            ),
            ConfigParam(
                key="password",
                type=ToolParameterType.STRING,
                description="UniFi Protect password.",
                default="",
                restart_required=True,
                sensitive=True,
            ),
            ConfigParam(
                key="doorbell_names",
                type=ToolParameterType.ARRAY,
                description="Doorbells to monitor (empty = all).",
                default=[],
                choices_from="doorbells",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Verify UniFi Protect credentials by attempting a "
                    "login and listing doorbell cameras."
                ),
            ),
        ]

    async def invoke_backend_action(
        self,
        key: str,
        payload: dict,
    ) -> ConfigActionResult:
        if key == "test_connection":
            return await self._action_test_connection()
        return ConfigActionResult(
            status="error",
            message=f"Unknown action: {key}",
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        """Verify the backend by calling the same method runtime polling uses.

        Intentionally does NOT call ``client.login()`` — ``UniFiClient``
        auto-logs-in on the first request (and on any 401), and that's
        the code path normal doorbell polling exercises. Calling login
        explicitly would test a different thing than what the real
        polling does, and mis-diagnose a live service as broken.
        """
        if self._client is None or self._protect is None:
            return ConfigActionResult(
                status="error",
                message=(
                    "UniFi doorbell backend is not initialized — set host "
                    "and credentials, then save and restart."
                ),
            )
        cameras = []
        camera_err: Exception | None = None
        try:
            cameras = await self._protect.list_cameras()
        except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
            camera_err = exc
        except Exception as exc:
            camera_err = exc

        access_doors: list = []
        access_err: Exception | None = None
        if self._access is not None:
            try:
                access_doors = await self._access.list_doors()
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                access_err = exc
            except Exception as exc:
                access_err = exc

        if camera_err and (access_err or self._access is None):
            return ConfigActionResult(
                status="error",
                message=f"UniFi Protect error: {camera_err}",
            )

        doorbell_count = sum(1 for c in cameras if c.is_doorbell)
        parts = [
            f"{len(cameras)} camera(s)",
            f"{doorbell_count} doorbell(s)",
            f"{len(access_doors)} Access door(s)",
        ]
        message = "Connected to UniFi. " + ", ".join(parts) + "."
        if camera_err:
            message += f" Protect error: {camera_err}."
        if access_err:
            message += f" Access error: {access_err}."
        return ConfigActionResult(
            status="ok" if not (camera_err or access_err) else "error",
            message=message,
        )

    def __init__(self) -> None:
        self._client: UniFiClient | None = None
        self._protect: UniFiProtect | None = None
        self._access: UniFiAccess | None = None

    async def initialize(self, config: dict[str, object]) -> None:
        host = config.get("host")
        if not host:
            logger.warning("UniFi doorbell backend: no host configured")
            return

        username = str(config.get("username", ""))
        password = str(config.get("password", ""))
        if not username or not password:
            logger.warning("UniFi doorbell backend: no credentials configured")
            return

        self._client = UniFiClient(str(host), username, password)
        self._protect = UniFiProtect(self._client)
        self._access = UniFiAccess(self._client)
        logger.info("UniFi doorbell backend initialized (%s)", host)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        self._protect = None
        self._access = None

    async def list_doorbell_names(self) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()

        if self._protect is not None:
            try:
                cameras = await self._protect.list_cameras()
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                logger.warning("UniFi Protect doorbell list unavailable: %s", exc)
            else:
                for c in cameras:
                    if c.is_doorbell and c.name and c.name.lower() not in seen:
                        names.append(c.name)
                        seen.add(c.name.lower())

        if self._access is not None:
            try:
                doors = await self._access.list_doors()
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                logger.warning("UniFi Access door list unavailable: %s", exc)
            else:
                for d in doors:
                    if d.name and d.name.lower() not in seen:
                        names.append(d.name)
                        seen.add(d.name.lower())

        return names

    async def get_ring_events(self, lookback_seconds: int = 10) -> list[RingEvent]:
        if self._protect is None and self._access is None:
            return []

        async def _protect_rings() -> list[RingEvent]:
            if self._protect is None:
                return []
            lookback_minutes = max(1, (lookback_seconds // 60) + 1)
            try:
                events = await self._protect.get_detection_events(
                    lookback_minutes=lookback_minutes,
                    event_types=["ring"],
                )
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                logger.warning("UniFi Protect ring poll failed: %s", exc)
                return []
            return [RingEvent(camera_name=e.camera_name, timestamp=e.start) for e in events]

        async def _access_rings() -> list[RingEvent]:
            if self._access is None:
                return []
            try:
                events = await self._access.get_doorbell_events(
                    lookback_seconds=lookback_seconds,
                )
            except (UniFiAuthError, UniFiConnectionError, UniFiAPIError) as exc:
                logger.warning("UniFi Access ring poll failed: %s", exc)
                return []
            return [RingEvent(camera_name=e.door_name, timestamp=e.timestamp) for e in events]

        protect_rings, access_rings = await asyncio.gather(
            _protect_rings(),
            _access_rings(),
        )
        return [*protect_rings, *access_rings]
