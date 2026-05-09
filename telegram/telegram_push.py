"""Telegram bot push-notification backend.

Hits the Telegram Bot API directly via httpx — no SDK is needed for the
narrow surface this backend exercises (``getMe``, ``getUpdates``,
``getWebhookInfo``, ``sendMessage``).

The bot token is admin-level (one bot, many users); each user's
destination is a numeric ``chat_id`` (or ``-100…`` for channels). Users
discover their chat_id via the ``discover_chat_id`` backend action,
which polls ``getUpdates`` and returns the recent ``(chat_id, name,
last_text)`` triples for the SPA to render as clickable chips.

**Webhook-mode bots are rejected on initialise.** ``getUpdates`` and a
webhook URL are mutually exclusive at the Telegram API level, so a
webhook-mode bot would fail every send and chat-id discovery would
return nothing useful. The backend stays DISABLED in that case rather
than silently failing.
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


_DEFAULT_TIMEOUT = 15
_RETRY_AFTER_CAP_S = 60.0


_TOKEN_RX = re.compile(
    r"(?:Bearer\s+\S+|/bot[A-Za-z0-9:_-]+/|"
    r"https?://[^\s]*?/api/webhooks/[^/\s]+/[A-Za-z0-9_-]+|"
    r"\?token=[^\s&]+)",
    re.IGNORECASE,
)


def _safe_repr(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return _TOKEN_RX.sub("<redacted>", text)


def _md_escape(text: str) -> str:
    """Escape Markdown legacy syntax used by ``parse_mode="Markdown"``."""
    return re.sub(r"([*_`\[])", r"\\\1", text)


class TelegramPush(PushNotificationBackend):
    backend_name = "telegram"

    def __init__(self) -> None:
        self._bot_token: str = ""
        self._timeout: int = _DEFAULT_TIMEOUT
        self._client: httpx.AsyncClient | None = None
        self._bot_username: str = ""
        self._webhook_mode: bool = False

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="bot_token",
                type=ToolParameterType.STRING,
                description="Telegram bot token from @BotFather.",
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
                key="chat_id",
                type=ToolParameterType.STRING,
                description=(
                    "Telegram chat id (numeric for users, '-100…' for "
                    "channels). Use the 'Discover chat id' action to "
                    "find yours."
                ),
                default="",
            ),
        ]

    @classmethod
    def backend_actions(cls) -> list[ConfigAction]:
        return [
            ConfigAction(
                key="test_connection",
                label="Verify bot",
                description="Calls /getMe to verify the bot token.",
            ),
            ConfigAction(
                key="discover_chat_id",
                label="Discover chat id",
                description=(
                    "Polls /getUpdates and returns recent chat ids the "
                    "bot has seen. Send any message to your bot first."
                ),
            ),
        ]

    def runtime_data(self) -> dict[str, Any]:
        # Token must NEVER be exposed here; only UI hints.
        return {"bot_username": self._bot_username}

    async def invoke_backend_action(
        self, key: str, payload: dict[str, Any]
    ) -> ConfigActionResult:
        if self._client is None or not self._bot_token:
            return ConfigActionResult(
                status="error",
                message="telegram backend not initialised — set bot_token first.",
            )
        if self._webhook_mode:
            return ConfigActionResult(
                status="error",
                message=(
                    "Bot is in webhook mode; v1 requires polling-mode "
                    "bots. Run /deleteWebhook on the bot or set "
                    "drop_pending_updates=true and try again."
                ),
            )
        if key == "test_connection":
            return await self._action_test_connection()
        if key == "discover_chat_id":
            return await self._action_discover_chat_id()
        return ConfigActionResult(
            status="error", message=f"Unknown action: {key}"
        )

    async def _action_test_connection(self) -> ConfigActionResult:
        try:
            assert self._client is not None
            resp = await self._client.get(self._bot_url("getMe"))
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {_safe_repr(exc)}",
            )
        if data.get("ok"):
            username = (data.get("result") or {}).get("username", "")
            return ConfigActionResult(
                status="ok",
                message=f"Bot verified — @{username}.",
                data={"bot_username": username},
            )
        return ConfigActionResult(
            status="error",
            message="Telegram getMe returned non-ok response.",
        )

    async def _action_discover_chat_id(self) -> ConfigActionResult:
        try:
            assert self._client is not None
            resp = await self._client.get(self._bot_url("getUpdates"))
            data = resp.json() if resp.status_code == 200 else {}
        except Exception as exc:
            return ConfigActionResult(
                status="error",
                message=f"Connection failed: {_safe_repr(exc)}",
            )
        if not data.get("ok"):
            return ConfigActionResult(
                status="error",
                message="Telegram getUpdates returned non-ok response.",
            )
        chats: list[dict[str, Any]] = []
        seen: set[str] = set()
        for upd in data.get("result", []) or []:
            msg = upd.get("message") or upd.get("edited_message") or {}
            chat = msg.get("chat") or {}
            chat_id = str(chat.get("id") or "")
            if not chat_id or chat_id in seen:
                continue
            seen.add(chat_id)
            name = (
                chat.get("title")
                or chat.get("username")
                or f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
                or chat_id
            )
            chats.append(
                {
                    "chat_id": chat_id,
                    "name": name,
                    "last_text": msg.get("text", "")[:200],
                }
            )
        if not chats:
            return ConfigActionResult(
                status="pending",
                message=(
                    "No chats found. Open Telegram, send any message to "
                    f"@{self._bot_username or 'the bot'}, then click "
                    "'Discover chat id' again."
                ),
                data={"chats": []},
            )
        return ConfigActionResult(
            status="ok",
            message=f"Found {len(chats)} chat(s).",
            data={"chats": chats},
        )

    async def initialize(self, config: dict[str, Any]) -> None:
        self._bot_token = str(config.get("bot_token", "") or "")
        self._timeout = int(config.get("timeout", _DEFAULT_TIMEOUT))
        self._client = httpx.AsyncClient(timeout=self._timeout)
        self._bot_username = ""
        self._webhook_mode = False
        if not self._bot_token:
            return
        # Cache getMe.username for the chat-id wizard's deep link.
        try:
            resp = await self._client.get(self._bot_url("getMe"))
            if resp.status_code == 200:
                payload = resp.json() or {}
                if payload.get("ok"):
                    result = payload.get("result") or {}
                    self._bot_username = str(result.get("username", "") or "")
        except Exception as exc:
            logger.error(
                "telegram: getMe init failed: %s", type(exc).__name__
            )
        # Webhook-mode rejection.
        try:
            resp = await self._client.get(self._bot_url("getWebhookInfo"))
            if resp.status_code == 200:
                payload = resp.json() or {}
                if payload.get("ok"):
                    result = payload.get("result") or {}
                    if result.get("url"):
                        logger.error(
                            "telegram bot is in webhook mode; v1 requires "
                            "polling-mode bots"
                        )
                        self._webhook_mode = True
        except Exception as exc:
            logger.error(
                "telegram: getWebhookInfo init failed: %s",
                type(exc).__name__,
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._bot_username = ""

    async def send(
        self,
        destination: PushDestination,
        message: PushMessage,
    ) -> PushDeliveryResult:
        if self._client is None or not self._bot_token:
            return PushDeliveryResult(
                status=PushDeliveryStatus.DISABLED,
                message="telegram backend not initialised",
            )
        if self._webhook_mode:
            return PushDeliveryResult(
                status=PushDeliveryStatus.DISABLED,
                message="webhook-mode bot",
            )
        chat_id = str(destination.data.get("chat_id", "")).strip()
        if not chat_id:
            return PushDeliveryResult(
                status=PushDeliveryStatus.REJECTED,
                message="route is missing 'chat_id'",
            )
        deep_link = ""
        if message.source_ref and isinstance(message.source_ref, dict):
            link = message.source_ref.get("deep_link_url")
            if isinstance(link, str):
                deep_link = link
        text_parts = [
            f"*{_md_escape(message.title)}*",
            _md_escape(message.body),
        ]
        if deep_link:
            text_parts.append(deep_link)
        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": "\n".join(text_parts),
            "parse_mode": "Markdown",
            "disable_notification": message.urgency is NotificationUrgency.INFO,
        }
        if deep_link:
            body["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": "Open in Gilbert", "url": deep_link}]
                ]
            }
        try:
            resp = await self._client.post(
                self._bot_url("sendMessage"), json=body
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
        try:
            payload = resp.json()
        except Exception:
            payload = {}
        if 200 <= resp.status_code < 300 and payload.get("ok"):
            msg_id = (payload.get("result") or {}).get("message_id", "")
            return PushDeliveryResult(
                status=PushDeliveryStatus.DELIVERED,
                message="HTTP 200",
                provider_message_id=str(msg_id) if msg_id else "",
            )
        if resp.status_code == 429:
            retry_after_s: float | None = None
            params = payload.get("parameters") or {}
            raw = params.get("retry_after")
            if raw is not None:
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
        # 4xx — REJECTED. Status line only — DO NOT include description
        # text or response body (echoes the bot token URL on some paths).
        return PushDeliveryResult(
            status=PushDeliveryStatus.REJECTED,
            message=f"HTTP {resp.status_code}",
        )

    def _bot_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self._bot_token}/{method}"

