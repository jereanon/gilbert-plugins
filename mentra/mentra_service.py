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
import uuid
from collections import deque
from datetime import UTC, datetime
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

# Per-user ring-buffer cap for debug events. 50 is enough to cover
# the full session-admit → first-utterance → AI-reply → audio-
# response loop on the in-glasses-app companion webview without
# burning memory.
_EVENTS_PER_USER = 50


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
        # Tracks when each live session was admitted (ISO8601 UTC).
        # Surfaced via the ``mentra.sessions.list`` WS RPC so the
        # admin SPA can show "connected 4m ago" without reaching
        # into the session object's internals.
        self._connected_at: dict[str, str] = {}
        # Per-Mentra-user ring buffer of recent events. Used by the
        # in-glasses-app companion webview to surface live debug
        # state to the user's phone. Keyed by Mentra ``userId``
        # (email) so the webview can resolve user from JWT and
        # show only their own events.
        self._events: dict[str, deque[dict[str, Any]]] = {}

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

    # ── WsHandlerProvider ──────────────────────────────────────────

    def get_ws_handlers(self) -> dict[str, Any]:
        """RPC handlers backing the admin SPA panel at ``/mentra``.

        Five frame types: list/create/update/delete on the user-mapping
        collection, plus a read-only ``sessions.list`` for the live
        session table. All five are admin-only — the panel is for
        the operator who configured the integration, not end users.
        """
        return {
            "mentra.mappings.list": self._ws_mappings_list,
            "mentra.mappings.create": self._ws_mappings_create,
            "mentra.mappings.update": self._ws_mappings_update,
            "mentra.mappings.delete": self._ws_mappings_delete,
            "mentra.sessions.list": self._ws_sessions_list,
        }

    async def _ws_mappings_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        gate = _require_admin(conn, frame)
        if gate is not None:
            return gate
        if self._storage is None:
            return _err(frame, 503, "storage unavailable")
        try:
            rows = await self._storage.backend.query(
                Query(collection=_MAPPINGS_COLLECTION, limit=10_000)
            )
        except Exception:
            logger.exception("Mentra mappings list failed")
            return _err(frame, 500, "failed to list mappings")
        rows.sort(key=lambda r: str(r.get("created_at") or ""))
        return {
            "type": "mentra.mappings.list.result",
            "ref": frame.get("id"),
            "mappings": [_mapping_to_dict(r) for r in rows],
        }

    async def _ws_mappings_create(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        gate = _require_admin(conn, frame)
        if gate is not None:
            return gate
        if self._storage is None:
            return _err(frame, 503, "storage unavailable")
        mentra_user_id = str(frame.get("mentra_user_id") or "").strip()
        gilbert_user_id = str(frame.get("gilbert_user_id") or "").strip()
        if not mentra_user_id or not gilbert_user_id:
            return _err(
                frame,
                400,
                "mentra_user_id and gilbert_user_id are required",
            )
        display_name = str(frame.get("display_name") or "").strip()
        roles_raw = frame.get("roles") or ["user"]
        if not isinstance(roles_raw, list):
            return _err(frame, 400, "roles must be a list of strings")
        roles = [str(r) for r in roles_raw if str(r).strip()]
        if not roles:
            roles = ["user"]

        # Refuse to silently overwrite an existing mapping for the
        # same Mentra account — the admin almost certainly meant
        # "edit" rather than "create".
        try:
            existing = await self._storage.backend.query(
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
            logger.exception("Mentra mapping pre-check query failed")
            return _err(frame, 500, "storage error")
        if existing:
            return _err(
                frame,
                409,
                f"mapping for {mentra_user_id!r} already exists",
            )

        entity_id = f"map_{uuid.uuid4().hex[:16]}"
        row = {
            "id": entity_id,
            "mentra_user_id": mentra_user_id,
            "gilbert_user_id": gilbert_user_id,
            "display_name": display_name or mentra_user_id,
            "roles": roles,
            "created_at": _now_iso(),
        }
        try:
            await self._storage.backend.put(
                _MAPPINGS_COLLECTION, entity_id, row
            )
        except Exception:
            logger.exception(
                "Mentra mapping create persist failed for %s",
                mentra_user_id,
            )
            return _err(frame, 500, "failed to persist mapping")
        return {
            "type": "mentra.mappings.create.result",
            "ref": frame.get("id"),
            "mapping": _mapping_to_dict(row),
        }

    async def _ws_mappings_update(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        gate = _require_admin(conn, frame)
        if gate is not None:
            return gate
        if self._storage is None:
            return _err(frame, 503, "storage unavailable")
        # ``mapping_id`` not ``id`` — ``id`` on the frame is reserved
        # for the RPC envelope's correlation id (echoed as ``ref``).
        entity_id = str(frame.get("mapping_id") or "").strip()
        if not entity_id:
            return _err(frame, 400, "mapping_id is required")
        try:
            existing = await self._storage.backend.get(
                _MAPPINGS_COLLECTION, entity_id
            )
        except Exception:
            logger.exception(
                "Mentra mapping fetch failed for id=%s", entity_id
            )
            return _err(frame, 500, "storage error")
        if existing is None:
            return _err(frame, 404, f"mapping {entity_id!r} not found")

        merged = dict(existing)
        if "mentra_user_id" in frame:
            merged["mentra_user_id"] = str(
                frame.get("mentra_user_id") or ""
            ).strip()
        if "gilbert_user_id" in frame:
            merged["gilbert_user_id"] = str(
                frame.get("gilbert_user_id") or ""
            ).strip()
        if "display_name" in frame:
            merged["display_name"] = str(
                frame.get("display_name") or ""
            ).strip()
        if "roles" in frame:
            roles_raw = frame.get("roles") or []
            if not isinstance(roles_raw, list):
                return _err(
                    frame, 400, "roles must be a list of strings"
                )
            roles = [str(r) for r in roles_raw if str(r).strip()]
            merged["roles"] = roles or ["user"]
        if not merged.get("mentra_user_id") or not merged.get(
            "gilbert_user_id"
        ):
            return _err(
                frame,
                400,
                "mentra_user_id and gilbert_user_id must be non-empty",
            )
        # Preserve identity + created_at across the merge.
        merged["id"] = entity_id
        merged.setdefault("created_at", _now_iso())
        try:
            await self._storage.backend.put(
                _MAPPINGS_COLLECTION, entity_id, merged
            )
        except Exception:
            logger.exception(
                "Mentra mapping update persist failed for id=%s",
                entity_id,
            )
            return _err(frame, 500, "failed to persist mapping")
        return {
            "type": "mentra.mappings.update.result",
            "ref": frame.get("id"),
            "mapping": _mapping_to_dict(merged),
        }

    async def _ws_mappings_delete(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        gate = _require_admin(conn, frame)
        if gate is not None:
            return gate
        if self._storage is None:
            return _err(frame, 503, "storage unavailable")
        # ``mapping_id`` not ``id`` — see _ws_mappings_update.
        entity_id = str(frame.get("mapping_id") or "").strip()
        if not entity_id:
            return _err(frame, 400, "mapping_id is required")
        try:
            await self._storage.backend.delete(
                _MAPPINGS_COLLECTION, entity_id
            )
        except Exception:
            logger.exception(
                "Mentra mapping delete failed for id=%s", entity_id
            )
            return _err(frame, 500, "failed to delete mapping")
        return {
            "type": "mentra.mappings.delete.result",
            "ref": frame.get("id"),
            "status": "ok",
        }

    async def _ws_sessions_list(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        gate = _require_admin(conn, frame)
        if gate is not None:
            return gate
        sessions: list[dict[str, Any]] = []
        for sid, session in self._sessions.items():
            caps = session.capabilities
            caps_dict: dict[str, Any] = {}
            if caps is not None:
                caps_dict = {
                    "modelName": caps.model_name,
                    "hasCamera": caps.has_camera,
                    "hasDisplay": caps.has_display,
                    "hasMicrophone": caps.has_microphone,
                    "hasSpeaker": caps.has_speaker,
                    "hasImu": caps.has_imu,
                    "hasButton": caps.has_button,
                    "hasLight": caps.has_light,
                    "hasWifi": caps.has_wifi,
                }
            sessions.append(
                {
                    "session_id": sid,
                    "mentra_user_id": session.user_id,
                    "gilbert_user_id": session.gilbert_user_id,
                    "connected_at": self._connected_at.get(sid, ""),
                    "capabilities": caps_dict,
                }
            )
        # Most-recently-connected first so the SPA's top row is the
        # session the admin is most likely diagnosing.
        sessions.sort(
            key=lambda s: str(s.get("connected_at") or ""), reverse=True
        )
        return {
            "type": "mentra.sessions.list.result",
            "ref": frame.get("id"),
            "sessions": sessions,
        }

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
        self._connected_at.clear()

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
        self._connected_at.pop(req.session_id, None)
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
        self._connected_at[req.session_id] = _now_iso()
        caps = session.capabilities
        logger.info(
            "Mentra session admitted — session_id=%s mentra_user=%s "
            "gilbert_user=%s model=%s has_display=%s has_camera=%s "
            "has_mic=%s has_speaker=%s",
            req.session_id,
            req.user_id,
            gilbert_user.user_id,
            caps.model_name if caps else "<unknown>",
            caps.has_display if caps else False,
            caps.has_camera if caps else False,
            caps.has_microphone if caps else False,
            caps.has_speaker if caps else False,
        )
        await self._publish_bus_event(
            "mentra.session_started",
            {
                "session_id": req.session_id,
                "user_id": gilbert_user.user_id,
                "mentra_user": req.user_id,
            },
        )
        # Record events for the debug webview. The model + capability
        # summary tells the user-on-their-phone immediately whether
        # they paired the right device.
        self._record_event(
            req.user_id,
            "session_started",
            (
                f"Connected: {caps.model_name if caps else 'unknown model'} — "
                f"display={'yes' if caps and caps.has_display else 'no'} "
                f"camera={'yes' if caps and caps.has_camera else 'no'} "
                f"mic={'yes' if caps and caps.has_microphone else 'no'} "
                f"speaker={'yes' if caps and caps.has_speaker else 'no'}"
            ),
        )
        # Register an inbound-message handler for audio_play_response
        # so the debug webview can see success / failure / silence on
        # every TTS request — without this the only signal on Mentra
        # Live's audio issues is "nothing happens", which is
        # debugging hell.
        from .protocol.message_types import CloudToAppMessageType

        async def _on_audio_response(message: dict[str, Any]) -> None:
            request_id = str(message.get("requestId") or "")
            success = bool(message.get("success", True))
            if success:
                self._record_event(
                    req.user_id,
                    "audio_play_response",
                    f"Audio play succeeded (request_id={request_id[:24]}…)",
                    data={
                        "request_id": request_id,
                        "duration_ms": message.get("duration"),
                    },
                )
            else:
                err = message.get("error") or {}
                self._record_event(
                    req.user_id,
                    "audio_play_response",
                    f"Audio play FAILED — code={err.get('code')} msg={err.get('message')}",
                    level="error",
                    data={
                        "request_id": request_id,
                        "error": err,
                    },
                )

        session.on_message(
            CloudToAppMessageType.AUDIO_PLAY_RESPONSE.value,
            _on_audio_response,
        )

        # Watch settings updates for the ``useOnboardMic`` flag. When
        # it's ``false`` on Mentra Live + iOS, the cloud uses the
        # PHONE'S mic, and iOS's audio session rules force the
        # speaker output to MATCH the mic device — i.e. audio comes
        # out of the phone speaker (which is usually muted in
        # everyday use), NOT the glasses. This is a well-known
        # Mentra bug per upstream issue #1631 / #2275. We can't fix
        # it from our app, but we CAN surface the warning so the
        # operator knows to flip the setting in the MentraOS phone
        # app.
        async def _on_settings_update(message: dict[str, Any]) -> None:
            mentraos = message.get("mentraosSettings") or message.get("settings")
            if not isinstance(mentraos, dict):
                return
            use_onboard = mentraos.get("useOnboardMic")
            if use_onboard is False:
                self._record_event(
                    req.user_id,
                    "settings_warning",
                    (
                        "useOnboardMic is FALSE — the MentraOS phone "
                        "app is using your phone's mic, which forces "
                        "iOS to route speaker output to the phone "
                        "(not the glasses). Enable 'Use onboard mic' "
                        "in the MentraOS phone app's audio settings "
                        "to hear Gilbert through the glasses."
                    ),
                    level="warning",
                )

        session.on_message(
            CloudToAppMessageType.SETTINGS_UPDATE.value, _on_settings_update
        )
        # The connection_ack also carries mentraosSettings — capture
        # it on the initial admit too so the warning fires
        # immediately without waiting for a later settings_update.
        session.on_message(
            CloudToAppMessageType.CONNECTION_ACK.value, _on_settings_update
        )
        # Welcome the user — display + speaker, gated on whichever
        # surfaces the device actually has. Mentra Live has no
        # display (audio is the only feedback channel); Even
        # Realities G1 has a display but check before assuming
        # speaker — some SKUs don't ship one. Sending a frame for a
        # surface the device lacks silently drops at the cloud
        # without an error response.
        welcome_text = "Welcome to Gilbert."
        if caps is None or caps.has_display:
            try:
                await session.display.show_text_wall(
                    welcome_text, duration_ms=3000
                )
                logger.info(
                    "Mentra welcome display sent for session=%s",
                    req.session_id,
                )
            except Exception:
                logger.warning(
                    "Mentra welcome display failed for session=%s",
                    req.session_id,
                    exc_info=True,
                )
        if self._tts_via_cloud and (caps is None or caps.has_speaker):
            try:
                await session.speaker.speak(welcome_text)
                logger.info(
                    "Mentra welcome speech sent for session=%s",
                    req.session_id,
                )
                self._record_event(
                    req.user_id,
                    "audio_play_request",
                    f'Sent welcome TTS: "{welcome_text}"',
                )
            except Exception:
                logger.warning(
                    "Mentra welcome speech failed for session=%s",
                    req.session_id,
                    exc_info=True,
                )
                self._record_event(
                    req.user_id,
                    "audio_play_request",
                    "Welcome TTS send FAILED",
                    level="error",
                )

        return WebhookResponse(status="success")

    async def _handle_stop_request(
        self, payload: dict[str, object]
    ) -> WebhookResponse:
        req = _parse_stop_request(payload)
        session = self._sessions.pop(req.session_id, None)
        self._connected_at.pop(req.session_id, None)
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
            logger.info(
                "Mentra transcription final — session=%s len=%d text=%r",
                session.session_id,
                len(text),
                text[:100],
            )
            self._record_event(
                session.user_id,
                "transcription_final",
                f'You said: "{text[:200]}"',
            )
            await self._dispatch_to_ai(session, user, text)

        session.transcription.on_transcription(on_final_transcript)

        async def on_disconnect(code: int, reason: str) -> None:
            self._sessions.pop(session.session_id, None)
            self._connected_at.pop(session.session_id, None)
            logger.info(
                "Mentra session %s closed: code=%s reason=%r",
                session.session_id,
                code,
                reason,
            )
            self._record_event(
                session.user_id,
                "session_closed",
                f"Disconnected — code={code} reason={reason or '(none)'}",
                level="warning",
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
        except Exception as exc:
            logger.exception(
                "Mentra AI dispatch failed for session %s",
                session.session_id,
            )
            self._record_event(
                session.user_id,
                "ai_dispatch_failed",
                f"AI dispatch failed: {exc}",
                level="error",
            )
            await session.display.show_text_wall(
                "Gilbert had an error.",
                duration_ms=3000,
            )
            return
        reply = (result.response_text or "").strip()
        if not reply:
            logger.info(
                "Mentra AI returned empty response for session=%s",
                session.session_id,
            )
            self._record_event(
                session.user_id,
                "ai_reply_empty",
                "AI returned empty response",
                level="warning",
            )
            return
        logger.info(
            "Mentra AI reply — session=%s len=%d preview=%r",
            session.session_id,
            len(reply),
            reply[:100],
        )
        self._record_event(
            session.user_id,
            "ai_reply",
            f'Gilbert says: "{reply[:200]}"',
        )
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

    # ── Debug ring buffer (MentraDebugProvider) ──────────────────

    def _record_event(
        self,
        mentra_user_id: str,
        kind: str,
        message: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
    ) -> None:
        """Push an event to the per-user ring buffer for the debug
        webview. No-op if user id is empty (e.g. fired before user
        resolution). Capped at ``_EVENTS_PER_USER`` entries per user.
        """
        if not mentra_user_id:
            return
        buf = self._events.get(mentra_user_id)
        if buf is None:
            buf = deque(maxlen=_EVENTS_PER_USER)
            self._events[mentra_user_id] = buf
        buf.append(
            {
                "timestamp": _now_iso(),
                "kind": kind,
                "level": level,
                "message": message,
                "data": data or {},
            }
        )

    def get_recent_events(
        self, mentra_user_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Implements ``MentraDebugProvider.get_recent_events`` — used
        by core's ``/api/mentra/debug/events`` route. Returns most-
        recent events LAST (chronological order)."""
        buf = self._events.get(mentra_user_id)
        if buf is None:
            return []
        # Slice the deque (oldest → newest), bounded by limit.
        return list(buf)[-limit:]

    def get_active_session_summary(
        self, mentra_user_id: str
    ) -> dict[str, Any] | None:
        """Implements ``MentraDebugProvider.get_active_session_summary``.
        Returns the session-id, capabilities, and connection time of
        the live session for this Mentra user — or ``None`` if no
        live session exists."""
        for session_id, session in self._sessions.items():
            if session.user_id == mentra_user_id:
                caps = session.capabilities
                return {
                    "session_id": session_id,
                    "mentra_user_id": session.user_id,
                    "gilbert_user_id": session.gilbert_user_id,
                    "connected_at": self._connected_at.get(session_id, ""),
                    "model": caps.model_name if caps else "",
                    "capabilities": {
                        "has_display": caps.has_display if caps else False,
                        "has_camera": caps.has_camera if caps else False,
                        "has_microphone": caps.has_microphone if caps else False,
                        "has_speaker": caps.has_speaker if caps else False,
                    }
                    if caps
                    else {},
                }
        return None

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


def _now_iso() -> str:
    """ISO8601 UTC timestamp with a trailing ``Z`` rather than the
    ``+00:00`` Python prints by default — keeps the wire format
    consistent with the messaging service and SPA-side parsers."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _mapping_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize a stored mapping row to the WS wire shape.

    The storage layer is permissive (any JSON-serializable dict). The
    SPA expects every field present with sensible defaults — this
    keeps the TypeScript types tight without forcing the SPA to
    handle ``undefined`` everywhere."""
    roles_raw = row.get("roles") or []
    if isinstance(roles_raw, (list, tuple, set, frozenset)):
        roles = [str(r) for r in roles_raw]
    else:
        roles = [str(roles_raw)]
    return {
        "id": str(row.get("id") or ""),
        "mentra_user_id": str(row.get("mentra_user_id") or ""),
        "gilbert_user_id": str(row.get("gilbert_user_id") or ""),
        "display_name": str(row.get("display_name") or ""),
        "roles": roles,
        "created_at": str(row.get("created_at") or ""),
    }


def _require_admin(
    conn: Any, frame: dict[str, Any]
) -> dict[str, Any] | None:
    """Return an error frame if the connection isn't an admin, else
    ``None`` (= proceed).

    Checks the connection's role set per the user prompt's contract
    (``"admin"`` in ``conn.roles``). Falls back to the canonical
    numeric ``user_level <= 0`` check the rest of the codebase uses,
    so we accept either signal — useful for fakes in tests and for
    forward-compat if the role taxonomy evolves."""
    roles = getattr(conn, "roles", None)
    if roles is None:
        roles = getattr(conn, "user_roles", frozenset())
    try:
        if "admin" in roles:  # type: ignore[operator]
            return None
    except TypeError:
        pass
    user_level = getattr(conn, "user_level", 999)
    try:
        if int(user_level) <= 0:
            return None
    except (TypeError, ValueError):
        pass
    return {
        "type": "mentra.error",
        "ref": frame.get("id"),
        "code": 403,
        "message": "admin role required",
    }


def _err(
    frame: dict[str, Any], code: int, message: str
) -> dict[str, Any]:
    return {
        "type": "mentra.error",
        "ref": frame.get("id"),
        "code": code,
        "message": message,
    }


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


