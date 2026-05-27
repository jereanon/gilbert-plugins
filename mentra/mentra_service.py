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
4. Construct ``MentraSession``, ``await session.connect()`` so the
   cloud handshake completes and we know which managers the device
   actually supports.
5. Build a ``_MentraConversationSession`` adapter around the live
   session and hand it to the ``voice_brain`` ConversationEngine
   capability via ``run_conversation(session, config)``. The engine
   owns the transcription → AI → TTS loop (with echo suppression,
   local VAD, barge-in, tool dispatch); this plugin just wires the
   transport.
6. On ``stop_request`` (or WS drop), cancel the engine task and
   forget the session.

This service is intentionally narrow — most of the heavy lifting
happens in the ``MentraSession`` + manager layer, with conversation
orchestration delegated to ``voice_brain``. The service is the
place where Gilbert's identity model and the Mentra protocol meet.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.ai import Message, MessageRole
from gilbert.interfaces.audio_blob import AudioBlobStore
from gilbert.interfaces.auth import UserContext
from gilbert.interfaces.configuration import (
    ConfigParam,
    ConfigurationReader,
)
from gilbert.interfaces.context import set_current_user
from gilbert.interfaces.conversation import (
    ConversationConfig,
    ConversationEngine,
    ConversationStatus,
    ConversationStatusEvent,
    OpeningBehavior,
    OpeningPolicy,
)
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
from gilbert.interfaces.tts import AudioFormat as TTSAudioFormat

from .session import MentraSession, MentraSessionConfig, WebSocketTransport
from .voice_session import _MentraAudioSink, _MentraConversationSession

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
    "\n\n"
    "Skip opening filler ('great question', 'happy to') and just "
    "answer. Do NOT start with 'hmm' / 'let me check' / 'one sec' — "
    "the runtime handles that automatically when a tool call is "
    "going to make you slow. If you open with a filler of your own, "
    "the user hears it TWICE."
)


# Short interjections the engine speaks while the LLM is still
# thinking, so the user doesn't sit in silence wondering if Gilbert
# heard them. Kicks in only when chat() takes longer than the
# threshold below (i.e. typically only on tool-using turns). Same
# pattern voice-agent uses.
_DEFAULT_FILLER_PHRASES = [
    "Hmm.",
    "Hmm, let me check.",
    "One sec.",
    "Let me look that up.",
    "Give me a moment.",
    "Hmm, looking.",
    "Just a sec.",
]

# Spoken if the LLM ends the conversation via an end_conversation
# tool call without including its own goodbye line. Casual register
# — the user is talking to their own assistant, no need for formal
# call-sign-off etiquette.
_DEFAULT_GOODBYE_PHRASES = [
    "Talk to you later!",
    "Catch you later.",
    "See you soon!",
    "Alright, later then.",
    "Bye for now!",
]

# How slow the LLM must be (in seconds) before the engine speaks a
# filler. 3.0s skips tool-free Q&A (typical ~1.5-2.5s end-to-end at
# Sonnet) but covers every knowledge.search / MCP call.
_FILLER_THRESHOLD_SECONDS = 3.0


class _NoopBrainToolProvider:
    """No-op ``BrainToolProvider`` for the ``use_full_ai_service=True``
    engine path.

    The engine requires a brain_tool_provider on its config, but in
    ``use_full_ai_service=True`` mode it routes tools through
    ``AIProvider.chat()``'s standard tool aggregation rather than
    the brain-tool provider. The provider's methods are never
    called. We still satisfy the protocol so the dataclass
    construction type-checks.

    If voice-agent is also loaded, its ``end_conversation`` Gilbert
    tool is automatically visible during Mentra sessions (the tool
    gates on ``get_current_conversation_ctx() is not None`` which
    the engine sets uniformly). Plugins that want a Mentra-specific
    session-end tool can declare their own ``ToolProvider`` with
    the same ContextVar gate.
    """

    def get_brain_tools(self) -> list[Any]:
        return []

    async def handle_brain_tool(
        self, name: str, args: dict[str, Any], ctx: Any
    ) -> Any:
        # Engine in use_full_ai_service mode never dispatches here.
        # If it ever does (config mismatch / future change), log
        # loud rather than silently returning OK.
        from gilbert.interfaces.conversation import BrainToolResult

        logger.warning(
            "Mentra _NoopBrainToolProvider received unexpected "
            "brain tool dispatch: name=%r — voice_brain may be "
            "running in the wrong mode for this plugin",
            name,
        )
        return BrainToolResult.OK


class MentraService(Service):
    """Mentra smart-glasses orchestration service.

    Capabilities provided: ``mentra``, ``mentra_webhook``,
    ``ws_handlers``.
    Capabilities consumed: ``entity_storage`` (user mappings),
    ``voice_brain`` (the ConversationEngine the plugin hands every
    glasses session off to), ``audio_blob_store`` (short-lived
    public URL for engine-synthesized TTS bytes), and ``ai_chat``
    (transitively — voice_brain itself requires it). ``event_bus``
    and ``configuration`` are optional.
    """

    slash_namespace = "mentra"

    def __init__(self) -> None:
        # ── Config-driven state ───────────────────────────────────
        self._enabled: bool = False
        self._api_key: str = ""
        self._package_name: str = ""
        self._public_base_url: str = ""
        self._system_prompt: str = _DEFAULT_SYSTEM_PROMPT
        self._display_duration_ms: int = 8000

        # ── Resolved dependencies ─────────────────────────────────
        self._resolver: ServiceResolver | None = None
        self._storage: StorageProvider | None = None
        # The voice_brain ConversationEngine runs the actual
        # conversation loop (echo suppression, local VAD, AI dispatch,
        # TTS pacing, barge-in). Mentra is a transport — we feed mic
        # PCM in, play TTS bytes back.
        self._voice_brain: ConversationEngine | None = None
        # Short-lived blob cache so engine-synthesized MP3 bytes get
        # an HTTPS URL Mentra Cloud can fetch server-side.
        self._blob_store: AudioBlobStore | None = None
        self._bus: Any = None

        # ── Live session registry — keyed by Mentra sessionId ─────
        self._sessions: dict[str, MentraSession] = {}
        # Per-session engine task (one ``voice_brain.run_conversation``
        # in flight per glasses session). Tracked so disconnects can
        # cancel cleanly and ``stop()`` can drain on shutdown.
        self._engine_tasks: dict[str, asyncio.Task[Any]] = {}
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
            # ``voice_brain`` runs the conversation loop;
            # ``audio_blob_store`` exposes engine TTS to Mentra Cloud
            # via a public URL. ``entity_storage`` keeps the
            # email→user mapping. ``ai_chat`` is still required
            # transitively by voice_brain itself — declaring it here
            # too makes the dependency tree explicit and lets
            # ``gilbert doctor`` flag missing AI early.
            requires=frozenset(
                {
                    "entity_storage",
                    "voice_brain",
                    "audio_blob_store",
                    "ai_chat",
                }
            ),
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
        brain_svc = resolver.get_capability("voice_brain")
        if isinstance(brain_svc, ConversationEngine):
            self._voice_brain = brain_svc
        blob_svc = resolver.get_capability("audio_blob_store")
        if isinstance(blob_svc, AudioBlobStore):
            self._blob_store = blob_svc
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
            "Mentra service started — package=%s public_base_url=%r "
            "voice_brain=%s audio_blob_store=%s",
            self._package_name,
            self._public_base_url or "<unset>",
            "✓" if self._voice_brain else "✗",
            "✓" if self._blob_store else "✗",
        )
        if not self._public_base_url:
            logger.warning(
                "Mentra public_base_url is unset — engine-synthesized "
                "TTS will be dropped (cloud has no host to fetch the "
                "blob URL from). Set Settings → Mentra → "
                "public_base_url to the Server URL registered with "
                "the Mentra developer console."
            )
        if self._voice_brain is None:
            logger.warning(
                "Mentra: voice_brain capability missing — sessions "
                "will admit but no conversation loop will run."
            )
        if self._blob_store is None:
            logger.warning(
                "Mentra: audio_blob_store capability missing — TTS "
                "audio cannot be served back to Mentra Cloud."
            )

    async def stop(self) -> None:
        # Cancel every in-flight engine task first so the brain
        # stops trying to write to a transport we're about to tear
        # down. Awaiting them isn't necessary — the engine reacts to
        # cancellation by exiting its gather() and we don't care
        # about partial outcomes during shutdown.
        for task in list(self._engine_tasks.values()):
            if not task.done():
                task.cancel()
        self._engine_tasks.clear()
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
                key="public_base_url",
                type=ToolParameterType.STRING,
                description=(
                    "Public HTTPS URL where Gilbert is reachable "
                    '(e.g. "https://gilbert.example.com"). Must match '
                    "the Server URL registered with the Mentra "
                    "developer console. Mentra Cloud fetches "
                    "``<this>/api/audio-blob/<id>`` server-side for "
                    "every TTS clip the voice_brain engine synthesizes, "
                    "so the URL has to be reachable from the public "
                    "internet — localhost / LAN-only values will not "
                    "work."
                ),
                default="",
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
        self._public_base_url = str(section.get("public_base_url") or "")
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
        if self._voice_brain is None:
            return WebhookResponse(
                status="error",
                message="voice_brain capability unavailable",
            )
        if self._blob_store is None:
            return WebhookResponse(
                status="error",
                message="audio_blob_store capability unavailable",
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
            public_base_url=self._public_base_url,
        )
        session = MentraSession(config=config, transport=transport)

        # Hook the session-closed event up to engine task cancellation
        # so a glasses-side disconnect tears down the conversation
        # loop too. Without this the engine sits forever waiting for
        # an audio_in chunk that will never come.
        async def _on_disconnect(code: int, reason: str) -> None:
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
            task = self._engine_tasks.pop(session.session_id, None)
            if task is not None and not task.done():
                task.cancel()

        session.on_disconnected(_on_disconnect)

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
        # Hand the session off to the voice_brain ConversationEngine.
        # The engine drives the welcome-greeting (SPEAK_FIRST opening
        # policy), runs the transcription → AI → TTS loop, handles
        # echo suppression / local VAD / barge-in / tool dispatch
        # without us reimplementing any of it. The engine task runs
        # for the lifetime of the glasses connection and exits when
        # the WS drops (we push ENDED on disconnect) or the LLM
        # calls a hang-up tool.
        engine_task = asyncio.create_task(
            self._run_voice_session(
                session=session, gilbert_user=gilbert_user, caps=caps
            ),
            name=f"mentra-brain:{req.session_id}",
        )
        self._engine_tasks[req.session_id] = engine_task

        # Pop the task off the registry when it finishes so we don't
        # leak references after a clean disconnect.
        def _on_engine_done(
            _t: asyncio.Task[Any], sid: str = req.session_id
        ) -> None:
            self._engine_tasks.pop(sid, None)

        engine_task.add_done_callback(_on_engine_done)

        return WebhookResponse(status="success")

    async def _handle_stop_request(
        self, payload: dict[str, object]
    ) -> WebhookResponse:
        req = _parse_stop_request(payload)
        session = self._sessions.pop(req.session_id, None)
        self._connected_at.pop(req.session_id, None)
        # Cancel the in-flight engine task first so it stops trying
        # to write to a transport we're about to close. The session
        # disconnect callback also tries to cancel it; both paths
        # are idempotent.
        task = self._engine_tasks.pop(req.session_id, None)
        if task is not None and not task.done():
            task.cancel()
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

    # ── voice_brain handoff ──────────────────────────────────────

    async def _run_voice_session(
        self,
        *,
        session: MentraSession,
        gilbert_user: UserContext,
        caps: Any,
    ) -> None:
        """Drive one glasses session through the voice_brain engine.

        Builds the ``ConversationSession`` adapter, wires MicManager
        → engine audio_in, builds the ``AudioSink`` that ships
        engine TTS back through SpeakerManager + the blob route,
        configures the engine for Mentra's audio formats, then awaits
        ``voice_brain.run_conversation()``.

        Engine returns when the conversation ends — terminal status
        event (disconnect / stop request), end-of-conversation tool
        call (``end_conversation``), or the watchdog cap. Cleanup
        runs in the wrapping ``finally``."""
        if self._voice_brain is None or self._blob_store is None:
            return  # already logged at start()

        # Pre-set the user context so every ai.chat() inside the
        # engine sees the right identity. Lives in this task's
        # ContextVar — no bleed to sibling sessions.
        set_current_user(gilbert_user)

        # Echo-suppression has two cooperating layers:
        #
        # 1. **Per-speaker "first seen" classifier.** Mentra Cloud
        #    diarizes its transcription stream — each detected
        #    speaker gets a stable per-session ``speakerId``. Logs
        #    show the user comes back consistently as ``"1"`` while
        #    Gilbert's echoes get ``"0"`` / ``"2"`` etc. The IDs are
        #    sequential, not semantic, so we LEARN per session by
        #    classifying each NEW speaker_id the first time we see
        #    it:
        #
        #      - First sighting INSIDE a mute window → classify as
        #        Gilbert (echo). The welcome speech fires immediately
        #        on session admit, BEFORE the user has a chance to
        #        speak, so Gilbert is reliably the first new speaker
        #        we see during a mute.
        #      - First sighting OUTSIDE any mute → classify as user
        #        (anything making sound the cloud transcribes while
        #        Gilbert isn't playing is human speech).
        #
        #    The classification STICKS for the session — once a
        #    speaker_id is labelled "user", later transcripts from
        #    them flow through even DURING a Gilbert mute, which
        #    restores barge-in. Once labelled "gilbert", later
        #    transcripts get dropped regardless of mute state, which
        #    catches late-arriving echoes after the time-window
        #    estimate runs out.
        #
        # 2. **Time-window mute (fallback for unclassified ids).**
        #    Armed from inside ``_MentraAudioSink.flush()`` — every
        #    TTS clip (real reply AND engine filler) goes through
        #    there, so the mute fires uniformly. Covers the rare
        #    case where the cloud assigns a brand-new speaker_id
        #    during a Gilbert playback (e.g. it gives Gilbert a
        #    different id mid-session); the mute drops it on first
        #    sight and the classifier records it as Gilbert for
        #    next time.
        mute_until_monotonic: list[float] = [0.0]
        speaker_class: dict[str, str] = {}  # speaker_id -> "user" | "gilbert"

        def _arm_mute(seconds: float) -> None:
            mute_until_monotonic[0] = time.monotonic() + seconds
            logger.info(
                "Mentra: muting transcription for %.1fs while "
                "Gilbert speaks (session=%s)",
                seconds,
                session.session_id,
            )

        # Build the adapter session — the engine will read inbound
        # mic chunks from its audio_in queue and write TTS bytes
        # via the sink.
        sink = _MentraAudioSink(
            blob_store=self._blob_store,
            speaker=session.speaker,
            public_base_url=self._public_base_url,
            mime="audio/mpeg",
            session_id_for_log=session.session_id,
            on_playback_armed=_arm_mute,
        )
        conv_session = _MentraConversationSession(
            session_id=session.session_id,
            audio_in=None,  # type: ignore[arg-type]  — set below
            audio_out=sink,
            events=None,  # type: ignore[arg-type]
        )
        # ``audio_in`` is never iterated when ``disable_internal_stt``
        # is True (engine listen loop early-returns) — wire a fresh
        # generator anyway so the dataclass field isn't None for any
        # observer that introspects it.
        conv_session.audio_in = conv_session._audio_in_iter()  # type: ignore[assignment]
        conv_session.events = conv_session._events_iter()  # type: ignore[assignment]

        # Cloud transcription → engine inject queue.
        #
        # Why not feed raw PCM to the engine's STT pump? Mentra Cloud
        # does NOT actually stream binary audio_chunk frames to apps
        # on Mentra Live + iOS (observed in production: the
        # subscription is sent but no binary frames arrive — the
        # engine's pump starves, Scribe idle-times out after 15s,
        # the listen loop reopens, then the audio_in generator is
        # closed-on-reopen and Gilbert stops responding entirely).
        # The cloud DOES ship cloud-side transcription results via
        # the JSON ``transcription`` stream — that's the path
        # everything actually works on. Subscribe to it and feed
        # the engine via its synthetic-turn queue, which behaves
        # identically to an STT-driven turn from the engine's
        # perspective.
        inject_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=50)

        async def _on_transcription(data: Any) -> None:
            # Only commit on isFinal — partials are noise, the engine
            # would re-think on every keystroke.
            if not getattr(data, "is_final", False):
                return
            text = (getattr(data, "text", "") or "").strip()
            if not text:
                return
            speaker_id = str(getattr(data, "speaker_id", "") or "")
            confidence = float(getattr(data, "confidence", 0.0) or 0.0)
            in_mute = time.monotonic() < mute_until_monotonic[0]

            # First-seen classification. New speaker_id gets a
            # sticky label based on whether their debut was during a
            # mute window. Existing ids keep their prior label —
            # critical for barge-in (user labelled "user" stays
            # "user" even when they speak during a later mute).
            known_class = speaker_class.get(speaker_id, "")
            if speaker_id and not known_class:
                known_class = "gilbert" if in_mute else "user"
                speaker_class[speaker_id] = known_class
                logger.info(
                    "Mentra: new speaker_id=%r classified as %r "
                    "(first seen %s mute, session=%s)",
                    speaker_id,
                    known_class,
                    "inside" if in_mute else "outside",
                    session.session_id,
                )

            # Decision tree:
            #   user-labelled speaker → dispatch (barge-in works)
            #   gilbert-labelled speaker → drop (echo)
            #   unknown speaker + in mute → drop (almost certainly
            #                                     Gilbert; the rare
            #                                     edge case where it's
            #                                     a genuine user
            #                                     utterance arriving
            #                                     inside the first
            #                                     mute is recoverable
            #                                     by re-launching the
            #                                     session)
            #   unknown speaker + outside mute → dispatch (trust)
            drop_reason: str | None = None
            if known_class == "user":
                drop_reason = None
            elif known_class == "gilbert":
                drop_reason = "known Gilbert speaker_id"
            elif in_mute:
                drop_reason = "mute window (unclassified speaker)"

            gilbert_ids = sorted(
                sid for sid, cls in speaker_class.items() if cls == "gilbert"
            )
            user_ids = sorted(
                sid for sid, cls in speaker_class.items() if cls == "user"
            )
            logger.info(
                "Mentra transcription %s — session=%s speaker_id=%r "
                "class=%r confidence=%.2f gilbert_ids=%s user_ids=%s "
                "len=%d text=%r",
                "DROPPED" if drop_reason else "final",
                session.session_id,
                speaker_id,
                known_class or "<new>",
                confidence,
                gilbert_ids,
                user_ids,
                len(text),
                text[:120],
            )

            if drop_reason is not None:
                self._record_event(
                    session.user_id,
                    "transcription_suppressed",
                    (
                        f'(suppressed: {drop_reason}): "{text[:140]}" '
                        f"[speaker={speaker_id or '<none>'} "
                        f"conf={confidence:.2f}]"
                    ),
                )
                return

            # Surface every committed user transcript in the debug
            # webview. With ``disable_internal_stt=True`` the engine
            # never emits ``on_transcript_turn("them", ...)`` itself
            # (its listen loop early-returns), so this is the only
            # place a "what the user said" event gets logged.
            self._record_event(
                session.user_id,
                "transcription_final",
                (
                    f'You said: "{text[:160]}" '
                    f"[speaker={speaker_id or '<none>'} "
                    f"conf={confidence:.2f}]"
                ),
            )
            try:
                inject_queue.put_nowait(text)
            except asyncio.QueueFull:
                logger.warning(
                    "Mentra inject queue full — dropping transcript "
                    "(session=%s text=%r)",
                    session.session_id,
                    text[:60],
                )

        transcription_cleanup = session.transcription.on_transcription(
            _on_transcription
        )

        # Engine callbacks — Mentra-specific behaviour bolted onto
        # the otherwise modality-agnostic engine.
        async def _on_transcript_turn(
            who: str, text: str, ts_seconds: float
        ) -> None:
            # Feed the per-user debug ring buffer so the webview
            # shows what the user said + what Gilbert said.
            label = "You said" if who == "them" else "Gilbert said"
            kind = (
                "transcription_final" if who == "them" else "ai_reply"
            )
            self._record_event(
                session.user_id,
                kind,
                f'{label}: "{text[:200]}"',
            )

        async def _on_llm_turn(text: str, tool_names: list[str]) -> None:
            # Surface the reply on the heads-up display (if device
            # has one). Truncated so it fits the small screen.
            #
            # The echo-suppression mute is armed by the audio sink
            # on flush (not here) so engine filler clips ("hmm, let
            # me check") — which bypass on_llm_turn — also trigger
            # the mute.
            if not text:
                return
            if caps is not None and not caps.has_display:
                return
            snippet = _summarize_for_display(text)
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
                    "Mentra display.show_text_wall raised "
                    "(session=%s)",
                    session.session_id,
                    exc_info=True,
                )

        async def _on_status_change(
            status: ConversationStatus, reason: str
        ) -> None:
            logger.info(
                "Mentra engine status: session=%s status=%s reason=%r",
                session.session_id,
                status.value,
                reason,
            )

        # Synthetic user-role priming so the SPEAK_FIRST opener has
        # something to respond to. The Anthropic Messages API
        # rejects ``messages=[]`` — without this the engine's first
        # ai.chat() call would crash on a brand-new session. Same
        # priming shape as voice-agent.
        priming = [
            Message(
                role=MessageRole.USER,
                content=(
                    "(SYSTEM) The user just put on their smart glasses "
                    "and activated Gilbert. Greet them briefly — one "
                    "short sentence — and let them know you're "
                    "listening."
                ),
            )
        ]

        config = ConversationConfig(
            system_prompt=self._system_prompt,
            # No brain-tool provider needed — we run in
            # use_full_ai_service mode where end-of-conversation
            # comes through the regular Gilbert tool ecosystem
            # (voice-agent's ``end_conversation`` tool is visible
            # via ContextVar-gated discovery; it ends both modes).
            brain_tool_provider=_NoopBrainToolProvider(),
            opening_policy=OpeningPolicy(
                behavior=OpeningBehavior.SPEAK_FIRST,
                fallback_timeout_seconds=1.0,
            ),
            max_conversation_seconds=900,
            priming_messages=priming,
            on_status_change=_on_status_change,
            on_transcript_turn=_on_transcript_turn,
            on_llm_turn=_on_llm_turn,
            # Filler ("hmm, let me check…") plays automatically when
            # the LLM is still thinking past the threshold. Without
            # it the user sits in silence on every tool-using turn
            # (knowledge.search, MCP calls, agent dispatch) wondering
            # if Gilbert heard them. The engine also injects the
            # filler through ``audio_out`` like a regular utterance —
            # so the audio sink's mute-arming covers it for free, no
            # extra plumbing needed.
            filler_threshold_seconds=_FILLER_THRESHOLD_SECONDS,
            filler_phrases=list(_DEFAULT_FILLER_PHRASES),
            default_goodbye_phrases=list(_DEFAULT_GOODBYE_PHRASES),
            # MP3 + no realtime pacing: Mentra Cloud fetches the
            # whole clip in one shot and plays it; per-chunk pacing
            # would stretch a 5s clip into 10s of buffering for no
            # benefit.
            tts_output_format=TTSAudioFormat.MP3,
            tts_output_mime="audio/mpeg",
            tts_realtime_pacing=False,
            use_full_ai_service=True,
            source="mentra",
            # Skip the engine's internal STT — Mentra Cloud handles
            # transcription server-side and ships finalised text
            # via the ``transcription`` JSON stream. We feed every
            # final turn into ``inject_synthetic_user_turn_queue``
            # above; the engine's synthetic-turn loop processes
            # each one identically to an STT-driven turn.
            disable_internal_stt=True,
            inject_synthetic_user_turn_queue=inject_queue,
        )

        # Kick the engine off with an ACTIVE event so its opening
        # policy fires immediately. The status loop schedules
        # ``_open_proactively`` on ACTIVE, which is what generates
        # the "Welcome to Gilbert"-style greeting.
        await conv_session.push_event(
            ConversationStatusEvent(status=ConversationStatus.ACTIVE)
        )

        try:
            await self._voice_brain.run_conversation(conv_session, config)
        except asyncio.CancelledError:
            logger.info(
                "Mentra engine task cancelled for session=%s",
                session.session_id,
            )
            raise
        except Exception:
            logger.exception(
                "Mentra voice_brain.run_conversation crashed for "
                "session=%s",
                session.session_id,
            )
        finally:
            # Unsubscribe from the transcription stream so we stop
            # paying bandwidth for a session no engine is consuming.
            try:
                transcription_cleanup()
            except Exception:
                logger.debug(
                    "transcription cleanup raised (session=%s)",
                    session.session_id,
                    exc_info=True,
                )
            conv_session.closed = True

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


