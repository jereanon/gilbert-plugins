"""Discord webhook push-notification backend.

Discord channel webhooks let any HTTP client post a message into a
specific channel — no shared admin secret is required, the secret is
the per-route webhook URL itself. This backend POSTs a small JSON
payload (with both ``content`` and an embed for rich rendering) and
respects the ``X-RateLimit-Reset-After`` header so the service-level
retry layer sleeps for the right duration on 429s.

**Security guard:** the webhook URL is prefix-validated against the
official ``discord.com`` / ``discordapp.com`` paths to prevent SSRF.
A user (or a misclick) cannot turn this backend into a probe against
internal endpoints.

**Test-message variant:** when ``message.source == "test"`` the JSON
adds ``"flags": 4096`` (SUPPRESS_NOTIFICATIONS) so testing in a shared
channel doesn't ping co-workers. The message body remains visible —
quiet but readable.
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
_DEFAULT_USERNAME = "Gilbert"
_RETRY_AFTER_CAP_S = 60.0
_TEST_FLAGS_SUPPRESS = 4096

# Discord rate-limit reset header is in fractional seconds.
_RATE_LIMIT_RESET_HEADER = "X-RateLimit-Reset-After"

_VALID_PREFIXES: tuple[str, ...] = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
)


_TOKEN_RX = re.compile(
    r"(?:Bearer\s+\S+|/bot[A-Za-z0-9:_-]+/|"
    r"https?://[^\s]*?/api/webhooks/[^/\s]+/[A-Za-z0-9_-]+|"
    r"\?token=[^\s&]+)",
    re.IGNORECASE,
)


def _safe_repr(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _TOKEN_RX.sub("<redacted>", text)


def _color_for_urgency(urgency: NotificationUrgency) -> int:
    return {
        NotificationUrgency.INFO: 0x6E7681,    # gray
        NotificationUrgency.NORMAL: 0xFF8C00,  # amber
        NotificationUrgency.URGENT: 0xCC2222,  # red
    }.get(urgency, 0xFF8C00)


def _is_valid_webhook_url(url: str) -> bool:
    return any(url.startswith(p) for p in _VALID_PREFIXES)


class DiscordWebhookPush(PushNotificationBackend):
    backend_name = "discord-webhook"

    def __init__(self) -> None:
        self._timeout: int = _DEFAULT_TIMEOUT
        self._username_override: str = _DEFAULT_USERNAME
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="timeout",
                type=ToolParameterType.INTEGER,
                description="HTTP timeout in seconds.",
                default=_DEFAULT_TIMEOUT,
            ),
            ConfigParam(
                key="username_override",
                type=ToolParameterType.STRING,
                description=(
                    "Override the webhook display name (default: "
                    "'Gilbert'). Empty = use the webhook's configured "
                    "name."
                ),
                default=_DEFAULT_USERNAME,
            ),
        ]

    @classmethod
    def destination_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="webhook_url",
                type=ToolParameterType.STRING,
                description=(
                    "Full Discord webhook URL "
                    "(https://discord.com/api/webhooks/<id>/<token>)."
                ),
                sensitive=True,
                default="",
            ),
            ConfigParam(
                key="mention",
                type=ToolParameterType.STRING,
                description=(
                    "Optional mention prefix on URGENT messages "
                    "(e.g. '@here' or '<@USER_ID>')."
                ),
                default="",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Test a webhook URL",
                description=(
                    "Pings the Discord webhook URL provided in the "
                    "action payload. Suppresses notifications "
                    "(flags=4096) so the channel doesn't ping members."
                ),
            ),
        ]

    async def invoke_backend_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if key == "test_connection":
            url = str(payload.get("webhook_url", "")).strip()
            if not url:
                return ConfigActionResult(
                    status="error",
                    message="Provide 'webhook_url' in the payload.",
                )
            if not _is_valid_webhook_url(url):
                return ConfigActionResult(
                    status="error",
                    message=(
                        "Webhook URL must start with "
                        "https://discord.com/api/webhooks/ "
                        "or https://discordapp.com/api/webhooks/."
                    ),
                )
            return await self._action_test(url)
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}"
        )

    async def _action_test(self, url: str) -> ConfigActionResult:
        if self._client is None:
            return ConfigActionResult(
                status="error",
                message="discord-webhook backend not initialised — save settings first.",
            )
        body = {
            "username": self._username_override or _DEFAULT_USERNAME,
            "content": "Gilbert test message",
            "flags": _TEST_FLAGS_SUPPRESS,
        }
        try:
            resp = await self._client.post(url, json=body)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ConfigActionResult(
                status="error",
                message=f"Discord returned HTTP {exc.response.status_code}.",
            )
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {_safe_repr(exc)}",
            )
        return ConfigActionResult(
            status="ok", message="Discord accepted the test message."
        )

    async def initialize(self, config: dict[str, Any]) -> None:
        self._timeout = int(config.get("timeout", _DEFAULT_TIMEOUT))
        self._username_override = str(
            config.get("username_override", _DEFAULT_USERNAME)
            or _DEFAULT_USERNAME
        )
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
                message="discord-webhook backend not initialised",
            )
        url = str(destination.data.get("webhook_url", "")).strip()
        if not url:
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message="route is missing 'webhook_url'",
            )
        if not _is_valid_webhook_url(url):
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message="invalid Discord webhook URL",
            )

        deep_link = ""
        if message.source_ref and isinstance(message.source_ref, dict):
            link = message.source_ref.get("deep_link_url")
            if isinstance(link, str):
                deep_link = link

        mention = (
            str(destination.data.get("mention", "") or "").strip()
            if message.urgency is NotificationUrgency.URGENT
            else ""
        )
        content_parts: list[str] = []
        if mention:
            content_parts.append(mention)
        content_parts.append(f"**{message.title}**")
        content_parts.append(message.body)
        if deep_link:
            content_parts.append(deep_link)
        body: dict[str, Any] = {
            "username": self._username_override or _DEFAULT_USERNAME,
            "content": "\n".join(content_parts),
            "flags": (
                _TEST_FLAGS_SUPPRESS if message.source == "test" else 0
            ),
            "embeds": [
                {
                    "title": message.title,
                    "description": message.body,
                    "color": _color_for_urgency(message.urgency),
                    "footer": {"text": f"Gilbert · {message.source}"},
                    **({"url": deep_link} if deep_link else {}),
                }
            ],
        }
        try:
            resp = await self._client.post(url, json=body)
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
        if resp.status_code == 429:
            retry_after_s: float | None = None
            raw = resp.headers.get(_RATE_LIMIT_RESET_HEADER)
            if raw:
                try:
                    retry_after_s = min(float(raw), _RETRY_AFTER_CAP_S)
                except (TypeError, ValueError):
                    retry_after_s = None
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message="HTTP 429 rate-limited",
                retry_after_s=retry_after_s,
            )
        if 500 <= resp.status_code < 600:
            return PushDeliveryResult(
                status=PushDeliveryStatus.TRANSIENT_ERROR,
                message=f"server HTTP {resp.status_code}",
            )
        # Status line only — DO NOT include resp.text (echoes URL+token).
        return PushDeliveryResult(
            status=PushDeliveryStatus.REJECTED,
            message=f"HTTP {resp.status_code}",
        )

