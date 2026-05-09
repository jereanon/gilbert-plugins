"""Unit tests for the discord-webhook push backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from gilbert_plugin_discord_webhook.discord_webhook_push import (
    DiscordWebhookPush,
)

from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)

pytestmark = pytest.mark.asyncio


_VALID_URL = (
    "https://discord.com/api/webhooks/123456789/abcdef-secret-token"
)


def _make_destination(
    *, url: str = _VALID_URL, mention: str = ""
) -> PushDestination:
    return PushDestination(
        user_id="u_1",
        route_id="r_1",
        data={"webhook_url": url, "mention": mention},
    )


def _make_message(
    *,
    urgency: NotificationUrgency = NotificationUrgency.NORMAL,
    source: str = "agent",
    deep_link: str | None = None,
) -> PushMessage:
    source_ref = {"deep_link_url": deep_link} if deep_link else None
    return PushMessage(
        title="Gilbert",
        body="hello",
        urgency=urgency,
        source=source,
        source_ref=source_ref,
        notification_id="n_1",
    )


async def test_registered_in_backend_registry() -> None:
    assert "discord-webhook" in PushNotificationBackend.registered_backends()


async def test_destination_params_includes_webhook_url() -> None:
    keys = {p.key for p in DiscordWebhookPush.destination_params()}
    assert "webhook_url" in keys
    assert "mention" in keys


async def test_send_204_is_delivered() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=204,
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.DELIVERED
    finally:
        await backend.close()


async def test_send_404_is_rejected() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=404,
                text='{"message":"Unknown Webhook"}',
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.REJECTED
        assert "404" in result.message
        # Status-line only — no echo of the response text or webhook URL.
        assert "abcdef-secret-token" not in result.message
        assert "Unknown Webhook" not in result.message
    finally:
        await backend.close()


async def test_send_429_uses_retry_after() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=429,
                headers={"X-RateLimit-Reset-After": "5.5"},
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
        assert result.retry_after_s == pytest.approx(5.5)
    finally:
        await backend.close()


async def test_send_429_caps_retry_after_at_60() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=429,
                headers={"X-RateLimit-Reset-After": "999"},
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.retry_after_s == 60.0
    finally:
        await backend.close()


async def test_send_5xx_is_transient() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=503,
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
    finally:
        await backend.close()


async def test_send_missing_url_is_rejected_without_http_call() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock()
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(
            _make_destination(url=""), _make_message()
        )
        assert result.status is PushDeliveryStatus.REJECTED
        mock_client.post.assert_not_called()
    finally:
        await backend.close()


async def test_send_invalid_prefix_is_rejected_without_http_call() -> None:
    """SSRF guard: anything outside discord.com / discordapp.com is dropped."""
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock()
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(
            _make_destination(url="https://evil.example/api/webhooks/1/2"),
            _make_message(),
        )
        assert result.status is PushDeliveryStatus.REJECTED
        assert "invalid" in result.message.lower()
        mock_client.post.assert_not_called()
    finally:
        await backend.close()


async def test_test_message_uses_suppress_flag() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=204,
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(
            _make_destination(),
            _make_message(source="test"),
        )
        body = mock_client.post.call_args.kwargs["json"]
        assert body["flags"] == 4096
    finally:
        await backend.close()


async def test_non_test_uses_zero_flags() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=204,
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(_make_destination(), _make_message(source="agent"))
        body = mock_client.post.call_args.kwargs["json"]
        assert body["flags"] == 0
    finally:
        await backend.close()


async def test_urgent_uses_red_color() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=204,
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(
            _make_destination(),
            _make_message(urgency=NotificationUrgency.URGENT),
        )
        body = mock_client.post.call_args.kwargs["json"]
        assert body["embeds"][0]["color"] == 0xCC2222
    finally:
        await backend.close()


async def test_urgent_with_mention_includes_prefix() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=204,
                request=httpx.Request("POST", _VALID_URL),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(
            _make_destination(mention="@here"),
            _make_message(urgency=NotificationUrgency.URGENT),
        )
        body = mock_client.post.call_args.kwargs["json"]
        assert body["content"].startswith("@here")
    finally:
        await backend.close()


async def test_failure_text_does_not_leak_webhook_token() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=RuntimeError(
                f"connection failed for {_VALID_URL}"
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert "abcdef-secret-token" not in result.message
        assert "<redacted>" in result.message
    finally:
        await backend.close()


async def test_test_connection_action_validates_prefix() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        result = await backend.invoke_backend_action(
            "test_connection",
            {"webhook_url": "https://evil.example/api/webhooks/1/2"},
        )
        assert result.status == "error"
        assert "https://discord.com" in result.message
    finally:
        await backend.close()


async def test_test_connection_requires_url() -> None:
    backend = DiscordWebhookPush()
    await backend.initialize({})
    try:
        result = await backend.invoke_backend_action("test_connection", {})
        assert result.status == "error"
    finally:
        await backend.close()

