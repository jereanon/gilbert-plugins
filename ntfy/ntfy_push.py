"""ntfy push-notification backend.

ntfy is a free, simple HTTP-based publish/subscribe service. The user
picks an obscure topic (a path component on the public ntfy.sh server,
or on a self-hosted instance), subscribes from the ntfy mobile/desktop
app, and Gilbert POSTs message bodies to ``<server>/<topic>``.

No API key is required for the public ntfy.sh server. Self-hosted
instances may require a Bearer token; admins set this via
``backend_config_params``. The per-route ``server`` field is optional —
when blank, the admin's ``default_server`` is used so users don't have
to know the URL.
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


_DEFAULT_SERVER = "https://ntfy.sh"
_DEFAULT_TIMEOUT = 10


# Same scrubber as the service uses; kept as a private copy so the
# plugin doesn't import from ``core/services/``.
_TOKEN_RX = re.compile(
    r"(?:Bearer\s+\S+|/bot[A-Za-z0-9:_-]+/|"
    r"https?://[^\s]*?/api/webhooks/[^/\s]+/[A-Za-z0-9_-]+|"
    r"\?token=[^\s&]+)",
    re.IGNORECASE,
)


def _safe_repr(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _TOKEN_RX.sub("<redacted>", text)


def _ntfy_priority(urgency: NotificationUrgency) -> str:
    return {
        NotificationUrgency.INFO: "2",
        NotificationUrgency.NORMAL: "3",
        NotificationUrgency.URGENT: "5",
    }.get(urgency, "3")


def _ntfy_tag_for_source(source: str) -> str:
    # ntfy renders these as emojis. Stay close to existing source tags.
    return {
        "agent": "robot",
        "scheduler": "alarm_clock",
        "inbox": "email",
        "doorbell": "bell",
        "presence": "house",
        "ai": "brain",
        "test": "white_check_mark",
    }.get(source, "bell")


def _header_value(value: str) -> str:
    return value.replace("·", "-").encode("ascii", "replace").decode("ascii")


class NtfyPush(PushNotificationBackend):
    backend_name = "ntfy"

    def __init__(self) -> None:
        self._default_server: str = _DEFAULT_SERVER
        self._auth_token: str = ""
        self._timeout: int = _DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="default_server",
                type=ToolParameterType.STRING,
                description=(
                    "Default ntfy server URL (used when a route doesn't "
                    "override it). Leave at https://ntfy.sh for the "
                    "free public server."
                ),
                default=_DEFAULT_SERVER,
            ),
            ConfigParam(
                key="auth_token",
                type=ToolParameterType.STRING,
                description=(
                    "Optional Bearer token for protected ntfy servers. "
                    "Leave empty for the public ntfy.sh."
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
                key="topic",
                type=ToolParameterType.STRING,
                description=(
                    "ntfy topic (path component). Pick something obscure "
                    "— anyone who guesses it can read your notifications."
                ),
                default="",
            ),
            ConfigParam(
                key="server",
                type=ToolParameterType.STRING,
                description=(
                    "ntfy server URL. Leave empty to use the admin "
                    "default."
                ),
                default="",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test connection",
                description=(
                    "Send 'Gilbert ntfy connectivity test' to a topic of "
                    "your choice. Provide ``topic`` in the payload."
                ),
            ),
        ]

    async def invoke_backend_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if key == "test_connection":
            topic = str(payload.get("topic", "")).strip()
            if not topic:
                # NEVER default to "gilbert-test" — the public ntfy.sh
                # broadcasts to anyone subscribed to that topic.
                return ConfigActionResult(
                    status="error",
                    message=(
                        "Provide a topic in the payload (e.g. a random "
                        "string)."
                    ),
                )
            return await self._action_test_connection(topic)
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}"
        )

    async def _action_test_connection(self, topic: str) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message="ntfy backend not initialised — save settings first.",
            )
        try:
            resp = await self._client.post(
                f"{self._default_server.rstrip('/')}/{topic}",
                content=b"Gilbert ntfy connectivity test",
                headers=self._headers({"Title": "Gilbert - Test"}),
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ConfigActionResult(
                status="error",
                message=f"ntfy returned HTTP {exc.response.status_code}.",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {_safe_repr(exc)}",
            )
        return ConfigActionResult(
            status="ok",
            message=f"ntfy accepted message on topic {topic!r}.",
        )

    async def initialize(self, config: dict[str, Any]) -> None:
        self._default_server = str(
            config.get("default_server", _DEFAULT_SERVER) or _DEFAULT_SERVER
        )
        self._auth_token = str(config.get("auth_token", "") or "")
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
                message="ntfy backend not initialised",
            )

        topic = str(destination.data.get("topic", "")).strip()
        if not topic:
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message="route is missing 'topic'",
            )
        server = (
            str(destination.data.get("server", "")).strip()
            or self._default_server
        )

        priority = _ntfy_priority(message.urgency)
        headers = self._headers(
            {
                "Title": _header_value(message.title),
                "Priority": priority,
                "Tags": _ntfy_tag_for_source(message.source),
            }
        )
        deep_link: str | None = None
        if message.source_ref and isinstance(message.source_ref, dict):
            link = message.source_ref.get("deep_link_url")
            if isinstance(link, str) and link:
                deep_link = link
        if deep_link:
            headers["Click"] = deep_link

        try:
            resp = await self._client.post(
                f"{server.rstrip('/')}/{topic}",
                content=message.body.encode("utf-8"),
                headers=headers,
            )
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

        if 200 <= resp.status_code < 300:
            return PushDeliveryResult(
                status=PushDeliveryStatus.DELIVERED,
                message=f"HTTP {resp.status_code}",
            )
        if 500 <= resp.status_code < 600:
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message=f"server HTTP {resp.status_code}",
            )
        # Status line only — DO NOT include resp.text (may echo URL/topic).
        return PushDeliveryResult(
            status=PushDeliveryStatus.REJECTED,
            message=f"HTTP {resp.status_code}",
        )

    def _headers(self, extras: dict[str, str]) -> dict[str, str]:
        headers = dict(extras)
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers
