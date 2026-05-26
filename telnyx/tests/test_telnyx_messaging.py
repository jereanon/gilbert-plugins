"""Telnyx messaging backend tests.

Two surfaces to cover:

1. ``send_message`` POSTs the right shape to ``/v2/messages`` and
   extracts the carrier-issued id from the response.
2. ``deliver_messaging_webhook`` parses Telnyx's ``message.received``
   event shape into a ``Message`` and dispatches via the bound
   deliverer. ``message.sent``/``message.finalized`` are no-ops.

httpx's ``MockTransport`` stands in for the live API; we never hit
the network.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from gilbert.interfaces.messaging import Message, MessageType, SendResult


# ── send_message ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_posts_v2_messages_and_returns_id() -> None:
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    captured: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = (
            await _read_json(request)
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "id": "msg_telnyx_001",
                    "from": {"phone_number": "+15551234567"},
                    "to": [{"phone_number": "+15555550100"}],
                    "text": "hello",
                }
            },
        )

    backend = TelnyxMessaging()
    await backend.initialize(
        {"api_key": "KEY_test", "messaging_profile_id": "prof_abc"}
    )
    # Swap the http client for one with our mock transport.
    backend._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.telnyx.com/v2/",
        headers={"Authorization": "Bearer KEY_test"},
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        to="+15555550100",
        body="hello",
        from_number="+15551234567",
    )
    assert isinstance(result, SendResult)
    assert result.message_id == "msg_telnyx_001"
    # Default preferred_type is RCS — the response didn't echo a
    # ``type`` field, so the backend falls back to the preference.
    assert result.actual_type == MessageType.RCS.value
    assert captured["url"].endswith("/v2/messages")
    assert captured["headers"].get("authorization") == "Bearer KEY_test"
    assert captured["json"] == {
        "from": "+15551234567",
        "to": "+15555550100",
        "text": "hello",
        "type": "RCS",
        "messaging_profile_id": "prof_abc",
    }


@pytest.mark.asyncio
async def test_send_message_includes_media_urls_when_present() -> None:
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    captured: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = await _read_json(request)
        return httpx.Response(200, json={"data": {"id": "mms_001"}})

    backend = TelnyxMessaging()
    await backend.initialize({"api_key": "KEY_test"})
    backend._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.telnyx.com/v2/",
        transport=httpx.MockTransport(_handler),
    )

    await backend.send_message(
        to="+15555550100",
        body="check this out",
        from_number="+15551234567",
        media_urls=["https://example.com/img.jpg"],
    )
    assert captured["json"]["media_urls"] == ["https://example.com/img.jpg"]


@pytest.mark.asyncio
async def test_send_message_raises_on_carrier_error() -> None:
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"errors": [{"detail": "bad"}]}')

    backend = TelnyxMessaging()
    await backend.initialize({"api_key": "KEY_test"})
    backend._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.telnyx.com/v2/",
        transport=httpx.MockTransport(_handler),
    )

    with pytest.raises(RuntimeError, match="returned 422"):
        await backend.send_message(
            to="+15555550100",
            body="rejected",
            from_number="+15551234567",
        )


@pytest.mark.asyncio
async def test_send_message_requires_from_number() -> None:
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    backend = TelnyxMessaging()
    await backend.initialize({"api_key": "KEY_test"})
    with pytest.raises(RuntimeError, match="from_number"):
        await backend.send_message(
            to="+15555550100",
            body="no sender",
            from_number="",
        )


# ── RCS / transport-tier send tests ──────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_request_type_defaults_to_uppercase_rcs() -> None:
    """Telnyx wants uppercase tier names (``"SMS"`` / ``"MMS"`` /
    ``"RCS"``). The default ``preferred_type`` is RCS so plain sends
    must ship ``type: "RCS"`` to the API."""
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    captured: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = await _read_json(request)
        return httpx.Response(
            200,
            json={"data": {"id": "rcs_001", "type": "RCS"}},
        )

    backend = TelnyxMessaging()
    await backend.initialize({"api_key": "KEY_test"})
    backend._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.telnyx.com/v2/",
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        to="+15555550100", body="hi", from_number="+15551234567"
    )
    assert captured["json"]["type"] == "RCS"
    assert result.actual_type == MessageType.RCS.value


@pytest.mark.asyncio
async def test_send_message_forwards_explicit_sms_preference() -> None:
    """Caller forces SMS (e.g. cheaper tier on a carrier where RCS
    billing is more expensive). Backend must pipe through the
    explicit preference instead of the default."""
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    captured: dict[str, Any] = {}

    async def _handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = await _read_json(request)
        return httpx.Response(
            200, json={"data": {"id": "sms_001", "type": "SMS"}}
        )

    backend = TelnyxMessaging()
    await backend.initialize({"api_key": "KEY_test"})
    backend._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.telnyx.com/v2/",
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        to="+15555550100",
        body="hi",
        from_number="+15551234567",
        preferred_type=MessageType.SMS,
    )
    assert captured["json"]["type"] == "SMS"
    assert result.actual_type == MessageType.SMS.value


@pytest.mark.asyncio
async def test_send_message_reports_carrier_downgrade_in_actual_type() -> None:
    """Caller asked for RCS; carrier downgraded to SMS because the
    recipient isn't RCS-capable. The backend must surface the
    downgrade via ``SendResult.actual_type`` so the SPA badge can
    show "(downgraded to SMS)"."""
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    async def _handler(_request: httpx.Request) -> httpx.Response:
        # Telnyx echoed back "SMS" even though we asked for "RCS".
        return httpx.Response(
            200, json={"data": {"id": "downgraded_001", "type": "SMS"}}
        )

    backend = TelnyxMessaging()
    await backend.initialize({"api_key": "KEY_test"})
    backend._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.telnyx.com/v2/",
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        to="+15555550100",
        body="hi",
        from_number="+15551234567",
        preferred_type=MessageType.RCS,
    )
    assert result.actual_type == MessageType.SMS.value


@pytest.mark.asyncio
async def test_send_message_falls_back_to_preferred_when_response_omits_type() -> None:
    """Older Telnyx responses (or mocks) may not echo ``data.type``.
    Backend must fall back to the caller's preference so callers
    never see ``actual_type=""`` for a successful send."""
    from gilbert_plugin_telnyx.telnyx_messaging import TelnyxMessaging

    async def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"id": "no_type_001"}}
        )

    backend = TelnyxMessaging()
    await backend.initialize({"api_key": "KEY_test"})
    backend._http = httpx.AsyncClient(  # noqa: SLF001
        base_url="https://api.telnyx.com/v2/",
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        to="+15555550100",
        body="hi",
        from_number="+15551234567",
        preferred_type=MessageType.MMS,
    )
    assert result.actual_type == MessageType.MMS.value


# ── inbound webhook parsing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_inbound_webhook_parses_message_received() -> None:
    from gilbert_plugin_telnyx.telnyx_messaging import (
        deliver_messaging_webhook,
        _set_inbound_deliverer,
    )

    captured: list[Message] = []

    async def _deliverer(msg: Message) -> None:
        captured.append(msg)

    _set_inbound_deliverer(_deliverer)

    await deliver_messaging_webhook(
        {
            "data": {
                "event_type": "message.received",
                "payload": {
                    "id": "msg_inbound_001",
                    "from": {"phone_number": "+15555550100"},
                    "to": [{"phone_number": "+15551234567"}],
                    "text": "hey gilbert",
                    "received_at": "2026-05-24T13:00:00Z",
                    "media": [],
                },
            }
        }
    )
    assert len(captured) == 1
    msg = captured[0]
    assert msg.message_id == "msg_inbound_001"
    assert msg.our_number == "+15551234567"
    assert msg.other_number == "+15555550100"
    assert msg.body == "hey gilbert"
    assert msg.direction == "inbound"
    assert msg.status == "received"
    assert msg.created_at == "2026-05-24T13:00:00Z"
    assert msg.media_urls == []
    assert msg.backend == "telnyx"


@pytest.mark.asyncio
async def test_inbound_webhook_extracts_mms_media_urls() -> None:
    from gilbert_plugin_telnyx.telnyx_messaging import (
        deliver_messaging_webhook,
        _set_inbound_deliverer,
    )

    captured: list[Message] = []

    async def _deliverer(msg: Message) -> None:
        captured.append(msg)

    _set_inbound_deliverer(_deliverer)

    await deliver_messaging_webhook(
        {
            "data": {
                "event_type": "message.received",
                "payload": {
                    "id": "mms_001",
                    "from": {"phone_number": "+15555550100"},
                    "to": [{"phone_number": "+15551234567"}],
                    "text": "look at this",
                    "received_at": "2026-05-24T13:00:00Z",
                    "media": [
                        {
                            "url": "https://example.com/img1.jpg",
                            "content_type": "image/jpeg",
                        },
                        {
                            "url": "https://example.com/img2.jpg",
                            "content_type": "image/jpeg",
                        },
                    ],
                },
            }
        }
    )
    assert captured[0].media_urls == [
        "https://example.com/img1.jpg",
        "https://example.com/img2.jpg",
    ]


@pytest.mark.asyncio
async def test_inbound_webhook_captures_carrier_reported_type() -> None:
    """Inbound payloads carry ``type: "SMS"`` / ``"MMS"`` / ``"RCS"``
    (uppercase per Telnyx). Backend normalizes to our lowercase enum
    so ``Message.type`` matches the rest of the codebase."""
    from gilbert_plugin_telnyx.telnyx_messaging import (
        _set_inbound_deliverer,
        deliver_messaging_webhook,
    )

    captured: list[Message] = []

    async def _deliverer(msg: Message) -> None:
        captured.append(msg)

    _set_inbound_deliverer(_deliverer)

    await deliver_messaging_webhook(
        {
            "data": {
                "event_type": "message.received",
                "payload": {
                    "id": "msg_rcs_in",
                    "from": {"phone_number": "+15555550100"},
                    "to": [{"phone_number": "+15551234567"}],
                    "text": "hey via RCS",
                    "received_at": "2099-01-01T00:00:00Z",
                    "type": "RCS",
                    "media": [],
                },
            }
        }
    )
    assert captured[-1].type == MessageType.RCS.value


@pytest.mark.asyncio
async def test_inbound_webhook_unknown_type_leaves_field_empty() -> None:
    """A carrier ships an unrecognized tier (or omits the field
    entirely on legacy payloads). Backend records an empty string
    rather than crashing — the SPA will hide the badge for those
    rows. Empty-string default is documented on ``Message.type``."""
    from gilbert_plugin_telnyx.telnyx_messaging import (
        _set_inbound_deliverer,
        deliver_messaging_webhook,
    )

    captured: list[Message] = []

    async def _deliverer(msg: Message) -> None:
        captured.append(msg)

    _set_inbound_deliverer(_deliverer)

    await deliver_messaging_webhook(
        {
            "data": {
                "event_type": "message.received",
                "payload": {
                    "id": "msg_unknown_type",
                    "from": {"phone_number": "+15555550100"},
                    "to": [{"phone_number": "+15551234567"}],
                    "text": "from the future",
                    "received_at": "2099-01-01T00:00:00Z",
                    "type": "QUANTUM-MESH",
                },
            }
        }
    )
    assert captured[-1].type == ""


@pytest.mark.asyncio
async def test_inbound_webhook_ignores_non_received_events() -> None:
    """``message.sent`` / ``message.finalized`` are carrier status
    callbacks for outbound messages we already persisted. Don't
    re-dispatch them as fresh inbounds."""
    from gilbert_plugin_telnyx.telnyx_messaging import (
        deliver_messaging_webhook,
        _set_inbound_deliverer,
    )

    captured: list[Message] = []

    async def _deliverer(msg: Message) -> None:
        captured.append(msg)

    _set_inbound_deliverer(_deliverer)

    for event_type in ("message.sent", "message.finalized", "message.failed"):
        await deliver_messaging_webhook(
            {
                "data": {
                    "event_type": event_type,
                    "payload": {
                        "id": "msg_status_001",
                        "from": {"phone_number": "+15551234567"},
                        "to": [{"phone_number": "+15555550100"}],
                        "text": "outbound status update",
                    },
                }
            }
        )
    assert captured == []


@pytest.mark.asyncio
async def test_inbound_webhook_with_no_deliverer_does_not_raise() -> None:
    """If the messaging plugin isn't loaded / hasn't bound a
    deliverer yet, a webhook arriving must NOT 500 — we just log and
    drop. Telnyx retries 4xx/5xx and we don't want webhook storms on
    a misconfigured deployment."""
    from gilbert_plugin_telnyx.telnyx_messaging import (
        deliver_messaging_webhook,
        _set_inbound_deliverer,
    )

    _set_inbound_deliverer(None)  # type: ignore[arg-type]

    # Should not raise.
    await deliver_messaging_webhook(
        {
            "data": {
                "event_type": "message.received",
                "payload": {
                    "id": "ghost",
                    "from": {"phone_number": "+15555550100"},
                    "to": [{"phone_number": "+15551234567"}],
                    "text": "nobody listening",
                },
            }
        }
    )


# ── Capability adapter ───────────────────────────────────────────────


def test_capability_service_advertises_telnyx_messaging_webhook() -> None:
    from gilbert_plugin_telnyx.telnyx_messaging import (
        TelnyxMessagingWebhookService,
    )

    svc = TelnyxMessagingWebhookService()
    info = svc.service_info()
    assert "telnyx_messaging_webhook" in info.capabilities


# ── helpers ──────────────────────────────────────────────────────────


async def _read_json(request: httpx.Request) -> dict[str, Any]:
    """httpx's mock transport hands us the raw Request; the JSON body
    is on ``request.content``."""
    import json

    if isinstance(request.content, bytes):
        return json.loads(request.content.decode("utf-8") or "{}")
    raise TypeError(f"unexpected request body type {type(request.content)}")
