"""Unit tests for the Telegram push backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from gilbert_plugin_telegram.telegram_push import TelegramPush

from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)

pytestmark = pytest.mark.asyncio

_BOT_TOKEN = "1234567890:ABCDEF-secret-bot-token"


def _make_destination(*, chat_id: str = "12345") -> PushDestination:
    return PushDestination(
        user_id="u_1",
        route_id="r_1",
        data={"chat_id": chat_id},
    )


def _make_message(
    *,
    urgency: NotificationUrgency = NotificationUrgency.NORMAL,
    deep_link: str | None = None,
) -> PushMessage:
    source_ref = {"deep_link_url": deep_link} if deep_link else None
    return PushMessage(
        title="Gilbert",
        body="hello",
        urgency=urgency,
        source="agent",
        source_ref=source_ref,
        notification_id="n_1",
    )


def _httpx_response(*, status: int = 200, json_payload: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json_payload or {},
        request=httpx.Request("POST", "https://api.telegram.org/bot/x"),
    )


async def test_registered_in_backend_registry() -> None:
    assert "telegram" in PushNotificationBackend.registered_backends()


async def _initialised_backend(
    *,
    webhook_url: str = "",
    bot_username: str = "MyGilbertBot",
) -> TelegramPush:
    backend = TelegramPush()
    mock_client = MagicMock()
    mock_client.aclose = AsyncMock()

    def _get(url: str) -> httpx.Response:
        if "getMe" in url:
            return _httpx_response(
                json_payload={
                    "ok": True,
                    "result": {"id": 1, "username": bot_username},
                }
            )
        if "getWebhookInfo" in url:
            return _httpx_response(
                json_payload={"ok": True, "result": {"url": webhook_url}}
            )
        return _httpx_response(json_payload={"ok": True})

    mock_client.get = AsyncMock(side_effect=_get)
    mock_client.post = AsyncMock()
    backend._client = mock_client
    # bypass real httpx client construction in initialize
    backend._bot_token = _BOT_TOKEN
    backend._timeout = 15
    # Trigger getMe + getWebhookInfo path manually so init mirrors real flow.
    me_resp = await mock_client.get(backend._bot_url("getMe"))
    payload = me_resp.json()
    backend._bot_username = payload["result"]["username"]
    wh_resp = await mock_client.get(backend._bot_url("getWebhookInfo"))
    wh_payload = wh_resp.json()
    backend._webhook_mode = bool(
        (wh_payload.get("result") or {}).get("url")
    )
    return backend


async def test_runtime_data_exposes_bot_username_not_token() -> None:
    backend = await _initialised_backend()
    runtime = backend.runtime_data()
    assert runtime["bot_username"] == "MyGilbertBot"
    assert _BOT_TOKEN not in str(runtime)


async def test_send_happy_path() -> None:
    backend = await _initialised_backend()
    backend._client.post = AsyncMock(  # type: ignore[union-attr]
        return_value=_httpx_response(
            status=200, json_payload={"ok": True, "result": {"message_id": 42}}
        )
    )
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.DELIVERED
    assert result.provider_message_id == "42"


async def test_send_403_is_rejected() -> None:
    backend = await _initialised_backend()
    backend._client.post = AsyncMock(  # type: ignore[union-attr]
        return_value=_httpx_response(
            status=403,
            json_payload={
                "ok": False,
                "error_code": 403,
                "description": "bot was blocked by the user",
            },
        )
    )
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.REJECTED
    # Status line only — no description leak.
    assert "blocked" not in result.message


async def test_send_429_uses_parameters_retry_after() -> None:
    backend = await _initialised_backend()
    backend._client.post = AsyncMock(  # type: ignore[union-attr]
        return_value=_httpx_response(
            status=429,
            json_payload={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests: retry after 3",
                "parameters": {"retry_after": 3},
            },
        )
    )
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
    assert result.retry_after_s == 3.0


async def test_send_5xx_is_transient() -> None:
    backend = await _initialised_backend()
    backend._client.post = AsyncMock(  # type: ignore[union-attr]
        return_value=_httpx_response(
            status=502, json_payload={"ok": False}
        )
    )
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.TRANSIENT_ERROR


async def test_send_missing_chat_id_is_rejected_without_http() -> None:
    backend = await _initialised_backend()
    mock_post = AsyncMock()
    backend._client.post = mock_post  # type: ignore[union-attr]
    result = await backend.send(
        _make_destination(chat_id=""), _make_message()
    )
    assert result.status is PushDeliveryStatus.REJECTED
    mock_post.assert_not_called()


async def test_webhook_mode_keeps_backend_disabled() -> None:
    backend = await _initialised_backend(webhook_url="https://hook.example/x")
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.DISABLED
    assert "webhook" in result.message


async def test_send_attaches_inline_keyboard_for_deep_link() -> None:
    backend = await _initialised_backend()
    backend._client.post = AsyncMock(  # type: ignore[union-attr]
        return_value=_httpx_response(
            status=200, json_payload={"ok": True, "result": {"message_id": 7}}
        )
    )
    await backend.send(
        _make_destination(),
        _make_message(deep_link="https://gilbert.example/chat?conversation=c1"),
    )
    body = backend._client.post.call_args.kwargs["json"]  # type: ignore[union-attr]
    assert "reply_markup" in body
    keyboard = body["reply_markup"]["inline_keyboard"]
    assert (
        keyboard[0][0]["url"]
        == "https://gilbert.example/chat?conversation=c1"
    )


async def test_send_disable_notification_for_info_urgency() -> None:
    backend = await _initialised_backend()
    backend._client.post = AsyncMock(  # type: ignore[union-attr]
        return_value=_httpx_response(
            status=200, json_payload={"ok": True, "result": {"message_id": 1}}
        )
    )
    await backend.send(
        _make_destination(),
        _make_message(urgency=NotificationUrgency.INFO),
    )
    body = backend._client.post.call_args.kwargs["json"]  # type: ignore[union-attr]
    assert body["disable_notification"] is True


async def test_failure_text_does_not_leak_bot_token() -> None:
    backend = await _initialised_backend()
    # Simulate httpx raising a RuntimeError that includes the URL with token.
    backend._client.post = AsyncMock(  # type: ignore[union-attr]
        side_effect=RuntimeError(
            f"Connection failed for https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
        )
    )
    result = await backend.send(_make_destination(), _make_message())
    assert _BOT_TOKEN not in result.message
    assert "<redacted>" in result.message


async def test_test_connection_action_returns_username() -> None:
    backend = await _initialised_backend()
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "ok"
    assert "MyGilbertBot" in result.message


async def test_test_connection_blocked_when_webhook_mode() -> None:
    backend = await _initialised_backend(webhook_url="https://x")
    result = await backend.invoke_backend_action("test_connection", {})
    assert result.status == "error"
    assert "webhook" in result.message.lower()


async def test_discover_chat_id_returns_chats() -> None:
    backend = await _initialised_backend()

    async def fake_get(url: str) -> httpx.Response:
        if "getUpdates" in url:
            return _httpx_response(
                json_payload={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 1,
                            "message": {
                                "chat": {
                                    "id": 11111,
                                    "first_name": "Jeff",
                                    "type": "private",
                                },
                                "text": "hi gilbert",
                            },
                        },
                        {
                            "update_id": 2,
                            "message": {
                                "chat": {
                                    "id": 22222,
                                    "title": "Family group",
                                    "type": "group",
                                },
                                "text": "hello",
                            },
                        },
                    ],
                }
            )
        return _httpx_response(json_payload={"ok": True})

    backend._client.get = AsyncMock(side_effect=fake_get)  # type: ignore[union-attr]
    result = await backend.invoke_backend_action("discover_chat_id", {})
    assert result.status == "ok"
    chats = result.data["chats"]
    assert len(chats) == 2
    chat_ids = {c["chat_id"] for c in chats}
    assert chat_ids == {"11111", "22222"}


async def test_discover_chat_id_empty_returns_pending() -> None:
    backend = await _initialised_backend()

    async def fake_get(url: str) -> httpx.Response:
        if "getUpdates" in url:
            return _httpx_response(
                json_payload={"ok": True, "result": []}
            )
        return _httpx_response(json_payload={"ok": True})

    backend._client.get = AsyncMock(side_effect=fake_get)  # type: ignore[union-attr]
    result = await backend.invoke_backend_action("discover_chat_id", {})
    assert result.status == "pending"
    assert result.data["chats"] == []

