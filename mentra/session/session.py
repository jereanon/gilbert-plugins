"""``MentraSession`` — one live WebSocket connection back to Mentra
Cloud for one glasses-app pairing.

Lifecycle:

1. Constructed with a ``Transport`` (real or fake).
2. ``connect()`` opens the transport + sends ``CONNECTION_INIT``.
3. Cloud responds with ``CONNECTION_ACK`` → settings + capabilities
   populated, ``connected`` event fired, queued subscriptions
   flushed.
4. Inbound frames route through a two-level dispatch:
   - top-level ``type`` → ``MessageHandlerRegistry``
   - ``DATA_STREAM`` envelope → ``DataStreamRouter`` by
     ``streamType`` (with prefix-match support for language-tagged
     subscriptions like ``transcription:en-US``)
5. Outbound frames go through ``send_frame()`` which JSON-encodes
   and ships over the transport.
6. ``disconnect()`` closes cleanly, fires ``disconnected``.

Reconnect / parked-session handling is deferred for v1 — the cloud
re-fires the webhook on subsequent user opens so we always get a
fresh ``CONNECTION_INIT`` path, which is good enough until we hit
production durability needs.

This module is pure: no Gilbert service knowledge, no AI dispatch.
The plugin's ``mentra_service.py`` consumes ``MentraSession`` and
wires it to ``AIService.chat``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from gilbert.interfaces.mentra import GlassesCapabilities

from ..protocol.frames import (
    build_connection_init,
    build_subscription_update,
    encode_frame,
    parse_frame,
)
from ..protocol.message_types import CloudToAppMessageType
from .managers import (
    ButtonManager,
    CameraManager,
    DashboardManager,
    DisplayManager,
    LedManager,
    LocationManager,
    MicManager,
    SpeakerManager,
    TranscriptionManager,
)
from .managers.base import ManagerDeps
from .transport import Transport, TransportState

logger = logging.getLogger(__name__)


__all__ = [
    "MentraSession",
    "MentraSessionConfig",
]


# Handler callable signatures used by the internal dispatch tables.
MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]
StreamHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class MentraSessionConfig:
    """Per-session configuration. ``session_id`` and ``user_id`` come
    from the webhook payload; ``api_key`` and ``package_name`` are
    plugin-level constants the service reads from ``/settings``."""

    package_name: str
    api_key: str
    session_id: str
    user_id: str = ""
    # Optional Gilbert-side user mapping. The plugin's session
    # manager fills this in after looking up ``user_id`` (the
    # Mentra-side email) in the mapping table. Sessions never refer
    # to it directly — it's surfaced here so handlers in the service
    # layer can grab it without re-querying storage.
    gilbert_user_id: str = ""


class _MessageHandlerRegistry:
    """Top-level message-type dispatch.

    Mirrors the upstream SDK's ``MessageHandlerRegistry`` — one or
    more handlers per ``type``, fired in registration order. The
    multi-handler shape matters for the connection-ack path where
    both the session itself and any user-registered ``on_connected``
    listener want to fire.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[MessageHandler]] = {}

    def register(self, msg_type: str, handler: MessageHandler) -> Callable[[], None]:
        self._handlers.setdefault(msg_type, []).append(handler)

        def _unsub() -> None:
            lst = self._handlers.get(msg_type)
            if lst is None:
                return
            try:
                lst.remove(handler)
            except ValueError:
                pass
            if not lst:
                self._handlers.pop(msg_type, None)

        return _unsub

    async def dispatch(self, message: dict[str, Any]) -> bool:
        msg_type = str(message.get("type") or "")
        handlers = self._handlers.get(msg_type, [])
        if not handlers:
            return False
        for handler in list(handlers):
            try:
                await handler(message)
            except Exception:
                logger.exception(
                    "Mentra message handler raised for type=%r", msg_type
                )
        return True


class _DataStreamRouter:
    """Second-level dispatch for ``DATA_STREAM`` envelopes.

    Routes by ``streamType``. Supports exact matches and
    prefix-with-colon matches so that registering ``transcription``
    catches ``transcription:en-US``, ``transcription:auto``, etc.
    Mirrors the upstream router's matching algorithm.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[StreamHandler]] = {}
        self._prefix_cache: list[str] | None = None

    def on(self, key: str, handler: StreamHandler) -> Callable[[], None]:
        self._handlers.setdefault(key, []).append(handler)
        self._prefix_cache = None

        def _unsub() -> None:
            lst = self._handlers.get(key)
            if lst is None:
                return
            try:
                lst.remove(handler)
            except ValueError:
                pass
            if not lst:
                self._handlers.pop(key, None)
                self._prefix_cache = None

        return _unsub

    async def handle(self, message: dict[str, Any]) -> bool:
        stream_type = str(message.get("streamType") or "")
        if not stream_type:
            return False
        data = message.get("data")
        if not isinstance(data, dict):
            data = {}

        matched = False
        # Exact match
        for handler in list(self._handlers.get(stream_type, [])):
            try:
                await handler(stream_type, data)
                matched = True
            except Exception:
                logger.exception(
                    "Mentra stream handler raised for streamType=%r",
                    stream_type,
                )
        # Prefix match (skip if exact already covered the key)
        for key in self._prefix_keys():
            if key == stream_type:
                continue
            if stream_type.startswith(key):
                nxt = stream_type[len(key) : len(key) + 1]
                if nxt in ("", ":"):
                    for handler in list(self._handlers.get(key, [])):
                        try:
                            await handler(stream_type, data)
                            matched = True
                        except Exception:
                            logger.exception(
                                "Mentra prefix handler raised for key=%r",
                                key,
                            )
        return matched

    def _prefix_keys(self) -> list[str]:
        if self._prefix_cache is None:
            self._prefix_cache = sorted(
                self._handlers.keys(), key=len, reverse=True
            )
        return self._prefix_cache


@dataclass
class _SubscriptionState:
    """Tracks active stream subscriptions. The session re-syncs this
    set with the cloud after every (re)connect."""

    subscriptions: set[str] = field(default_factory=set)
    dirty: bool = False


class MentraSession:
    """One Mentra Cloud session.

    Owns one ``Transport`` and the per-feature managers (transcription,
    button, display, dashboard, speaker, mic, location, camera, LED).
    The plugin's service layer constructs one of these per inbound
    ``session_request`` webhook and tears it down on the matching
    ``stop_request`` (or permanent disconnect).
    """

    def __init__(
        self, *, config: MentraSessionConfig, transport: Transport
    ) -> None:
        self._config = config
        self._transport = transport
        self._messages = _MessageHandlerRegistry()
        self._streams = _DataStreamRouter()
        self._subs = _SubscriptionState()
        self._connected = asyncio.Event()
        self._stopped = asyncio.Event()
        self._capabilities: GlassesCapabilities | None = None
        self._settings: list[dict[str, Any]] = []
        self._mentraos_settings: dict[str, Any] = {}
        self._connected_handlers: list[Callable[[], Awaitable[None]]] = []
        self._disconnected_handlers: list[
            Callable[[int, str], Awaitable[None]]
        ] = []
        self._stopped_handlers: list[Callable[[str], Awaitable[None]]] = []
        # Reconnect state — currently used only to mark a session
        # PERMANENTLY closed (e.g. RECONNECT_REJECTED with NOT_RUNNING
        # / BOOT_TIMEOUT). Cloud re-fires the webhook on the next
        # user-initiated open, so we don't need full auto-reconnect.
        self._permanent: bool = False

        # Wire transport callbacks into our dispatch.
        self._transport.on_text(self._on_text)
        self._transport.on_binary(self._on_binary)
        self._transport.on_close(self._on_close)
        self._transport.on_error(self._on_error)

        # Build manager deps + instantiate managers.
        deps = ManagerDeps(
            package_name=config.package_name,
            get_session_id=lambda: self._config.session_id,
            send_frame=self.send_frame,
            add_subscription=self._add_subscription,
            remove_subscription=self._remove_subscription,
            register_message_handler=self._messages.register,
            register_stream_handler=self._streams.on,
        )
        self.transcription = TranscriptionManager(deps)
        self.button = ButtonManager(deps)
        self.display = DisplayManager(deps)
        self.dashboard = DashboardManager(deps)
        self.speaker = SpeakerManager(deps)
        self.mic = MicManager(deps)
        self.location = LocationManager(deps)
        self.camera = CameraManager(deps)
        self.led = LedManager(deps)

        # Register the core handlers that drive lifecycle state.
        self._register_core_handlers()

    # ── Public surface ─────────────────────────────────────────────

    @property
    def session_id(self) -> str:
        return self._config.session_id

    @property
    def user_id(self) -> str:
        """Mentra-side user identifier (email)."""
        return self._config.user_id

    @property
    def gilbert_user_id(self) -> str:
        """Gilbert-side user id this session is acting on behalf of.
        Populated by the service layer at session creation."""
        return self._config.gilbert_user_id

    @property
    def package_name(self) -> str:
        return self._config.package_name

    @property
    def capabilities(self) -> GlassesCapabilities | None:
        """Hardware advertisement from the cloud — ``None`` until
        ``CONNECTION_ACK`` arrives."""
        return self._capabilities

    @property
    def is_connected(self) -> bool:
        return (
            self._transport.ready_state is TransportState.OPEN
            and self._connected.is_set()
        )

    async def connect(self) -> None:
        """Open the transport and send the connection-init frame.
        Resolves when the cloud's ``CONNECTION_ACK`` has arrived (or
        ``CONNECTION_ERROR`` raises)."""
        await self._transport.connect()
        await self.send_frame(
            build_connection_init(
                package_name=self._config.package_name,
                api_key=self._config.api_key,
            )
        )
        # Wait for the ack to populate settings/capabilities before
        # returning control to the caller.
        await self._connected.wait()

    async def disconnect(self) -> None:
        """Close the transport. Idempotent."""
        await self._transport.close()

    async def send_frame(self, frame: dict[str, Any]) -> None:
        """Encode + send one frame. Centralized so all wire-layer
        logging / metrics flow through one place."""
        await self._transport.send(encode_frame(frame))

    # Event-subscription helpers — async callbacks that fire on the
    # corresponding lifecycle transition.

    def on_connected(
        self, handler: Callable[[], Awaitable[None]]
    ) -> Callable[[], None]:
        self._connected_handlers.append(handler)
        return lambda: _safe_remove(self._connected_handlers, handler)

    def on_disconnected(
        self, handler: Callable[[int, str], Awaitable[None]]
    ) -> Callable[[], None]:
        self._disconnected_handlers.append(handler)
        return lambda: _safe_remove(self._disconnected_handlers, handler)

    def on_stopped(
        self, handler: Callable[[str], Awaitable[None]]
    ) -> Callable[[], None]:
        self._stopped_handlers.append(handler)
        return lambda: _safe_remove(self._stopped_handlers, handler)

    # ── Internal lifecycle ─────────────────────────────────────────

    def _register_core_handlers(self) -> None:
        self._messages.register(
            CloudToAppMessageType.DATA_STREAM.value, self._on_data_stream
        )
        self._messages.register(
            CloudToAppMessageType.CONNECTION_ACK.value, self._on_connection_ack
        )
        self._messages.register(
            CloudToAppMessageType.RECONNECT_ACK.value, self._on_connection_ack
        )
        self._messages.register(
            CloudToAppMessageType.SETTINGS_UPDATE.value,
            self._on_settings_update,
        )
        self._messages.register(
            CloudToAppMessageType.CAPABILITIES_UPDATE.value,
            self._on_capabilities_update,
        )
        self._messages.register(
            CloudToAppMessageType.APP_STOPPED.value, self._on_app_stopped
        )
        self._messages.register(
            CloudToAppMessageType.CONNECTION_ERROR.value,
            self._on_connection_error,
        )
        # Reconnect outcomes — cloud emits these in response to our
        # ``reconnect`` frame after a transport drop. We don't fire
        # reconnects automatically (the cloud re-fires the webhook
        # for any user-initiated open), so these are mostly defensive
        # — if a future ``reconnect()`` path goes in, the handlers
        # already wire the right cleanup.
        self._messages.register(
            CloudToAppMessageType.RECONNECT_REJECTED.value,
            self._on_reconnect_rejected,
        )
        self._messages.register(
            CloudToAppMessageType.RECONNECT_DEFERRED.value,
            self._on_reconnect_deferred,
        )

    async def _on_text(self, raw: str) -> None:
        frame = parse_frame(raw)
        if not frame:
            return
        # Temporary diagnostic tracing for production debug — log
        # every inbound frame's top-level type so we can spot
        # silently-dropped audio_play_response / permission_error /
        # other cloud events we haven't wired up handlers for. Drop
        # to DEBUG once we have a stable picture of which event
        # shapes the cloud actually emits.
        frame_type = str(frame.get("type") or "<no-type>")
        if frame_type == "data_stream":
            stream_type = str(frame.get("streamType") or "<no-streamType>")
            logger.info(
                "Mentra inbound — type=data_stream streamType=%s",
                stream_type,
            )
        else:
            logger.info("Mentra inbound — type=%s", frame_type)
        handled = await self._messages.dispatch(frame)
        if not handled:
            logger.warning(
                "Mentra inbound UNHANDLED — type=%s body=%s",
                frame_type,
                {k: v for k, v in frame.items() if k != "type"},
            )

    async def _on_binary(self, data: bytes) -> None:
        """Binary frames carry mic audio (16 kHz mono 16-bit PCM).
        Mentra's protocol doesn't currently multiplex multiple
        binary streams on one connection, so we forward every
        binary frame to ``MicManager`` and let it decide whether to
        dispatch (it skips when no ``on_audio_chunk`` subscriber is
        registered)."""
        await self.mic.handle_binary_audio(data)

    async def _on_close(self, code: int, reason: str) -> None:
        # Mark connected event so a pending connect() raises rather
        # than hanging forever on a failed handshake.
        self._connected.set()
        for handler in list(self._disconnected_handlers):
            try:
                await handler(code, reason)
            except Exception:
                logger.exception("Mentra disconnected handler raised")

    async def _on_error(self, exc: BaseException) -> None:
        logger.error("Mentra transport error: %s", exc)

    async def _on_reconnect_rejected(self, message: dict[str, Any]) -> None:
        """Cloud refused our reconnect attempt. ``NOT_RUNNING`` /
        ``BOOT_TIMEOUT`` mean the original session is unrecoverable
        — close the transport and let the service layer drop us
        from the registry. Other codes are transient and the cloud
        will let us retry; we don't reattempt automatically because
        Mentra Cloud re-fires the webhook on the user's next open."""
        code = str(message.get("code") or "")
        msg = str(message.get("message") or f"reconnect rejected: {code}")
        permanent = code in ("NOT_RUNNING", "BOOT_TIMEOUT")
        logger.info(
            "Mentra reconnect rejected: code=%r permanent=%s msg=%s",
            code,
            permanent,
            msg,
        )
        if permanent:
            self._permanent = True
            self._connected.set()
            await self._transport.close(code=1000, reason=msg[:120])

    async def _on_reconnect_deferred(self, message: dict[str, Any]) -> None:
        """Cloud is asking us to wait before retrying — it'll be
        ready for us again within ``timeoutMs``. v1 just logs +
        keeps the transport open; if the user takes another action
        the cloud will eventually re-fire the webhook."""
        try:
            timeout_ms = int(message.get("timeoutMs") or 30000)
        except (TypeError, ValueError):
            timeout_ms = 30000
        logger.info(
            "Mentra reconnect deferred for %dms", timeout_ms
        )

    @property
    def is_permanently_closed(self) -> bool:
        """``True`` after the cloud signalled a permanent rejection
        (e.g. ``RECONNECT_REJECTED`` with ``NOT_RUNNING``). The
        service layer uses this to decide whether to keep the
        session in its registry or drop it."""
        return self._permanent

    async def _on_data_stream(self, message: dict[str, Any]) -> None:
        await self._streams.handle(message)

    async def _on_connection_ack(self, message: dict[str, Any]) -> None:
        # Apply settings + capabilities + accept any rotated session id.
        settings = message.get("settings")
        if isinstance(settings, list):
            self._settings = [s for s in settings if isinstance(s, dict)]
        mentraos = message.get("mentraosSettings")
        if isinstance(mentraos, dict):
            self._mentraos_settings = mentraos
        caps_raw = message.get("capabilities")
        if isinstance(caps_raw, dict):
            self._capabilities = _parse_capabilities(caps_raw)
        rotated = message.get("sessionId")
        if isinstance(rotated, str) and rotated:
            self._config.session_id = rotated
        # Mark connected + sync subscriptions before firing user
        # callbacks so handlers see the populated session state.
        self._connected.set()
        await self._sync_subscriptions()
        for handler in list(self._connected_handlers):
            try:
                await handler()
            except Exception:
                logger.exception("Mentra connected handler raised")

    async def _on_settings_update(self, message: dict[str, Any]) -> None:
        settings = message.get("settings")
        if isinstance(settings, list):
            self._settings = [s for s in settings if isinstance(s, dict)]

    async def _on_capabilities_update(self, message: dict[str, Any]) -> None:
        caps_raw = message.get("capabilities")
        if isinstance(caps_raw, dict):
            self._capabilities = _parse_capabilities(caps_raw)

    async def _on_app_stopped(self, message: dict[str, Any]) -> None:
        reason = str(message.get("reason") or "unknown")
        self._stopped.set()
        for handler in list(self._stopped_handlers):
            try:
                await handler(reason)
            except Exception:
                logger.exception("Mentra stopped handler raised")
        # Close the transport so we stop reading from a doomed socket.
        await self._transport.close(code=1000, reason="app_stopped")

    async def _on_connection_error(self, message: dict[str, Any]) -> None:
        msg = str(message.get("message") or "connection error")
        logger.warning("Mentra cloud rejected connection: %s", msg)
        self._connected.set()  # unblock any pending connect()
        await self._transport.close(code=1008, reason=msg[:120])

    # ── Subscriptions ──────────────────────────────────────────────

    def _add_subscription(self, stream_type: str) -> None:
        if stream_type in self._subs.subscriptions:
            return
        self._subs.subscriptions.add(stream_type)
        self._subs.dirty = True
        if self.is_connected:
            # Fire-and-forget — order is irrelevant.
            asyncio.create_task(self._sync_subscriptions())

    def _remove_subscription(self, stream_type: str) -> None:
        if stream_type not in self._subs.subscriptions:
            return
        self._subs.subscriptions.discard(stream_type)
        self._subs.dirty = True
        if self.is_connected:
            asyncio.create_task(self._sync_subscriptions())

    async def _sync_subscriptions(self) -> None:
        if not self._subs.dirty:
            return
        frame = build_subscription_update(
            package_name=self._config.package_name,
            session_id=self._config.session_id,
            subscriptions=sorted(self._subs.subscriptions),
        )
        await self.send_frame(frame)
        self._subs.dirty = False


def _parse_capabilities(raw: dict[str, Any]) -> GlassesCapabilities:
    """Translate the cloud's capabilities JSON into a typed
    dataclass. Defaults preserve safe behavior — unknown fields
    just don't unlock the matching features."""
    return GlassesCapabilities(
        model_name=str(raw.get("modelName") or ""),
        has_camera=bool(raw.get("hasCamera", False)),
        has_display=bool(raw.get("hasDisplay", False)),
        has_microphone=bool(raw.get("hasMicrophone", False)),
        has_speaker=bool(raw.get("hasSpeaker", False)),
        has_imu=bool(raw.get("hasIMU", False)),
        has_button=bool(raw.get("hasButton", False)),
        has_light=bool(raw.get("hasLight", False)),
        has_wifi=bool(raw.get("hasWifi", False)),
        raw=dict(raw),
    )


def _safe_remove(lst: list[Any], item: Any) -> None:
    try:
        lst.remove(item)
    except ValueError:
        pass
