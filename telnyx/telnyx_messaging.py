"""Telnyx SMS / messaging backend.

Sends via ``POST /v2/messages`` (the standard Telnyx Messaging API).
Receives via webhook events posted to ``/api/telnyx/messages/webhook``
on the public host — core's matching route resolves the
``telnyx_messaging_webhook`` capability ``TelnyxMessagingWebhookService``
exposes here and hands the raw payload off.

Parsing rules for inbound:

- Telnyx sends a JSON body with ``data.event_type`` ∈ {``message.received``,
  ``message.sent``, ``message.finalized``}. We only act on
  ``message.received`` — outbound finalizations are status updates we
  already persisted at send time.
- ``data.payload.id`` is the carrier-issued message id (used as the
  Gilbert ``message_id`` so duplicate webhook deliveries collapse).
- ``data.payload.from.phone_number`` is the remote party.
- ``data.payload.to[0].phone_number`` is the Gilbert number that
  received the message — used to resolve which user owns the thread.
- ``data.payload.text`` is the message body. MMS attachments come in
  ``data.payload.media[*].url``.
- ``data.payload.received_at`` is ISO 8601 — used as ``created_at``.

The backend is one of two Telnyx-product services in this plugin
(the other is ``TelnyxTelephony``). They share an HTTP client / API key
pattern but live in separate modules because the products are
genuinely separate on the Telnyx side.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.messaging import (
    InboundDeliverer,
    Message,
    MessageDirection,
    MessageStatus,
    MessagingBackend,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)


_TELNYX_API = "https://api.telnyx.com/v2/"


class TelnyxMessaging(MessagingBackend):
    """``MessagingBackend`` implementation that talks to Telnyx
    Messaging API.

    Inbound delivery is push-based: Telnyx hits our webhook, core's
    route resolves ``telnyx_messaging_webhook`` capability, this
    plugin's ``TelnyxMessagingWebhookService`` parses the payload
    into a ``Message`` and calls back through the ``InboundDeliverer``
    the MessagingService bound here at startup."""

    backend_name: ClassVar[str] = "telnyx"

    def __init__(self) -> None:
        self._api_key: str = ""
        self._messaging_profile_id: str = ""
        self._http: httpx.AsyncClient | None = None
        # Bound by MessagingService at startup; the webhook service
        # reads this when an inbound event arrives.
        self._inbound_deliverer: InboundDeliverer | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description=(
                    "Telnyx API v2 key (starts with KEY...). Found in "
                    "the Telnyx portal under Account → API Keys. "
                    "Same key as the voice backend uses; configured "
                    "separately so the two products can rotate keys "
                    "independently."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="messaging_profile_id",
                type=ToolParameterType.STRING,
                description=(
                    "Telnyx Messaging Profile id. Found under "
                    "Messaging → Messaging Profiles. The profile is "
                    "what binds your number(s) to the webhook URL — "
                    "configure the profile's ``Webhook URL`` to "
                    '``<public-url>/api/telnyx/messages/webhook``.'
                ),
                default="",
            ),
        ]

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key") or "")
        self._messaging_profile_id = str(
            config.get("messaging_profile_id") or ""
        )
        if not self._api_key:
            logger.warning("TelnyxMessaging initialized without an api_key")
        if self._http is not None:
            await self._http.aclose()
        self._http = httpx.AsyncClient(
            base_url=_TELNYX_API,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def bind_inbound_deliverer(self, deliverer: InboundDeliverer) -> None:
        """Called by ``MessagingService`` at startup. We store the
        reference; the webhook service reads it when parsing inbound
        events."""
        self._inbound_deliverer = deliverer
        # Surface on the module-level singleton path too so the
        # capability adapter below can find it without holding a
        # backend reference.
        _set_inbound_deliverer(deliverer)

    async def send_message(
        self,
        *,
        to: str,
        body: str,
        from_number: str = "",
        media_urls: list[str] | None = None,
    ) -> str:
        if self._http is None:
            raise RuntimeError("TelnyxMessaging is not initialized")
        if not from_number:
            raise RuntimeError(
                "Telnyx requires an explicit from_number on every send"
            )
        payload: dict[str, Any] = {
            "from": from_number,
            "to": to,
            "text": body,
        }
        # MMS attachments — only included when present so plain SMS
        # routes stay on the cheaper path.
        if media_urls:
            payload["media_urls"] = list(media_urls)
        # Bind the message to the profile if configured. Without this,
        # Telnyx uses whatever default the from-number is on, which is
        # usually fine but harder to debug.
        if self._messaging_profile_id:
            payload["messaging_profile_id"] = self._messaging_profile_id

        resp = await self._http.post("messages", json=payload)
        if resp.status_code >= 400:
            # Pull as much detail as Telnyx gave us so the FAILED row
            # in storage has something to debug from.
            detail = resp.text[:500]
            raise RuntimeError(
                f"Telnyx /v2/messages returned {resp.status_code}: {detail}"
            )
        data = resp.json()
        msg = data.get("data") or {}
        msg_id = str(msg.get("id") or "")
        if not msg_id:
            raise RuntimeError(
                f"Telnyx /v2/messages 200 but missing data.id: {data!r}"
            )
        logger.info(
            "TelnyxMessaging: sent id=%s to=%s from=%s len=%d",
            msg_id,
            to,
            from_number,
            len(body),
        )
        return msg_id


# ── Webhook parsing / dispatch ────────────────────────────────────────
#
# Module-level state so the capability adapter (a Service) and the
# Backend (instantiated by MessagingService) share one inbound-deliverer
# slot. Mirrors the voice side's module-level session registry.


_inbound_deliverer: InboundDeliverer | None = None


def _set_inbound_deliverer(d: InboundDeliverer) -> None:
    global _inbound_deliverer
    _inbound_deliverer = d


async def deliver_messaging_webhook(payload: dict[str, Any]) -> None:
    """Parse a Telnyx messaging webhook payload and dispatch any
    inbound message to the bound deliverer.

    Telnyx sends one event per webhook fire; the envelope looks like:

        {"data": {"event_type": "message.received",
                  "payload": {"id": "...", "from": {...}, "to": [...],
                              "text": "...", "received_at": "...",
                              "media": [{"url": "..."}]}}}

    Status-update events (``message.sent``, ``message.finalized``,
    ``message.failed``) are logged but not dispatched — the outbound
    flow persists status at send time and doesn't need the carrier
    confirmation to update the SPA today (could be a follow-up).
    """
    data = payload.get("data") or {}
    event_type = str(data.get("event_type") or "")
    msg = data.get("payload") or {}

    if event_type != "message.received":
        logger.debug(
            "TelnyxMessaging: ignoring webhook event_type=%r id=%s",
            event_type,
            msg.get("id"),
        )
        return

    if _inbound_deliverer is None:
        logger.warning(
            "TelnyxMessaging: inbound %s dropped — no MessagingService "
            "deliverer bound (is the messaging plugin loaded + enabled?)",
            msg.get("id"),
        )
        return

    # Extract the fields we care about. Telnyx wraps everything in
    # objects (from is {"phone_number": "+1..."}; to is a list of the
    # same; media is a list of {"url": "...", "content_type": "..."}).
    from_obj = msg.get("from") or {}
    to_list = msg.get("to") or []
    media = msg.get("media") or []

    other_number = ""
    if isinstance(from_obj, dict):
        other_number = str(from_obj.get("phone_number") or "")

    our_number = ""
    if isinstance(to_list, list) and to_list:
        first = to_list[0]
        if isinstance(first, dict):
            our_number = str(first.get("phone_number") or "")

    media_urls: list[str] = []
    if isinstance(media, list):
        for m in media:
            if isinstance(m, dict):
                url = str(m.get("url") or "")
                if url:
                    media_urls.append(url)

    message = Message(
        message_id=str(msg.get("id") or ""),
        user_id="",  # MessagingService resolves from owner_user_id
        our_number=our_number,
        other_number=other_number,
        direction=MessageDirection.INBOUND.value,
        body=str(msg.get("text") or ""),
        status=MessageStatus.RECEIVED.value,
        created_at=str(msg.get("received_at") or ""),
        media_urls=media_urls,
        backend="telnyx",
    )

    try:
        await _inbound_deliverer(message)
    except Exception:
        logger.exception(
            "TelnyxMessaging: inbound delivery raised for id=%s",
            message.message_id,
        )


# ── Capability adapter — core route resolves this ────────────────────


class TelnyxMessagingWebhookService(Service):
    """Capability exposed to core's ``/api/telnyx/messages/webhook``
    route. Mirrors the voice side's ``TelnyxWebhookService`` —
    keeps ``web/`` from importing this plugin module directly.
    """

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="telnyx_messaging_webhook",
            capabilities=frozenset({"telnyx_messaging_webhook"}),
            requires=frozenset(),
            optional=frozenset(),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def deliver_webhook_event(self, payload: dict[str, object]) -> None:
        await deliver_messaging_webhook(dict(payload))
