"""Unit tests for the Pushover push backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from gilbert_plugin_pushover.pushover_push import PushoverPush

from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)

pytestmark = pytest.mark.asyncio


def _make_destination(
    *, user_key: str = "u_secret_key_30char_aaaaaaaaaa", device: str = ""
) -> PushDestination:
    return PushDestination(
        user_id="u_1",
        route_id="r_1",
        data={"user_key": user_key, "device": device},
    )


def _make_message(
    *, urgency: NotificationUrgency = NotificationUrgency.NORMAL
) -> PushMessage:
    return PushMessage(
        title="Gilbert · Test",
        body="hello",
        urgency=urgency,
        source="agent",
        notification_id="n_1",
    )


async def test_registered_in_backend_registry() -> None:
    assert "pushover" in PushNotificationBackend.registered_backends()


async def test_destination_params_includes_user_key() -> None:
    keys = {p.key for p in PushoverPush.destination_params()}
    assert keys == {"user_key", "device"}


async def test_send_happy_path_returns_delivered() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                json={"status": 1, "request": "abc-req-id"},
                request=httpx.Request(
                    "POST", "https://api.pushover.net/1/messages.json"
                ),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.DELIVERED
        assert result.provider_message_id == "abc-req-id"
        body = mock_client.post.call_args.kwargs["data"]
        assert body["token"] == "app-token"
        assert body["user"].startswith("u_secret")
        assert body["priority"] == 0
    finally:
        await backend.close()


async def test_send_5xx_is_transient() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=503,
                request=httpx.Request(
                    "POST", "https://api.pushover.net/1/messages.json"
                ),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
    finally:
        await backend.close()


async def test_send_400_is_rejected() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=400,
                json={"status": 0, "errors": ["application token invalid"]},
                request=httpx.Request(
                    "POST", "https://api.pushover.net/1/messages.json"
                ),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.REJECTED
    finally:
        await backend.close()


async def test_send_status_zero_on_200_is_rejected() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                json={"status": 0, "errors": ["user invalid"]},
                request=httpx.Request(
                    "POST", "https://api.pushover.net/1/messages.json"
                ),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.REJECTED
    finally:
        await backend.close()


async def test_send_missing_user_key_is_rejected_without_http() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock()
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(
            _make_destination(user_key=""), _make_message()
        )
        assert result.status is PushDeliveryStatus.REJECTED
        mock_client.post.assert_not_called()
    finally:
        await backend.close()


async def test_send_returns_disabled_when_no_app_token() -> None:
    backend = PushoverPush()
    await backend.initialize({})
    try:
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.DISABLED
    finally:
        await backend.close()


async def test_send_priority_for_each_urgency() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                json={"status": 1},
                request=httpx.Request(
                    "POST", "https://api.pushover.net/1/messages.json"
                ),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        for urgency, expected in [
            (NotificationUrgency.INFO, -1),
            (NotificationUrgency.NORMAL, 0),
            (NotificationUrgency.URGENT, 1),
        ]:
            await backend.send(
                _make_destination(),
                _make_message(urgency=urgency),
            )
            assert mock_client.post.call_args.kwargs["data"]["priority"] == expected
    finally:
        await backend.close()


async def test_failure_text_does_not_leak_user_key() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=RuntimeError(
                "Bearer leaky-token-xyz-failed"
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert "leaky-token-xyz" not in result.message
        assert "<redacted>" in result.message
    finally:
        await backend.close()


async def test_test_connection_action_validates() -> None:
    backend = PushoverPush()
    await backend.initialize({"api_token": "app-token"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                json={"status": 1, "devices": ["iphone", "android"]},
                request=httpx.Request(
                    "POST", "https://api.pushover.net/1/users/validate.json"
                ),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.invoke_backend_action(
            "test_connection", {"user_key": "u_test"}
        )
        assert result.status == "ok"
        assert "2 device" in result.message
    finally:
        await backend.close()

