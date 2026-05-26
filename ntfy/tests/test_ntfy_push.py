"""Unit tests for the ntfy push backend."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from gilbert_plugin_ntfy.ntfy_push import NtfyPush

from gilbert.interfaces.notifications import NotificationUrgency
from gilbert.interfaces.push_notifications import (
    PushDeliveryStatus,
    PushDestination,
    PushMessage,
    PushNotificationBackend,
)

pytestmark = pytest.mark.asyncio


def _make_message(
    *,
    urgency: NotificationUrgency = NotificationUrgency.NORMAL,
    source: str = "agent",
) -> PushMessage:
    return PushMessage(
        title="Gilbert · Test",
        body="hello",
        urgency=urgency,
        source=source,
        notification_id="n_1",
    )


def _make_destination(
    *, topic: str = "gilbert-test-x82js", server: str = ""
) -> PushDestination:
    return PushDestination(
        user_id="u_1",
        route_id="r_1",
        data={"topic": topic, "server": server},
    )


# ── Registration ──────────────────────────────────────────────────────


async def test_ntfy_registered_in_backend_registry() -> None:
    assert "ntfy" in PushNotificationBackend.registered_backends()
    assert PushNotificationBackend.registered_backends()["ntfy"] is NtfyPush


async def test_destination_params_declares_topic_and_server() -> None:
    keys = {p.key for p in NtfyPush.destination_params()}
    assert keys == {"topic", "server"}


async def test_backend_actions_includes_test_connection() -> None:
    keys = {a.key for a in NtfyPush.backend_actions()}
    assert "test_connection" in keys


# ── send() happy path and error paths ─────────────────────────────────


async def test_send_delivers_on_2xx() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                request=httpx.Request("POST", "https://ntfy.sh/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.DELIVERED
        assert "200" in result.message
        # Verify the headers carry priority + tag.
        call_kwargs = mock_client.post.call_args.kwargs
        headers = call_kwargs["headers"]
        assert headers["Priority"] == "3"
        assert headers["Tags"] == "robot"
        assert headers["Title"] == "Gilbert - Test"
        headers["Title"].encode("ascii")
    finally:
        await backend.close()


async def test_send_sanitizes_non_ascii_title_for_http_headers() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                request=httpx.Request("POST", "https://ntfy.sh/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(_make_destination(), _make_message())
        title = mock_client.post.call_args.kwargs["headers"]["Title"]
        assert title == "Gilbert - Test"
        title.encode("ascii")
    finally:
        await backend.close()


async def test_send_5xx_is_transient_error() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=503,
                request=httpx.Request("POST", "https://ntfy.sh/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
        assert "503" in result.message
    finally:
        await backend.close()


async def test_send_4xx_is_rejected() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=401,
                request=httpx.Request("POST", "https://ntfy.sh/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.REJECTED
        assert result.message == "HTTP 401"
    finally:
        await backend.close()


async def test_send_missing_topic_is_rejected_without_http_call() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock()
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(
            _make_destination(topic=""),
            _make_message(),
        )
        assert result.status is PushDeliveryStatus.REJECTED
        assert "missing" in result.message
        mock_client.post.assert_not_called()
    finally:
        await backend.close()


async def test_send_uses_route_server_when_set() -> None:
    backend = NtfyPush()
    await backend.initialize({"default_server": "https://ntfy.sh"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                request=httpx.Request("POST", "https://custom.example/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(
            _make_destination(server="https://custom.example"),
            _make_message(),
        )
        url = mock_client.post.call_args.args[0]
        assert url.startswith("https://custom.example/")
    finally:
        await backend.close()


async def test_send_falls_back_to_default_server_when_route_blank() -> None:
    backend = NtfyPush()
    await backend.initialize({"default_server": "https://my-ntfy.example"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                request=httpx.Request(
                    "POST", "https://my-ntfy.example/x"
                ),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(
            _make_destination(server=""),
            _make_message(),
        )
        url = mock_client.post.call_args.args[0]
        assert url.startswith("https://my-ntfy.example/")
    finally:
        await backend.close()


async def test_send_attaches_click_header_for_deep_link() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                request=httpx.Request("POST", "https://ntfy.sh/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        msg = PushMessage(
            title="Gilbert",
            body="hi",
            urgency=NotificationUrgency.NORMAL,
            source="agent",
            source_ref={"deep_link_url": "https://gilbert.example/chat?conversation=c_1"},
        )
        await backend.send(_make_destination(), msg)
        headers = mock_client.post.call_args.kwargs["headers"]
        assert (
            headers["Click"]
            == "https://gilbert.example/chat?conversation=c_1"
        )
    finally:
        await backend.close()


async def test_send_returns_disabled_when_uninitialised() -> None:
    backend = NtfyPush()
    result = await backend.send(_make_destination(), _make_message())
    assert result.status is PushDeliveryStatus.DISABLED


async def test_send_handles_network_error_as_transient() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            side_effect=httpx.ConnectError("connect refused")
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert result.status is PushDeliveryStatus.TRANSIENT_ERROR
        assert "ConnectError" in result.message
    finally:
        await backend.close()


# ── Auth header / scrubbing ──────────────────────────────────────────


async def test_auth_token_scrubbed_from_error_messages() -> None:
    """Bearer token must NOT leak into the result message even if the
    underlying HTTP error includes it (e.g. via httpx's str(url))."""
    backend = NtfyPush()
    await backend.initialize({"auth_token": "super-secret-bearer"})
    try:
        mock_client = MagicMock()
        # Raise a non-network error whose string contains the bearer.
        mock_client.post = AsyncMock(
            side_effect=RuntimeError(
                "request failed: Bearer super-secret-bearer / x"
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.send(_make_destination(), _make_message())
        assert "super-secret-bearer" not in result.message
        assert "<redacted>" in result.message
    finally:
        await backend.close()


async def test_auth_header_set_when_token_configured() -> None:
    backend = NtfyPush()
    await backend.initialize({"auth_token": "tok"})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                request=httpx.Request("POST", "https://ntfy.sh/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        await backend.send(_make_destination(), _make_message())
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok"
    finally:
        await backend.close()


# ── test_connection action ───────────────────────────────────────────


async def test_test_connection_requires_explicit_topic() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        result = await backend.invoke_backend_action(
            "test_connection", {}
        )
        assert result.status == "error"
        assert "topic" in result.message.lower()
    finally:
        await backend.close()


async def test_test_connection_happy_path() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        mock_client = MagicMock()
        mock_client.post = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                request=httpx.Request("POST", "https://ntfy.sh/x"),
            )
        )
        mock_client.aclose = AsyncMock()
        backend._client = mock_client
        result = await backend.invoke_backend_action(
            "test_connection", {"topic": "test-x82js"}
        )
        assert result.status == "ok"
        title = mock_client.post.call_args.kwargs["headers"]["Title"]
        assert title == "Gilbert - Test"
        title.encode("ascii")
    finally:
        await backend.close()


async def test_unknown_action_returns_error() -> None:
    backend = NtfyPush()
    await backend.initialize({})
    try:
        result = await backend.invoke_backend_action("nope", {})
        assert result.status == "error"
        assert "Unknown action" in result.message
    finally:
        await backend.close()


# ── Priority mapping ─────────────────────────────────────────────────


async def test_priority_map_for_urgency() -> None:
    from gilbert_plugin_ntfy.ntfy_push import _ntfy_priority

    assert _ntfy_priority(NotificationUrgency.INFO) == "2"
    assert _ntfy_priority(NotificationUrgency.NORMAL) == "3"
    assert _ntfy_priority(NotificationUrgency.URGENT) == "5"


@pytest.mark.parametrize(
    "source,expected",
    [
        ("agent", "robot"),
        ("scheduler", "alarm_clock"),
        ("inbox", "email"),
        ("doorbell", "bell"),
        ("whatever-else", "bell"),
    ],
)
async def test_tag_map_for_source(source: str, expected: str) -> None:
    from gilbert_plugin_ntfy.ntfy_push import _ntfy_tag_for_source

    assert _ntfy_tag_for_source(source) == expected
