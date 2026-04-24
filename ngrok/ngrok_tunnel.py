"""Ngrok tunnel backend — public HTTPS URLs via pyngrok."""

import asyncio
import logging
from typing import Any

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.tools import ToolParameterType
from gilbert.interfaces.tunnel import TunnelBackend

logger = logging.getLogger(__name__)

# Ngrok returns this when a reserved endpoint is still bound by another
# agent. Most commonly caused by a previous Gilbert process that crashed
# without tearing down its ngrok subprocess — the orphan keeps the
# endpoint reserved until it's killed.
_ENDPOINT_ALREADY_ONLINE = "ERR_NGROK_334"


class NgrokTunnel(TunnelBackend):
    """Tunnel backend using ngrok via pyngrok."""

    backend_name = "ngrok"

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description="Ngrok auth token.",
                sensitive=True,
                restart_required=True,
            ),
            ConfigParam(
                key="domain",
                type=ToolParameterType.STRING,
                description="Custom ngrok domain (e.g., 'myapp.ngrok.io').",
                default="",
                restart_required=True,
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Report the current public tunnel URL (or an error if the tunnel isn't active)."
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
        if not self._public_url or self._tunnel is None:
            return ConfigActionResult(
                status="error",
                message="Ngrok tunnel is not active — enable the service and restart.",
            )
        return ConfigActionResult(
            status="ok",
            message=f"Tunnel is up: {self._public_url}",
            open_url=self._public_url,
        )

    def __init__(self) -> None:
        self._tunnel: Any = None
        self._public_url: str = ""

    async def connect(self, local_port: int, config: dict[str, Any]) -> str:
        from pyngrok import conf, ngrok
        from pyngrok.exception import PyngrokNgrokHTTPError

        api_key = config.get("api_key", "")
        domain = config.get("domain", "")

        if api_key:
            conf.get_default().auth_token = api_key
            logger.info("Ngrok auth token configured")

        options: dict[str, Any] = {"addr": str(local_port)}
        if domain:
            options["domain"] = domain

        try:
            self._tunnel = ngrok.connect(**options)
        except PyngrokNgrokHTTPError as exc:
            if _ENDPOINT_ALREADY_ONLINE not in str(exc):
                raise
            logger.warning(
                "Ngrok endpoint still bound by a previous session — "
                "killing stale ngrok processes and retrying once",
            )
            await self._kill_stale_ngrok()
            # Give ngrok's cloud side a moment to register the agent drop
            # before we try to claim the endpoint again.
            await asyncio.sleep(5)
            self._tunnel = ngrok.connect(**options)

        self._public_url = self._tunnel.public_url

        # Ensure HTTPS
        if self._public_url.startswith("http://"):
            self._public_url = self._public_url.replace("http://", "https://", 1)

        logger.info("Ngrok tunnel started: %s -> localhost:%d", self._public_url, local_port)
        return self._public_url

    async def _kill_stale_ngrok(self) -> None:
        """Tear down lingering ngrok agents that may be holding the endpoint.

        ``pyngrok.ngrok.kill()`` only covers processes started by the
        current Python process's pyngrok session, so it misses orphans
        left behind by a previous Gilbert process that crashed (SIGABRT
        skips the normal teardown path). ``pkill -x ngrok`` handles
        those: ``-x`` requires an exact match on the process name
        ``ngrok``, so Python processes that happen to import pyngrok
        are not affected.
        """
        from pyngrok import ngrok

        try:
            ngrok.kill()
        except Exception:
            logger.debug("pyngrok session kill failed", exc_info=True)

        try:
            proc = await asyncio.create_subprocess_exec(
                "pkill",
                "-9",
                "-x",
                "ngrok",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
        except FileNotFoundError:
            logger.debug("pkill not available on this system — skipping orphan cleanup")
        except Exception:
            logger.debug("pkill ngrok failed", exc_info=True)

    async def disconnect(self) -> None:
        if self._tunnel is not None:
            from pyngrok import ngrok

            try:
                ngrok.disconnect(self._tunnel.public_url)
            except Exception:
                logger.debug("Error disconnecting ngrok tunnel")
            self._tunnel = None
            self._public_url = ""
            logger.info("Ngrok tunnel stopped")
