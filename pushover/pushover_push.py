"""Pushover push-notification backend.

Pushover is a one-time-payment app on iOS/Android. The admin creates a
Pushover application once (free for the dev quota; paid otherwise) and
shares its **app token** via ``backend_config_params``. Every user who
wants Pushover notifications enters their personal **user_key** (30
characters from pushover.net) on their route. Optional per-route
``device`` targets a specific device when the user has multiple.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from gilbert.interfaces.configuration import (
    ConfigAction,
    ConfigActionResult,
    ConfigParam,
)
from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryResult,
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 10
_PUSHOVER_MESSAGES_URL = "https://api.pushover.net/1/messages.json"
_PUSHOVER_VALIDATE_URL = "https://api.pushover.net/1/users/validate.json"


_TOKEN_RX = re.compile(
    r"(?:Bearer\s+\S+|/bot[A-Za-z0-9:_-]+/|"
    r"https?://[^\s]*?/api/webhooks/[^/\s]+/[A-Za-z0-9_-]+|"
    r"\?token=[^\s&]+)",
    re.IGNORECASE,
)


def _safe_repr(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _TOKEN_RX.sub("<redacted>", text)


def _priority_for_urgency(urgency: NotificationUrgency) -> int:
    return {
        NotificationUrgency.INFO: -1,
        NotificationUrgency.NORMAL: 0,
        NotificationUrgency.URGENT: 1,
    }.get(urgency, 0)


class PushoverPush(PushNotificationBackend):
    backend_name = "pushover"

    def __init__(self) -> None:
        self._api_token: str = ""
        self._timeout: int = _DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_token",
                type=ToolParameterType.STRING,
                description=(
                    "Pushover application API token (admin creates a "
                    "Pushover app once and shares the token across all "
                    "users)."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="timeout",
                type=ToolParameterType.INTEGER,
                description="HTTP timeout in seconds.",
                default=_DEFAULT_TIMEOUT,
            ),
        ]

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="user_key",
                type=ToolParameterType.STRING,
                description=(
                    "Your Pushover user key (30-character string from "
                    "pushover.net)."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="device",
                type=ToolParameterType.STRING,
                description=(
                    "Optional device name to target a specific device. "
                    "Leave empty for all."
                ),
                default="",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Validate API token",
                description=(
                    "Calls Pushover's /users/validate.json with the "
                    "configured token and a user_key from the payload."
                ),
            ),
        ]

    async def invoke_backend_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if key == "test_connection":
            user_key = str(payload.get("user_key", "")).strip()
            if not user_key:
                return ConfigActionResult(
                    status="error",
                    message="Provide 'user_key' in the payload.",
                )
            return await self._action_test(user_key)
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}"
        )

    async def _action_test(self, user_key: str) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message="pushover backend not initialised — save settings first.",
            )
        if not self._api_token:
            return ConfigActionResult(
                status="error",
                message="Pushover api_token is not configured.",
            )
        try:
            resp = await self._client.post(
                _PUSHOVER_VALIDATE_URL,
                data={"token": self._api_token, "user": user_key},
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {_safe_repr(exc)}",
            )
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if payload.get("status") == 1:
                devices = payload.get("devices") or []
                return ConfigActionResult(
                    status="ok",
                    message=(
                        f"Pushover validated user_key — {len(devices)} "
                        "device(s)."
                    ),
                )
        return ConfigActionResult(
            status="error",
            message=f"Pushover returned HTTP {resp.status_code}.",
        )

    async def initialize(self, config: dict[str, Any]) -> None:
        self._api_token = str(config.get("api_token", "") or "")
        self._timeout = int(config.get("timeout", _DEFAULT_TIMEOUT))
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        if self._client is None:
            return PushDeliveryResult(
                status=PushDeliveryStatus.DISABLED,
                message="pushover backend not initialised",
            )
        if not self._api_token:
            return PushDeliveryResult(
                status=PushDeliveryStatus.DISABLED,
                message="pushover api_token not configured",
            )
        user_key = str(destination.data.get("user_key", "")).strip()
        if not user_key:
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message="route is missing 'user_key'",
            )
        device = str(destination.data.get("device", "") or "").strip()
        priority = _priority_for_urgency(message.urgency)
        deep_link = ""
        if message.source_ref and isinstance(message.source_ref, dict):
            link = message.source_ref.get("deep_link_url")
            if isinstance(link, str):
                deep_link = link
        body: dict[str, Any] = {
            "token": self._api_token,
            "user": user_key,
            "title": message.title,
            "message": message.body,
            "priority": priority,
        }
        if device:
            body["device"] = device
        if deep_link:
            body["url"] = deep_link
            body["url_title"] = "Open in Gilbert"
        try:
            resp = await self._client.post(_PUSHOVER_MESSAGES_URL, data=body)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message=f"network error ({type(exc).__name__})",
            )
        except Exception as exc:
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message=_safe_repr(exc),
            )
        if 500 <= resp.status_code < 600:
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message=f"server HTTP {resp.status_code}",
            )
        if resp.status_code == 200:
            try:
                payload = resp.json()
            except Exception:
                payload = {}
            if payload.get("status") == 1:
                return PushDeliveryResult(
                    status=PushDeliveryStatus.DELIVERED,
                    message="HTTP 200",
                    provider_message_id=str(
                        payload.get("request") or ""
                    ),
                )
            # Pushover returns HTTP 200 with status=0 on auth failure.
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message="pushover rejected message",
            )
        return PushDeliveryResult(
            status=PushDeliveryStatus.REJECTED,
            message=f"HTTP {resp.status_code}",
        )

