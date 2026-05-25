"""``MessagingService`` — bidirectional text messaging orchestrator.

Shape:

- ``Service`` + ``Configurable`` + ``ToolProvider`` + ``WsHandlerProvider``
- Capability: ``messaging`` (advertised in ``service_info``)
- Loads one ``MessagingBackend`` selected by config (``backend``
  config key, defaulting to ``telnyx``) and forwards
  ``send_message`` through it.
- Persists every message (inbound + outbound) into the
  ``messages`` collection.
- Publishes bus events:
  - ``messaging.message_received`` — inbound, after persist
  - ``messaging.message_sent`` — outbound, after carrier accepted
  - ``messaging.thread_updated`` — either direction, after persist
- AI tool: ``send_text_message`` — single tool, role=user. Resolved
  via the standard Gilbert tool path (NOT ContextVar-gated; sending
  a text is a normal user-initiated action).
- WS handlers: ``messaging.threads.list``, ``messaging.thread.get``,
  ``messaging.send``.

Backend selection mirrors the other multi-backend services (TTS,
phone): the registered backend's ``backend_config_params()`` are
flattened in under ``settings.<key>`` and re-resolved on config
change.

Inbound delivery: the messaging-aware backend plugin (e.g. telnyx)
gets a reference to ``self._inbound_deliverer`` at startup via the
plugin's webhook-endpoint capability wiring. The backend parses raw
webhook payloads (carrier-specific shapes) into ``Message`` objects
and calls back. This service then runs the multi-user-routing /
persist / bus / optional-auto-reply flow.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.ai import (
    AIProvider,
    ConversationMessagePoster,
)
from gilbert.interfaces.configuration import (
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.messaging import (
    Message,
    MessageDirection,
    MessageStatus,
    MessageType,
    MessagingBackend,
    MessagingProvider,
    ThreadSummary,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    Query,
    SortField,
    StorageProvider,
)
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)

logger = logging.getLogger(__name__)


# Storage collection — flat, every row is one Message.
_COLLECTION = "messages"


_DEFAULT_AUTO_REPLY_PROMPT = (
    "You are Gilbert, replying to a text message on behalf of the user. "
    "Keep replies SHORT — texting tone, one or two sentences max. No "
    "markdown, no bullet lists, no formal sign-offs. The remote can "
    "only see your text (the AI tools you call are invisible to "
    "them), so anything you want them to know must be in the reply "
    "body itself."
    "\n\n"
    "You can ignore a message by responding with empty text — sometimes "
    "the right answer is to NOT reply (e.g. spam, group chat noise, "
    "things that obviously aren't directed at the user). Use your "
    "judgement."
)


class MessagingService(Service):
    """Bidirectional text messaging.

    Capabilities provided: ``messaging``, ``ai_tools``, ``ws_handlers``.
    Capabilities consumed: ``entity_storage``, ``event_bus``,
    ``ai_chat`` (optional — only used when ``auto_reply`` is enabled).
    """

    slash_namespace = "msg"

    def __init__(self) -> None:
        # --- config-driven state ---
        self._backend: MessagingBackend | None = None
        self._backend_name: str = "telnyx"
        self._enabled: bool = False
        self._from_number: str = ""
        self._auto_reply_enabled: bool = False
        self._auto_reply_system_prompt: str = _DEFAULT_AUTO_REPLY_PROMPT
        self._owner_user_id: str = ""
        self._config: dict[str, object] = {}
        # Default outbound transport preference. ``RCS`` per the
        # modern-first policy; the backend / carrier downgrades to
        # ``MMS`` (media + no RCS) or ``SMS`` (no media, no RCS).
        self._default_message_type: MessageType = MessageType.RCS

        # --- resolved dependencies ---
        self._resolver: ServiceResolver | None = None
        self._storage: StorageProvider | None = None
        self._bus: Any = None  # EventBus from EventBusProvider
        self._ai: AIProvider | None = None
        self._message_poster: ConversationMessagePoster | None = None

    # ── Service lifecycle ───────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="messaging",
            capabilities=frozenset({"messaging", "ai_tools", "ws_handlers"}),
            requires=frozenset({"entity_storage", "event_bus"}),
            optional=frozenset({"configuration", "ai_chat"}),
            toggleable=True,
            toggle_description="Bidirectional text messaging (SMS).",
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        storage = resolver.get_capability("entity_storage")
        if isinstance(storage, StorageProvider):
            self._storage = storage
        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._bus = bus_svc.bus
        ai_svc = resolver.get_capability("ai_chat")
        if isinstance(ai_svc, AIProvider):
            self._ai = ai_svc
        if isinstance(ai_svc, ConversationMessagePoster):
            self._message_poster = ai_svc

        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Messaging service disabled")
            return

        self._enabled = True
        await self._apply_config(section)

        if self._backend is None:
            logger.warning(
                "Messaging service enabled but no backend configured "
                "(set messaging.backend in /settings)"
            )
            return

        logger.info(
            "Messaging service started — backend=%s from=%s auto_reply=%s",
            self._backend_name,
            self._from_number or "<not configured>",
            self._auto_reply_enabled,
        )

    async def stop(self) -> None:
        if self._backend is not None:
            try:
                await self._backend.close()
            except Exception:
                logger.exception("Messaging backend close failed")
        self._backend = None

    # ── Configurable ────────────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "messaging"

    @property
    def config_category(self) -> str:
        return "Messaging"

    def config_params(self) -> list[ConfigParam]:
        registry = MessagingBackend.registered_backends()
        params: list[ConfigParam] = [
            ConfigParam(
                key="backend",
                type=ToolParameterType.STRING,
                description="Messaging backend provider.",
                default="telnyx",
                restart_required=True,
                choices=tuple(registry.keys()) or ("telnyx",),
            ),
            ConfigParam(
                key="from_number",
                type=ToolParameterType.STRING,
                description=(
                    "Shared E.164 sender number for outbound messages "
                    '(e.g. "+13035550100"). Must be a number you control '
                    "on the chosen messaging provider."
                ),
                default="",
            ),
            ConfigParam(
                key="owner_user_id",
                type=ToolParameterType.STRING,
                description=(
                    "Which Gilbert user owns the shared from_number. "
                    "Inbound messages route to this user's threads. "
                    "Required for inbound to work — outbound works "
                    "without it (uses the caller's user_id directly)."
                ),
                default="",
            ),
            ConfigParam(
                key="default_message_type",
                type=ToolParameterType.STRING,
                description=(
                    "Outbound transport preference when the caller "
                    "doesn't specify one. ``rcs`` is the modern "
                    "default (rich text, read receipts, media, no "
                    "per-segment length cap); the carrier falls "
                    "back to ``mms`` (media + no RCS) or ``sms`` "
                    "(no media, no RCS) per the recipient's "
                    "capabilities. Force ``sms`` to disable RCS "
                    "globally — useful if your messaging provider "
                    "charges differently for RCS or doesn't "
                    "support it for your number yet."
                ),
                default=str(MessageType.RCS.value),
                choices=tuple(t.value for t in MessageType),
            ),
            ConfigParam(
                key="auto_reply",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "When enabled, Gilbert reads each inbound message "
                    "and replies via the AI service automatically. When "
                    "disabled, inbound messages are persisted + appear "
                    "in the SPA but Gilbert stays quiet — the user can "
                    "still send manual replies from the UI."
                ),
                default=False,
            ),
            ConfigParam(
                key="auto_reply_system_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt the LLM uses when auto-replying to "
                    "an inbound text. Keep tight — texts are short by "
                    "convention and the LLM is reply-not-respond."
                ),
                default=_DEFAULT_AUTO_REPLY_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]
        # Flatten backend-specific config under settings.<key>.
        backend_cls = registry.get(self._backend_name)
        if backend_cls is not None:
            for bp in backend_cls.backend_config_params():
                params.append(
                    ConfigParam(
                        key=f"settings.{bp.key}",
                        type=bp.type,
                        description=bp.description,
                        default=bp.default,
                        restart_required=bp.restart_required,
                        sensitive=bp.sensitive,
                        choices=bp.choices,
                        multiline=bp.multiline,
                        ai_prompt=bp.ai_prompt,
                        backend_param=True,
                    )
                )
        return params

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        await self._apply_config(config)

    async def _apply_config(self, section: dict[str, Any]) -> None:
        self._from_number = str(section.get("from_number") or "")
        self._owner_user_id = str(section.get("owner_user_id") or "")
        self._auto_reply_enabled = bool(section.get("auto_reply", False))
        self._auto_reply_system_prompt = str(
            section.get("auto_reply_system_prompt")
            or _DEFAULT_AUTO_REPLY_PROMPT
        )
        # Parse the configured default — fall back to RCS on any
        # unknown value rather than crashing the service.
        raw_type = str(section.get("default_message_type") or "").lower()
        try:
            self._default_message_type = MessageType(raw_type)
        except ValueError:
            self._default_message_type = MessageType.RCS
        self._config = dict(section.get("settings") or {})

        backend_name = str(section.get("backend") or self._backend_name)
        registry = MessagingBackend.registered_backends()
        backend_cls = registry.get(backend_name)
        if backend_cls is None:
            logger.warning(
                "Messaging backend %r not registered (available: %s)",
                backend_name,
                ", ".join(registry.keys()) or "<none>",
            )
            self._backend = None
            self._backend_name = backend_name
            return

        if (
            self._backend is not None
            and self._backend_name == backend_name
        ):
            # Same backend instance, possibly new credentials.
            try:
                await self._backend.initialize(self._config)
            except Exception:
                logger.exception(
                    "Messaging backend reinitialize failed for %s",
                    backend_name,
                )
            return

        # Different backend (or first-time wiring) — instantiate fresh.
        if self._backend is not None:
            try:
                await self._backend.close()
            except Exception:
                logger.exception("Old messaging backend close failed")
        self._backend = backend_cls()
        self._backend_name = backend_name
        # Attach the inbound deliverer reference if the backend
        # advertises that hook — keeps the backend decoupled from
        # the service (it doesn't import this class).
        if hasattr(self._backend, "bind_inbound_deliverer"):
            try:
                self._backend.bind_inbound_deliverer(self.receive_inbound)
            except Exception:
                logger.exception(
                    "Backend bind_inbound_deliverer failed"
                )
        try:
            await self._backend.initialize(self._config)
        except Exception:
            logger.exception(
                "Messaging backend initialize failed for %s",
                backend_name,
            )
            self._backend = None

    # ── MessagingProvider impl ──────────────────────────────────────

    async def send(
        self,
        *,
        user_id: str,
        to_number: str,
        body: str,
        from_number: str = "",
        media_urls: list[str] | None = None,
        preferred_type: MessageType | None = None,
    ) -> Message:
        """Send an outbound message on behalf of ``user_id``. Persists
        + publishes the bus event in addition to handing off to the
        backend.

        ``preferred_type`` defaults to the configured
        ``default_message_type`` (``RCS`` out of the box). The carrier
        downgrades to ``MMS`` / ``SMS`` per recipient capability — the
        actual transport the carrier picked is stamped on
        ``Message.type``."""
        if self._backend is None:
            raise RuntimeError(
                "Messaging service has no backend configured — "
                "set messaging.backend in /settings"
            )
        if not to_number:
            raise ValueError("to_number is required")
        if not body:
            raise ValueError("body is required")
        our_number = from_number or self._from_number
        if not our_number:
            raise RuntimeError(
                "No from_number configured — set messaging.from_number"
            )
        resolved_pref = preferred_type or self._default_message_type

        # Send first; we want the backend-issued id as the row's
        # primary key. On failure we still record a row so the SPA
        # can show the error.
        msg_id = ""
        error = ""
        # Default the persisted ``type`` to the caller's preference —
        # on a failed send we never heard back from the carrier, so
        # preference is the best we have for the SPA badge.
        actual_type = resolved_pref.value
        try:
            result = await self._backend.send_message(
                to=to_number,
                body=body,
                from_number=our_number,
                media_urls=media_urls,
                preferred_type=resolved_pref,
            )
            msg_id = result.message_id
            actual_type = result.actual_type or resolved_pref.value
            status = MessageStatus.SENT.value
        except Exception as exc:
            error = str(exc)
            status = MessageStatus.FAILED.value
            # Mint a local id so the row is still indexable.
            msg_id = f"local_{uuid.uuid4().hex[:16]}"
            logger.exception(
                "Outbound message send failed for user=%s to=%s",
                user_id,
                to_number,
            )

        msg = Message(
            message_id=msg_id,
            user_id=user_id,
            our_number=our_number,
            other_number=to_number,
            direction=MessageDirection.OUTBOUND.value,
            body=body,
            status=status,
            created_at=_now_iso(),
            media_urls=list(media_urls or []),
            error=error,
            backend=self._backend_name,
            type=actual_type,
        )
        await self._persist(msg)
        await self._publish(
            "messaging.message_sent",
            {
                "message_id": msg.message_id,
                "user_id": msg.user_id,
                "our_number": msg.our_number,
                "other_number": msg.other_number,
                "body": msg.body,
                "status": msg.status,
                "created_at": msg.created_at,
                "media_urls": msg.media_urls,
                "error": msg.error,
                "type": msg.type,
            },
        )
        await self._publish(
            "messaging.thread_updated",
            {
                "user_id": msg.user_id,
                "our_number": msg.our_number,
                "other_number": msg.other_number,
                "last_message_at": msg.created_at,
                "last_message_direction": msg.direction,
                "last_message_preview": msg.body[:80],
            },
        )
        return msg

    async def list_threads(self, user_id: str) -> list[ThreadSummary]:
        if self._storage is None or not user_id:
            return []
        rows = await self._storage.backend.query(
            Query(
                collection=_COLLECTION,
                filters=[
                    Filter(field="user_id", op=FilterOp.EQ, value=user_id),
                ],
                sort=[SortField(field="created_at", descending=False)],
                limit=10_000,
            )
        )
        # Group by (our_number, other_number) — small enough to do in
        # Python; the storage backend doesn't do aggregation.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for r in rows:
            key = (
                str(r.get("our_number") or ""),
                str(r.get("other_number") or ""),
            )
            groups.setdefault(key, []).append(r)
        summaries: list[ThreadSummary] = []
        for (our, other), msgs in groups.items():
            if not msgs:
                continue
            last = msgs[-1]
            summaries.append(
                ThreadSummary(
                    user_id=user_id,
                    our_number=our,
                    other_number=other,
                    last_message_at=str(last.get("created_at") or ""),
                    last_message_preview=str(last.get("body") or "")[:80],
                    last_message_direction=str(last.get("direction") or ""),
                    unread_count=0,  # unread tracking is a TODO
                    message_count=len(msgs),
                )
            )
        # Most-recent first.
        summaries.sort(key=lambda s: s.last_message_at, reverse=True)
        return summaries

    async def get_messages(
        self,
        *,
        user_id: str,
        other_number: str,
        our_number: str = "",
        limit: int = 200,
    ) -> list[Message]:
        if self._storage is None:
            return []
        filters = [
            Filter(field="user_id", op=FilterOp.EQ, value=user_id),
            Filter(field="other_number", op=FilterOp.EQ, value=other_number),
        ]
        if our_number:
            filters.append(
                Filter(field="our_number", op=FilterOp.EQ, value=our_number)
            )
        rows = await self._storage.backend.query(
            Query(
                collection=_COLLECTION,
                filters=filters,
                sort=[SortField(field="created_at", descending=False)],
                limit=limit,
            )
        )
        return [_row_to_message(r) for r in rows]

    # ── Inbound delivery (called by backends) ───────────────────────

    async def receive_inbound(self, message: Message) -> None:
        """Backend pushes a parsed inbound ``Message`` here. The
        backend has filled in everything except ``user_id`` — we
        resolve that from ``our_number`` (the recipient on our side)
        via the ``owner_user_id`` config.

        Persists, publishes, optionally triggers AI auto-reply.
        """
        if not message.user_id:
            # Resolve recipient → owner. Single-user mapping for now;
            # multi-tenant would index by our_number.
            message = Message(
                **{**message.__dict__, "user_id": self._owner_user_id}
            )
        if not message.user_id:
            logger.warning(
                "Inbound message dropped — no owner_user_id configured "
                "for our_number=%s",
                message.our_number,
            )
            return

        await self._persist(message)
        await self._publish(
            "messaging.message_received",
            {
                "message_id": message.message_id,
                "user_id": message.user_id,
                "our_number": message.our_number,
                "other_number": message.other_number,
                "body": message.body,
                "status": message.status,
                "created_at": message.created_at,
                "media_urls": message.media_urls,
                "type": message.type,
            },
        )
        await self._publish(
            "messaging.thread_updated",
            {
                "user_id": message.user_id,
                "our_number": message.our_number,
                "other_number": message.other_number,
                "last_message_at": message.created_at,
                "last_message_direction": message.direction,
                "last_message_preview": message.body[:80],
            },
        )

        if self._auto_reply_enabled and self._ai is not None:
            try:
                await self._auto_reply(message)
            except Exception:
                logger.exception(
                    "Auto-reply failed for message %s", message.message_id
                )

    async def _auto_reply(self, inbound: Message) -> None:
        """Run the incoming text through the LLM and send the reply.
        Tagged with ``source="messaging"`` so the saved AI conversation
        doesn't pollute the chat sidebar."""
        if self._ai is None:
            return
        # Frame the inbound so the LLM knows it's responding to a text
        # from a specific number, not chatting with the user directly.
        framed = (
            f"(INBOUND TEXT from {inbound.other_number}) {inbound.body}"
        )
        result = await self._ai.chat(
            user_message=framed,
            system_prompt=self._auto_reply_system_prompt,
            source="messaging",
        )
        reply_text = (result.response_text or "").strip()
        if not reply_text:
            logger.info(
                "Auto-reply skipped — LLM produced empty response for "
                "message=%s",
                inbound.message_id,
            )
            return
        # Inherit the from/to mapping from the inbound message.
        await self.send(
            user_id=inbound.user_id,
            to_number=inbound.other_number,
            from_number=inbound.our_number,
            body=reply_text,
        )

    # ── Storage helpers ─────────────────────────────────────────────

    async def _persist(self, msg: Message) -> None:
        if self._storage is None:
            return
        try:
            await self._storage.backend.put(
                _COLLECTION,
                msg.message_id,
                _message_to_row(msg),
            )
        except Exception:
            logger.exception(
                "Failed to persist message %s", msg.message_id
            )

    async def _publish(self, event_type: str, data: dict[str, Any]) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(
                Event(
                    event_type=event_type,
                    data=data,
                    source="messaging",
                )
            )
        except Exception:
            logger.debug("Bus publish failed for %s", event_type, exc_info=True)

    # ── ToolProvider ────────────────────────────────────────────────

    @property
    def tool_provider_name(self) -> str:
        return "messaging"

    def get_tools(self, user_ctx: Any = None) -> list[ToolDefinition]:
        if not self._enabled or self._backend is None:
            return []
        return [
            ToolDefinition(
                name="send_text_message",
                slash_group="msg",
                slash_command="send",
                slash_help=(
                    "Send a text to a phone number. "
                    '/msg send "+13035550100" "Running late, be 10 min"'
                ),
                description=(
                    "Send a text message to a phone number on the "
                    "user's behalf via the configured messaging "
                    "provider. The recipient sees the message from "
                    "the shared Gilbert sender number. The carrier "
                    "picks the best transport available — RCS for "
                    "modern recipients (rich text, read receipts, "
                    "media, no length cap), falling back to MMS or "
                    "SMS when the recipient isn't RCS-capable."
                    "\n\n"
                    "Keep texts SHORT — one to three sentences max. "
                    "No markdown, no formal sign-offs. If the user "
                    "asked you to relay a specific phrasing, use "
                    "their phrasing verbatim. Otherwise summarize "
                    "in casual texting-style language."
                    "\n\n"
                    "Returns the message id and the actual transport "
                    "tier the carrier picked. Delivery status arrives "
                    "later via the carrier — the tool's success only "
                    "means the carrier accepted the message for "
                    "delivery."
                ),
                parameters=[
                    ToolParameter(
                        name="to_number",
                        type=ToolParameterType.STRING,
                        description=(
                            "Destination phone number in E.164 form "
                            '(e.g. "+13035550100").'
                        ),
                    ),
                    ToolParameter(
                        name="body",
                        type=ToolParameterType.STRING,
                        description=(
                            "Message body. Keep short; SMS-tier "
                            "fallback caps each segment at 160 chars "
                            "and multi-segment messages bill per "
                            "segment."
                        ),
                    ),
                    ToolParameter(
                        name="message_type",
                        type=ToolParameterType.STRING,
                        description=(
                            "Preferred transport tier. Defaults to "
                            "the service's configured default "
                            '(``rcs`` out of the box). Pass "sms" to '
                            "force the plain-text fallback (e.g. for "
                            "carriers where RCS billing is more "
                            'expensive). Pass "mms" when sending '
                            "media to a recipient without RCS."
                        ),
                        required=False,
                        enum=[t.value for t in MessageType],
                    ),
                ],
                required_role="user",
            ),
        ]

    async def execute_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> str:
        if name != "send_text_message":
            raise KeyError(name)
        # Per the Gilbert tool-handler convention: caller identity comes
        # from the async-local user context the AI orchestrator sets
        # right before invoking this provider.
        from gilbert.interfaces.auth import UserContext
        from gilbert.interfaces.context import get_current_user

        ctx = get_current_user()
        if ctx is UserContext.SYSTEM or not getattr(ctx, "user_id", ""):
            raise ValueError(
                "send_text_message must be invoked in the context of a user"
            )
        user_id = str(getattr(ctx, "user_id", ""))
        to_number = str(arguments.get("to_number") or "").strip()
        body = str(arguments.get("body") or "").strip()
        if not to_number or not body:
            raise ValueError("to_number and body are required")
        raw_type = str(arguments.get("message_type") or "").strip().lower()
        preferred: MessageType | None = None
        if raw_type:
            try:
                preferred = MessageType(raw_type)
            except ValueError:
                raise ValueError(
                    f"message_type must be one of "
                    f"{[t.value for t in MessageType]} (got {raw_type!r})"
                ) from None
        msg = await self.send(
            user_id=user_id,
            to_number=to_number,
            body=body,
            preferred_type=preferred,
        )
        return (
            f"Message sent. id={msg.message_id} "
            f"status={msg.status} type={msg.type or 'unknown'}"
        )

    # ── WsHandlerProvider ───────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        return {
            "messaging.threads.list": self._ws_threads_list,
            "messaging.thread.get": self._ws_thread_get,
            "messaging.send": self._ws_send,
        }

    async def _ws_threads_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = str(getattr(conn, "user_id", "") or "")
        threads = await self.list_threads(user_id)
        return {
            "type": "messaging.threads.list.result",
            "ref": frame.get("id"),
            "threads": [_thread_to_dict(t) for t in threads],
        }

    async def _ws_thread_get(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = str(getattr(conn, "user_id", "") or "")
        other = str(frame.get("other_number") or "")
        our = str(frame.get("our_number") or "")
        limit_raw = frame.get("limit") or 200
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            limit = 200
        if not user_id or not other:
            return _err(frame, 400, "user_id and other_number are required")
        msgs = await self.get_messages(
            user_id=user_id,
            other_number=other,
            our_number=our,
            limit=limit,
        )
        return {
            "type": "messaging.thread.get.result",
            "ref": frame.get("id"),
            "other_number": other,
            "our_number": our,
            "messages": [_message_to_row(m) for m in msgs],
        }

    async def _ws_send(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        user_id = str(getattr(conn, "user_id", "") or "")
        to = str(frame.get("to_number") or "")
        body = str(frame.get("body") or "")
        if not user_id or not to or not body:
            return _err(
                frame, 400, "user_id, to_number, and body are required"
            )
        raw_type = str(frame.get("preferred_type") or "").strip().lower()
        preferred: MessageType | None = None
        if raw_type:
            try:
                preferred = MessageType(raw_type)
            except ValueError:
                return _err(
                    frame,
                    400,
                    f"preferred_type must be one of "
                    f"{[t.value for t in MessageType]} (got {raw_type!r})",
                )
        try:
            msg = await self.send(
                user_id=user_id,
                to_number=to,
                body=body,
                preferred_type=preferred,
            )
        except RuntimeError as exc:
            return _err(frame, 503, str(exc))
        except ValueError as exc:
            return _err(frame, 400, str(exc))
        return {
            "type": "messaging.send.result",
            "ref": frame.get("id"),
            "message_id": msg.message_id,
            "status": msg.status,
            "message_type": msg.type,
        }


# ── module-private helpers ───────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _message_to_row(msg: Message) -> dict[str, Any]:
    return {
        "message_id": msg.message_id,
        "user_id": msg.user_id,
        "our_number": msg.our_number,
        "other_number": msg.other_number,
        "direction": msg.direction,
        "body": msg.body,
        "status": msg.status,
        "created_at": msg.created_at,
        "media_urls": list(msg.media_urls),
        "error": msg.error,
        "backend": msg.backend,
        "type": msg.type,
    }


def _row_to_message(row: dict[str, Any]) -> Message:
    return Message(
        message_id=str(row.get("message_id") or ""),
        user_id=str(row.get("user_id") or ""),
        our_number=str(row.get("our_number") or ""),
        other_number=str(row.get("other_number") or ""),
        direction=str(row.get("direction") or ""),
        body=str(row.get("body") or ""),
        status=str(row.get("status") or ""),
        created_at=str(row.get("created_at") or ""),
        media_urls=list(row.get("media_urls") or []),
        error=str(row.get("error") or ""),
        backend=str(row.get("backend") or ""),
        type=str(row.get("type") or ""),
    )


def _thread_to_dict(t: ThreadSummary) -> dict[str, Any]:
    return {
        "user_id": t.user_id,
        "our_number": t.our_number,
        "other_number": t.other_number,
        "last_message_at": t.last_message_at,
        "last_message_preview": t.last_message_preview,
        "last_message_direction": t.last_message_direction,
        "unread_count": t.unread_count,
        "message_count": t.message_count,
    }


def _err(frame: dict[str, Any], code: int, message: str) -> dict[str, Any]:
    return {
        "type": "messaging.error",
        "ref": frame.get("id"),
        "code": code,
        "message": message,
    }
