"""``MentraService`` — orchestrates Mentra smart-glasses sessions.

Shape:

- ``Service`` + ``Configurable`` + ``WsHandlerProvider``
- Capabilities: ``mentra`` (Gilbert-side identity), ``mentra_webhook``
  (carrier-style webhook delivery for core's
  ``/api/mentra/webhook`` route).
- Owns the email → Gilbert ``user_id`` mapping in the
  ``mentra_user_mappings`` entity collection. Without a mapping for
  the inbound ``userId`` the service refuses to open a session —
  better to drop than to attribute glasses input to the wrong
  Gilbert user.

Per-session flow:

1. Webhook arrives at ``/api/mentra/webhook``. Core resolves the
   ``mentra_webhook`` capability (this service) and calls
   ``deliver_webhook_event(payload)``.
2. For ``session_request``, we look up the Mentra ``userId`` in the
   mapping table → Gilbert ``UserContext``. If unknown, refuse with
   ``status=error``.
3. Construct ``WebSocketTransport`` against the cloud-supplied
   ``websocketUrl`` with the standard auth headers (apiKey,
   sessionId, userId, packageName).
4. Construct ``MentraSession``, register the transcription handler
   that dispatches finals into ``AIService.chat(source="mentra")``,
   wire the AI response back to ``session.display`` +
   ``session.speaker.speak``.
5. ``await session.connect()`` runs the handshake. Cloud responds
   with capabilities → we know which managers can do anything.
6. On ``stop_request``, tear down the session and forget the
   mapping.

This service is intentionally narrow — most of the heavy lifting
happens in the ``MentraSession`` + manager layer. The service is
the place where Gilbert's identity model and the Mentra protocol
meet.
"""

from __future__ import annotations

import logging
from typing import Any

from gilbert.interfaces.ai import AIProvider
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.context import set_current_user
from gilbert.interfaces.events import Event, EventBusProvider
from gilbert.interfaces.mentra import (
    SessionWebhookRequest,
    StopRequestReason,
    StopWebhookRequest,
    WebhookRequestType,
    WebhookResponse,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import (
    Filter,
    FilterOp,
    Query,
    StorageProvider,
)
from gilbert.interfaces.tools import ToolParameterType

from .session import MentraSession, MentraSessionConfig, WebSocketTransport

logger = logging.getLogger(__name__)


# Storage collection for the email → user_id mapping.
_MAPPINGS_COLLECTION = "mentra_user_mappings"


_DEFAULT_SYSTEM_PROMPT = (
    "You are Gilbert, the user's AI assistant, replying through their "
    "smart glasses. The user spoke a request via the glasses' "
    "microphone and your reply will be shown on a small heads-up "
    "display AND read aloud through the speaker. Keep replies VERY "
    "SHORT — one or two sentences, no markdown, no bullet lists, no "
    "formal sign-offs. Glasses screens are tiny; speak naturally for "
    "the TTS path; assume the user is mid-activity and can't read a "
    "wall of text."
    "\n\n"
    "If the question genuinely needs a long answer (a recipe, a "
    "summary, a list), respond with a TWO-SENTENCE summary and offer "
    "to send the rest to their phone or chat — the user can ask "
    "\"send the full thing\" to get it. Don't dump paragraphs onto "
    "the glasses display."
)


class MentraService(Service):
    """Mentra smart-glasses orchestration service.

    Capabilities provided: ``mentra``, ``mentra_webhook``,
    ``ws_handlers``.
    Capabilities consumed: ``entity_storage``, ``ai_chat`` (required —
    no point in glasses input without an AI to dispatch to), plus
    ``event_bus`` and ``configuration`` (optional).
    """

    slash_namespace = "mentra"

    def __init__(self) -> None:
        # ── Config-driven state ───────────────────────────────────
        self._enabled: bool = False
        self._api_key: str = ""
        self._package_name: str = ""
        self._tts_via_cloud: bool = True
        self._system_prompt: str = _DEFAULT_SYSTEM_PROMPT
        self._display_duration_ms: int = 8000

        # ── Resolved dependencies ─────────────────────────────────
        self._resolver: ServiceResolver | None = None
        self._storage: StorageProvider | None = None
        self._ai: AIProvider | None = None
        self._bus: Any = None

        # ── Live session registry — keyed by Mentra sessionId ─────
        self._sessions: dict[str, MentraSession] = {}

    # ── Service lifecycle ──────────────────────────────────────────

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="mentra",
            capabilities=frozenset(
                {"mentra", "mentra_webhook", "ws_handlers"}
            ),
            requires=frozenset({"entity_storage", "ai_chat"}),
            optional=frozenset({"configuration", "event_bus"}),
            toggleable=True,
            toggle_description=(
                "Mentra smart-glasses platform — heads-up Gilbert on "
                "Even Realities G1, Vuzix Z100, Mentra Live."
            ),
        )

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        storage = resolver.get_capability("entity_storage")
        if isinstance(storage, StorageProvider):
            self._storage = storage
        ai_svc = resolver.get_capability("ai_chat")
        if isinstance(ai_svc, AIProvider):
            self._ai = ai_svc
        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._bus = bus_svc.bus

        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if isinstance(config_svc, ConfigurationReader):
            section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Mentra service disabled")
            return

        self._enabled = True
        await self._apply_config(section)

        if not self._api_key or not self._package_name:
            logger.warning(
                "Mentra service enabled but missing api_key / "
                "package_name in /settings — webhook deliveries will "
                "be refused until both are set"
            )
            return

        logger.info(
            "Mentra service started — package=%s tts_via_cloud=%s",
            self._package_name,
            self._tts_via_cloud,
        )

    async def stop(self) -> None:
        # Close every live session before the service goes away.
        for sid, session in list(self._sessions.items()):
            try:
                await session.disconnect()
            except Exception:
                logger.exception(
                    "Failed to close Mentra session %s during shutdown",
                    sid,
                )
        self._sessions.clear()

    # ── Configurable ───────────────────────────────────────────────

    @property
    def config_namespace(self) -> str:
        return "mentra"

    @property
    def config_category(self) -> str:
        return "Mentra"

    def config_params(self) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description=(
                    "Mentra app API key (from the MentraOS developer "
                    "console). Used both in the WebSocket upgrade "
                    "headers and the first JSON frame to authenticate "
                    "this app to the cloud."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="package_name",
                type=ToolParameterType.STRING,
                description=(
                    "Mentra app package identifier (typically reverse-"
                    'DNS like "com.example.gilbert"). Must match the '
                    "package registered in the Mentra developer "
                    "console."
                ),
                default="",
            ),
            ConfigParam(
                key="tts_via_cloud",
                type=ToolParameterType.BOOLEAN,
                description=(
                    "When enabled, AI replies are spoken via Mentra "
                    "Cloud's built-in TTS (consistent voice across "
                    "Mentra apps). When disabled, the plugin falls "
                    "back to showing the reply on-display only — "
                    "useful when you want voice consistency with "
                    "Gilbert's other speakers (Sonos, Browser TTS) "
                    "and plan to wire those up separately later."
                ),
                default=True,
            ),
            ConfigParam(
                key="display_duration_ms",
                type=ToolParameterType.INTEGER,
                description=(
                    "How long an AI reply stays on the glasses "
                    "display before auto-clearing, in milliseconds. "
                    "Set 0 for indefinite (until replaced)."
                ),
                default=8000,
            ),
            ConfigParam(
                key="system_prompt",
                type=ToolParameterType.STRING,
                description=(
                    "System prompt the LLM uses when responding to "
                    "voice input from the glasses. Tuned for brevity "
                    "(small display, audio readback) — keep it tight."
                ),
                default=_DEFAULT_SYSTEM_PROMPT,
                multiline=True,
                ai_prompt=True,
            ),
        ]

    async def on_config_changed(self, config: dict[str, Any]) -> None:
        await self._apply_config(config)

    async def _apply_config(self, section: dict[str, Any]) -> None:
        self._api_key = str(section.get("api_key") or "")
        self._package_name = str(section.get("package_name") or "")
        self._tts_via_cloud = bool(section.get("tts_via_cloud", True))
        try:
            self._display_duration_ms = int(
                section.get("display_duration_ms") or 8000
            )
        except (TypeError, ValueError):
            self._display_duration_ms = 8000
        self._system_prompt = str(
            section.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT
        )

    # ── MentraWebhookEndpoint impl ─────────────────────────────────

    async def deliver_webhook_event(
        self, payload: dict[str, object]
    ) -> WebhookResponse:
        """Dispatch one webhook payload — either a session start or
        a session stop. Always returns a ``WebhookResponse`` (never
        raises) so the route stays at 200."""
        msg_type = str(payload.get("type") or "")
        try:
            if msg_type == WebhookRequestType.SESSION_REQUEST.value:
                return await self._handle_session_request(payload)
            if msg_type == WebhookRequestType.STOP_REQUEST.value:
                return await self._handle_stop_request(payload)
        except Exception:
            logger.exception(
                "Mentra webhook dispatch raised for type=%r", msg_type
            )
            return WebhookResponse(
                status="error",
                message="internal dispatch error",
            )
        logger.warning("Mentra webhook with unknown type=%r", msg_type)
        return WebhookResponse(
            status="error", message=f"unknown webhook type: {msg_type}"
        )

    async def _handle_session_request(
        self, payload: dict[str, object]
    ) -> WebhookResponse:
        if not self._enabled:
            return WebhookResponse(
                status="error", message="mentra service disabled"
            )
        if not self._api_key or not self._package_name:
            return WebhookResponse(
                status="error",
                message="mentra service missing api_key / package_name",
            )
        if self._ai is None:
            return WebhookResponse(
                status="error", message="ai_chat capability unavailable"
            )

        req = _parse_session_request(payload)
        if not req.session_id or not req.user_id:
            return WebhookResponse(
                status="error",
                message="webhook missing sessionId / userId",
            )
        ws_url = req.resolved_websocket_url
        if not ws_url:
            return WebhookResponse(
                status="error",
                message="webhook missing websocketUrl",
            )

        # Resolve Mentra email → Gilbert user. Refuse if unknown.
        gilbert_user = await self._resolve_user(req.user_id)
        if gilbert_user is None:
            logger.warning(
                "Mentra session_request for unmapped user %r — drop",
                req.user_id,
            )
            return WebhookResponse(
                status="error",
                message=(
                    "no Gilbert user mapping configured for "
                    f"{req.user_id!r}"
                ),
            )

        # Tear down any existing session with the same id (cloud
        # sometimes re-fires the webhook after a transient drop).
        old = self._sessions.pop(req.session_id, None)
        if old is not None:
            try:
                await old.disconnect()
            except Exception:
                logger.exception(
                    "Failed to close old Mentra session %s", req.session_id
                )

        transport = WebSocketTransport(
            url=ws_url,
            headers={
                "x-api-key": self._api_key,
                "x-package-name": self._package_name,
                "x-session-id": req.session_id,
                "x-user-id": req.user_id,
            },
        )
        config = MentraSessionConfig(
            package_name=self._package_name,
            api_key=self._api_key,
            session_id=req.session_id,
            user_id=req.user_id,
            gilbert_user_id=gilbert_user.user_id,
        )
        session = MentraSession(config=config, transport=transport)

        # Wire the transcription → AI → display+TTS loop. Pre-bind
        # the UserContext into the closure's task-local context so the
        # AI service sees the right caller identity.
        self._wire_session(session, gilbert_user)

        try:
            await session.connect()
        except Exception as exc:
            logger.exception(
                "Mentra session connect failed for %s", req.session_id
            )
            return WebhookResponse(
                status="error",
                message=f"websocket connect failed: {exc}",
            )

        self._sessions[req.session_id] = session
        await self._publish_bus_event(
            "mentra.session_started",
            {
                "session_id": req.session_id,
                "user_id": gilbert_user.user_id,
                "mentra_user": req.user_id,
            },
        )
        # Welcome card on the display so the user knows we're live.
        try:
            await session.display.show_text_wall(
                "Gilbert ready.", duration_ms=3000
            )
        except Exception:
            logger.debug("Mentra welcome display failed", exc_info=True)

        return WebhookResponse(status="success")

    async def _handle_stop_request(
        self, payload: dict[str, object]
    ) -> WebhookResponse:
        req = _parse_stop_request(payload)
        session = self._sessions.pop(req.session_id, None)
        if session is not None:
            try:
                await session.disconnect()
            except Exception:
                logger.exception(
                    "Mentra disconnect raised for session %s",
                    req.session_id,
                )
        await self._publish_bus_event(
            "mentra.session_stopped",
            {
                "session_id": req.session_id,
                "mentra_user": req.user_id,
                "reason": req.reason,
            },
        )
        return WebhookResponse(status="success")

    # ── Session wiring ─────────────────────────────────────────────

    def _wire_session(
        self, session: MentraSession, user: UserContext
    ) -> None:
        """Attach the per-session handlers that turn transcription
        events into AI dispatches and play the response back."""

        async def on_final_transcript(data: Any) -> None:
            # Only react to final (committed) transcripts. Partials
            # are useful for UI feedback but the AI shouldn't fire
            # on every keystroke.
            if not getattr(data, "is_final", False):
                return
            text = (getattr(data, "text", "") or "").strip()
            if not text:
                return
            await self._dispatch_to_ai(session, user, text)

        session.transcription.on_transcription(on_final_transcript)

        async def on_disconnect(code: int, reason: str) -> None:
            self._sessions.pop(session.session_id, None)
            logger.info(
                "Mentra session %s closed: code=%s reason=%r",
                session.session_id,
                code,
                reason,
            )

        session.on_disconnected(on_disconnect)

    async def _dispatch_to_ai(
        self, session: MentraSession, user: UserContext, text: str
    ) -> None:
        """Run ``text`` through the AI service as if it were a chat
        turn from ``user``, and pipe the response back to the
        glasses (display + optional TTS).

        Each session has its own WS reader task, so per-session
        ``UserContext`` doesn't bleed across sessions — we pass
        ``user_ctx=user`` explicitly anyway as belt-and-suspenders
        (the AI service prefers the explicit param over the
        ContextVar) and ALSO set the ContextVar so any tools the AI
        invokes inherit the right identity."""
        if self._ai is None:
            return
        set_current_user(user)
        try:
            result = await self._ai.chat(
                user_message=text,
                user_ctx=user,
                system_prompt=self._system_prompt,
            )
        except Exception:
            logger.exception(
                "Mentra AI dispatch failed for session %s",
                session.session_id,
            )
            await session.display.show_text_wall(
                "Gilbert had an error.",
                duration_ms=3000,
            )
            return
        reply = (result.response_text or "").strip()
        if not reply:
            return
        await self._render_reply(session, reply)

    async def _render_reply(
        self, session: MentraSession, reply: str
    ) -> None:
        """Send the reply to the glasses — display + TTS (if
        cloud-side TTS is enabled).

        Long replies get a reference-card layout with the first two
        sentences; very long replies are truncated with an ellipsis
        so the display stays readable.
        """
        snippet = _summarize_for_display(reply)
        duration = (
            self._display_duration_ms
            if self._display_duration_ms > 0
            else None
        )
        try:
            await session.display.show_text_wall(
                snippet, duration_ms=duration
            )
        except Exception:
            logger.debug(
                "Mentra display.show_text_wall raised", exc_info=True
            )
        if self._tts_via_cloud:
            try:
                await session.speaker.speak(reply)
            except Exception:
                logger.debug("Mentra speaker.speak raised", exc_info=True)

    # ── User mapping ───────────────────────────────────────────────

    async def _resolve_user(
        self, mentra_user_id: str
    ) -> UserContext | None:
        """Map a Mentra ``userId`` (email) to a Gilbert UserContext.

        The mapping table is keyed by the Mentra-side email. If no
        row exists, we refuse the session — auto-creating users
        would be a surprise; the operator should explicitly opt
        each Mentra account in via the Settings UI.
        """
        if self._storage is None:
            return None
        try:
            rows = await self._storage.backend.query(
                Query(
                    collection=_MAPPINGS_COLLECTION,
                    filters=[
                        Filter(
                            field="mentra_user_id",
                            op=FilterOp.EQ,
                            value=mentra_user_id,
                        ),
                    ],
                    limit=1,
                )
            )
        except Exception:
            logger.exception(
                "Mentra mapping lookup failed for %s", mentra_user_id
            )
            return None
        if not rows:
            return None
        row = rows[0]
        gilbert_user_id = str(row.get("gilbert_user_id") or "")
        if not gilbert_user_id:
            return None
        return UserContext(
            user_id=gilbert_user_id,
            email=mentra_user_id,
            display_name=str(row.get("display_name") or mentra_user_id),
            roles=frozenset(row.get("roles") or {"user"}),
            provider="mentra",
        )

    # ── Bus / WS plumbing ──────────────────────────────────────────

    async def _publish_bus_event(
        self, event_type: str, data: dict[str, Any]
    ) -> None:
        if self._bus is None:
            return
        try:
            await self._bus.publish(
                Event(
                    event_type=event_type,
                    data=data,
                    source="mentra",
                )
            )
        except Exception:
            logger.debug(
                "Mentra bus publish failed for %s",
                event_type,
                exc_info=True,
            )


# ── Helpers ──────────────────────────────────────────────────────────


def _parse_session_request(
    payload: dict[str, object],
) -> SessionWebhookRequest:
    """Lift a raw JSON dict into the typed dataclass. Accepts both
    the modern ``websocketUrl`` field and the deprecated aliases."""
    return SessionWebhookRequest(
        session_id=str(payload.get("sessionId") or ""),
        user_id=str(payload.get("userId") or ""),
        timestamp=str(payload.get("timestamp") or ""),
        websocket_url=str(payload.get("websocketUrl") or ""),
        mentraos_websocket_url_alias=str(
            payload.get("mentraOSWebsocketUrl") or ""
        ),
        augmentos_websocket_url_alias=str(
            payload.get("augmentOSWebsocketUrl") or ""
        ),
    )


def _parse_stop_request(payload: dict[str, object]) -> StopWebhookRequest:
    raw_reason = str(payload.get("reason") or "")
    try:
        reason = StopRequestReason(raw_reason).value
    except ValueError:
        reason = StopRequestReason.SYSTEM_STOP.value
    return StopWebhookRequest(
        session_id=str(payload.get("sessionId") or ""),
        user_id=str(payload.get("userId") or ""),
        timestamp=str(payload.get("timestamp") or ""),
        reason=reason,
    )


def _summarize_for_display(text: str, *, max_chars: int = 200) -> str:
    """Trim an AI reply for the glasses display.

    Glasses screens fit roughly two-three sentences before the
    user has to scroll (which they can't, on a non-interactive
    display). Cap at ``max_chars`` and add an ellipsis when
    truncated so the user knows there's more (and can ask "say
    that again, longer" or similar)."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    # Try to break on a sentence boundary near the limit.
    cut = text.rfind(". ", 0, max_chars)
    if cut > max_chars // 2:
        return text[: cut + 1] + " …"
    return text[: max_chars - 1] + "…"


