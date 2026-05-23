"""Telnyx telephony backend.

Telnyx's Call Control v2 API places outbound calls; Media Streaming
attaches a bidirectional WebSocket where audio flows in mulaw 8 kHz
chunks (base64-encoded inside JSON frames). The backend bridges this
into Gilbert's vendor-free ``TelephonyBackend`` ABC:

- ``place_call(to, from)`` POSTs to ``POST /v2/calls`` to dial out,
  then POSTs to ``POST /v2/calls/{id}/actions/streaming_start`` with
  our public WebSocket URL.
- Telnyx connects to that WebSocket, starts forwarding the remote
  audio, and accepts our outbound audio back over the same socket.
- Webhooks (``POST /webhooks/telnyx`` on Gilbert's public tunnel) deliver
  status events — ``call.initiated``, ``call.answered``, ``call.hangup``.
  Those land in ``register_webhook_event`` which feeds the matching
  session's event queue.

The ``CallSession`` returned by ``place_call`` is keyed off Gilbert's
internal ``call_id``; Telnyx's own ``call_control_id`` is held in a
side table so webhook lookups can find the right session.

This module is self-contained — no imports from core services. The
plugin's ``setup()`` triggers a registration import; the rest is
exercised via the abstract ``TelephonyBackend`` API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urljoin

import httpx

from gilbert.interfaces.configuration import ConfigParam
from gilbert.interfaces.telephony import (
    CallErrorEvent,
    CallEvent,
    CallSession,
    CallStatus,
    CallStatusEvent,
    DtmfEvent,
    TelephonyBackend,
)
from gilbert.interfaces.tools import ToolParameterType

logger = logging.getLogger(__name__)

_TELNYX_API = "https://api.telnyx.com/v2/"


# ── Public registry — webhooks + media WebSockets find their session ───
#
# The HTTP / WebSocket route handlers in ``web/routes/telnyx_*`` import
# this module and call ``deliver_webhook_event`` / ``register_media_ws``
# to push state into the right ``_TelnyxCallSession``. Keeping the
# registry on the module rather than the backend lets the routes find
# the right session without holding a service-resolver reference at
# import time.
_active_sessions: dict[str, _TelnyxCallSession] = {}
_call_control_to_gilbert: dict[str, str] = {}


def find_session_by_call_control_id(cc_id: str) -> _TelnyxCallSession | None:
    """Webhook lookup helper — Telnyx events identify the call by its
    ``call_control_id``; we keep a sidecar map back to Gilbert's id."""
    gilbert_id = _call_control_to_gilbert.get(cc_id)
    if not gilbert_id:
        return None
    return _active_sessions.get(gilbert_id)


def find_session_by_gilbert_id(call_id: str) -> _TelnyxCallSession | None:
    return _active_sessions.get(call_id)


def find_session_by_token(token: str) -> _TelnyxCallSession | None:
    """Media-stream WebSocket lookup. Telnyx echoes our ``custom_parameters``
    (which we stamp with the webhook token) in the ``start`` frame, so the
    socket can authenticate against the right session at connect time."""
    if not token:
        return None
    for sess in _active_sessions.values():
        if sess.webhook_token == token:
            return sess
    return None


# ── The session, threaded between three concurrent producers ──────────


@dataclass
class _TelnyxCallSession:
    """Concrete ``CallSession`` for a Telnyx call.

    Three queues are populated by three different sources:

    - ``_audio_in_queue``  — mulaw bytes pushed by the Media WS route
                              when Telnyx forwards remote audio
    - ``_events_queue``    — CallEvent rows pushed by the webhook route
                              when Telnyx delivers status updates
    - the outbound side    — ``_TelnyxAudioSink.write`` ships bytes back
                              over the same Media WS socket

    Closed by ``hang_up``, which (a) tells Telnyx to terminate, (b)
    pushes a HUNG_UP status event so the brain's listeners shut down
    cleanly, (c) closes the WS socket so the media pump unwinds.
    """

    call_id: str  # Gilbert-issued id
    call_control_id: str  # Telnyx-issued id (set after place_call succeeds)
    webhook_token: str
    backend: TelnyxTelephony

    # Internal queues — Telnyx fans events into these; the brain reads.
    _audio_in_queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=500)
    )
    _events_queue: asyncio.Queue[CallEvent] = field(
        default_factory=lambda: asyncio.Queue(maxsize=200)
    )
    # The active media-stream WebSocket. Set by the route handler when
    # Telnyx connects to our endpoint; cleared on disconnect.
    media_ws: Any = None
    # Stream id Telnyx assigns inside the WS start frame — needed to
    # tag outbound media frames so they get routed to the right call.
    stream_id: str = ""
    closed: bool = False

    async def push_audio_in(self, chunk: bytes) -> None:
        try:
            self._audio_in_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            # Queue overflow means the brain isn't keeping up — drop
            # the oldest chunk rather than the newest, so a stalled
            # consumer doesn't introduce a growing latency tail.
            try:
                self._audio_in_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._audio_in_queue.put_nowait(chunk)

    async def push_event(self, ev: CallEvent) -> None:
        try:
            self._events_queue.put_nowait(ev)
        except asyncio.QueueFull:
            # Drop oldest non-status event; never drop a status event
            # (they're terminal and the brain needs them).
            if isinstance(ev, CallStatusEvent):
                # Force-room one out
                try:
                    self._events_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._events_queue.put_nowait(ev)

    async def _audio_in_iter(self) -> AsyncIterator[bytes]:
        while not self.closed:
            try:
                chunk = await asyncio.wait_for(
                    self._audio_in_queue.get(), timeout=1.0
                )
                yield chunk
            except TimeoutError:
                continue

    async def _events_iter(self) -> AsyncIterator[CallEvent]:
        while not self.closed:
            try:
                ev = await asyncio.wait_for(
                    self._events_queue.get(), timeout=1.0
                )
                yield ev
                if isinstance(ev, CallStatusEvent) and ev.status in (
                    CallStatus.HUNG_UP,
                    CallStatus.FAILED,
                ):
                    return
            except TimeoutError:
                continue

    def as_call_session(self) -> CallSession:
        sink = _TelnyxAudioSink(self)
        # ``CallSession`` now inherits ``session_id`` / ``audio_in``
        # / ``audio_out`` / ``events`` from the generic
        # ``ConversationSession``. Phone-specific code still reads
        # ``call_id`` (kept as a property alias) and ``hang_up``
        # (kept as a method alias for ``end_session``).
        session = CallSession(
            session_id=self.call_id,
            audio_in=self._audio_in_iter(),
            audio_out=sink,
            events=self._events_iter(),
        )
        # Bind hang_up at construction so the brain can ``await
        # session.hang_up()`` without holding a backend reference.
        # The same callable is exposed under ``end_session`` so the
        # conversation engine can drive any session uniformly.
        session.hang_up = self._do_hang_up  # type: ignore[method-assign]
        session.end_session = self._do_hang_up  # type: ignore[method-assign]
        return session

    async def _do_hang_up(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            await self.backend._api_hangup(self.call_control_id)
        except Exception:
            logger.exception("Telnyx hangup API call failed for %s", self.call_id)
        await self.push_event(
            CallStatusEvent(status=CallStatus.HUNG_UP, reason="local_hangup")
        )
        if self.media_ws is not None:
            try:
                await self.media_ws.close()
            except Exception:
                pass


class _TelnyxAudioSink:
    """Outbound-audio sink. Serializes mulaw bytes into Telnyx's
    ``media`` JSON frames and sends them over the active Media WS.

    Telnyx expects base64-encoded mulaw 8 kHz chunks of any size, but
    we batch into 20 ms frames upstream (160 bytes) so barge-in cancel
    takes effect at chunk boundaries.
    """

    def __init__(self, session: _TelnyxCallSession) -> None:
        self._session = session
        # Per-call telemetry — bytes/frames out, dropped writes.
        # Logged from ``write`` so we can tell at a glance whether
        # audio is actually leaving Gilbert.
        self._sent = 0
        self._dropped = 0

    async def write(self, chunk: bytes) -> None:
        ws = self._session.media_ws
        if ws is None:
            # Surface this — silent drops here look identical to "TTS
            # ran but the recipient heard silence," and we've burned
            # debugging cycles on exactly that.
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 50 == 0:
                logger.warning(
                    "media WS write dropped (no active WS) — count=%d call=%s",
                    self._dropped,
                    self._session.call_id,
                )
            return
        # Telnyx's docs for bidirectional WS injection (see
        # /docs/voice/programmable-voice/media-streaming) show the
        # outbound frame as just ``{"event": "media", "media":
        # {"payload": ...}}`` — no ``stream_id`` at the top level.
        # The WebSocket itself is already scoped to one stream, so
        # the field is implicit. We used to send a top-level
        # ``stream_id`` defensively but it's not part of the documented
        # contract; keeping it out matches what their reference
        # examples do.
        frame = {
            "event": "media",
            "media": {
                "payload": base64.b64encode(chunk).decode("ascii"),
            },
        }
        try:
            await ws.send_text(json.dumps(frame))
            self._sent += 1
            # First-byte log is the smoking gun for "did any audio
            # leave Gilbert at all" — keep it loud.
            if self._sent == 1:
                logger.info(
                    "media WS first outbound chunk sent — call=%s "
                    "stream_id=%s bytes=%d",
                    self._session.call_id,
                    self._session.stream_id,
                    len(chunk),
                )
        except Exception:
            logger.warning(
                "media WS send failed — call=%s sent_before_failure=%d",
                self._session.call_id,
                self._sent,
                exc_info=True,
            )

    async def clear(self) -> None:
        ws = self._session.media_ws
        if ws is None:
            return
        # Telnyx supports ``clear`` to drop everything they have
        # buffered for the outbound direction — used on barge-in.
        # Same shape contract as ``media`` frames: no top-level
        # ``stream_id`` needed; the WS scopes the operation.
        try:
            await ws.send_text(json.dumps({"event": "clear"}))
        except Exception:
            logger.debug("media WS clear failed", exc_info=True)


# ── The backend ──────────────────────────────────────────────────────


class TelnyxTelephony(TelephonyBackend):
    backend_name: ClassVar[str] = "telnyx"

    def __init__(self) -> None:
        self._api_key: str = ""
        self._connection_id: str = ""
        # Public base URL where Telnyx can reach our webhook + media WS
        # endpoints. Pulled from config or (later) auto-detected via
        # TunnelService. Required — Telnyx can't reach private hosts.
        self._public_url: str = ""
        self._http: httpx.AsyncClient | None = None

    @classmethod
    def backend_config_params(cls) -> list[ConfigParam]:
        return [
            ConfigParam(
                key="api_key",
                type=ToolParameterType.STRING,
                description=(
                    "Telnyx API v2 key (starts with KEY...). Found in "
                    "the Telnyx portal under Account → API Keys."
                ),
                default="",
                sensitive=True,
            ),
            ConfigParam(
                key="connection_id",
                type=ToolParameterType.STRING,
                description=(
                    "Telnyx Call Control Connection id. Found under "
                    "Voice → Call Control Applications. This is what "
                    "tells Telnyx which application's webhook URL to "
                    "use for events from outbound calls."
                ),
                default="",
            ),
            ConfigParam(
                key="public_url",
                type=ToolParameterType.STRING,
                description=(
                    "Public HTTPS base URL where Telnyx can reach this "
                    "Gilbert instance for webhooks + the media-stream "
                    'WebSocket (e.g. "https://gilbert.example.com"). '
                    "Webhooks land at ``/api/telnyx/webhook`` and the "
                    "media stream at ``wss://.../api/telnyx/media``."
                ),
                default="",
            ),
        ]

    async def initialize(self, config: dict[str, object]) -> None:
        self._api_key = str(config.get("api_key") or "")
        self._connection_id = str(config.get("connection_id") or "")
        self._public_url = str(config.get("public_url") or "").rstrip("/")
        if not self._api_key:
            logger.warning("TelnyxTelephony initialized without an api_key")
        if not self._connection_id:
            logger.warning("TelnyxTelephony initialized without a connection_id")
        self._http = httpx.AsyncClient(
            base_url=_TELNYX_API,
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=30.0,
        )

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def place_call(
        self,
        *,
        to_number: str,
        from_number: str,
        call_id: str,
        webhook_token: str,
    ) -> CallSession:
        if self._http is None:
            raise RuntimeError("TelnyxTelephony is not initialized")
        if not self._connection_id:
            raise RuntimeError("Telnyx connection_id is not configured")
        if not self._public_url:
            raise RuntimeError(
                "Telnyx public_url is not configured — Telnyx can't reach "
                "your Gilbert instance without a publicly-routable URL."
            )

        # Build the session record + register it BEFORE the API call so
        # any webhook racing back at us has a target.
        session = _TelnyxCallSession(
            call_id=call_id,
            call_control_id="",
            webhook_token=webhook_token,
            backend=self,
        )
        _active_sessions[call_id] = session

        webhook_url = urljoin(self._public_url + "/", "api/telnyx/webhook")
        media_url = urljoin(
            self._public_url.replace("https://", "wss://").replace(
                "http://", "ws://"
            )
            + "/",
            "api/telnyx/media",
        )

        try:
            r = await self._http.post(
                "calls",
                json={
                    "connection_id": self._connection_id,
                    "to": to_number,
                    "from": from_number,
                    "webhook_url": webhook_url,
                    "webhook_url_method": "POST",
                    "stream_url": media_url,
                    # ``inbound_track`` = the remote party's audio
                    # only. With ``both_tracks`` Telnyx mixes
                    # Gilbert's own outbound TTS back into the
                    # inbound stream; STT then transcribes Gilbert
                    # talking to himself and the brain sees every
                    # one of its own utterances as a fresh "user
                    # said" turn. The reported "Gilbert kept asking
                    # me to ask a question" symptom was exactly that
                    # feedback loop — Scribe was returning lines
                    # like "Hi, this is Gilbert, Jeremy Arnold's
                    # personal assistant." as user input.
                    #
                    # Outbound audio still works because the
                    # ``stream_bidirectional_mode`` setting below
                    # enables the inject-into-call path independently
                    # of which track gets mirrored back to us. If we
                    # ever want a full-call recording, use Telnyx's
                    # separate recording API rather than tee'ing the
                    # media stream — that keeps the STT input clean.
                    "stream_track": "inbound_track",
                    # Bidirectional mode is REQUIRED to send audio FROM
                    # the application INTO the call. Without it Telnyx
                    # silently drops every outbound media frame — no
                    # ``streaming.failed`` event, no error on the WS,
                    # just dead air on the recipient's phone. The name
                    # ``"rtp"`` is a Telnyx misnomer: no separate RTP
                    # socket is opened. Audio still flows back over
                    # this same WebSocket; ``"rtp"`` just enables the
                    # inject-into-call path. (Earlier we'd briefly
                    # tried this value and seen ``streaming.failed``,
                    # but that was a different config defect — the
                    # docs at developers.telnyx.com/docs/voice
                    # /programmable-voice/media-streaming explicitly
                    # require this for outbound injection.)
                    "stream_bidirectional_mode": "rtp",
                    "stream_bidirectional_codec": "PCMU",
                    # ``stream_custom_parameters`` is what Telnyx echoes
                    # into the ``start`` frame's ``custom_parameters``.
                    # We use ``call_control_id`` from the start frame
                    # as the primary auth signal, but this is a useful
                    # belt-and-suspenders so a future media-WS handler
                    # that wants the Gilbert call_id can grab it
                    # without round-tripping through the sidecar map.
                    "stream_custom_parameters": {
                        "call_id": call_id,
                        "token": webhook_token,
                    },
                    # Round-trip the token so the media-WS handshake
                    # can authenticate the inbound socket against the
                    # correct session.
                    "custom_headers": [
                        {"name": "X-Gilbert-Call-Id", "value": call_id},
                        {"name": "X-Gilbert-Token", "value": webhook_token},
                    ],
                    "client_state": _encode_client_state(
                        {"call_id": call_id, "token": webhook_token}
                    ),
                },
            )
            if r.status_code >= 400:
                # Surface Telnyx's structured error body — they return
                # JSON with a list of {code, title, detail, source}
                # objects that pinpoint which field / config is wrong.
                # ``raise_for_status`` alone gives "Client error 403"
                # with no actionable detail, which makes diagnosing
                # carrier-side misconfigurations a guessing game.
                try:
                    body = r.json()
                except Exception:
                    body = {"raw": r.text[:500]}
                logger.error(
                    "Telnyx place_call rejected: status=%d body=%s",
                    r.status_code,
                    body,
                )
                raise RuntimeError(
                    f"Telnyx returned {r.status_code}: {body}"
                )
            data = r.json().get("data", {})
            session.call_control_id = str(data.get("call_control_id") or "")
            _call_control_to_gilbert[session.call_control_id] = call_id
        except Exception as exc:
            # Failed before the call left our process — clean up.
            _active_sessions.pop(call_id, None)
            await session.push_event(
                CallStatusEvent(
                    status=CallStatus.FAILED,
                    reason=f"telnyx_place_call_failed: {exc}",
                )
            )
            raise

        await session.push_event(CallStatusEvent(status=CallStatus.INITIATED))
        return session.as_call_session()

    async def _api_hangup(self, call_control_id: str) -> None:
        """Tell Telnyx to terminate the call. Safe to call repeatedly —
        Telnyx returns 404 if the call already ended."""
        if self._http is None or not call_control_id:
            return
        try:
            r = await self._http.post(
                f"calls/{call_control_id}/actions/hangup",
                json={},
            )
            if r.status_code not in (200, 404):
                logger.warning(
                    "Telnyx hangup returned %d: %s",
                    r.status_code,
                    r.text[:200],
                )
        except Exception:
            logger.debug("Telnyx hangup API failure", exc_info=True)


# ── Webhook + media-stream entry points (used by the route handlers) ──


# Map Telnyx event types → CallStatus transitions we surface.
_STATUS_MAP = {
    "call.initiated": CallStatus.INITIATED,
    "call.bridged": CallStatus.CONNECTED,
    "call.answered": CallStatus.CONNECTED,
    "call.hangup": CallStatus.HUNG_UP,
}


async def deliver_webhook_event(payload: dict[str, Any]) -> None:
    """Route an inbound Telnyx webhook into the matching call session.

    Webhook shape (abbreviated): ::

        {
          "data": {
            "event_type": "call.answered",
            "payload": {
              "call_control_id": "...",
              "hangup_cause": "normal_clearing"
            }
          }
        }
    """
    data = (payload or {}).get("data") or {}
    event_type = str(data.get("event_type") or "")
    inner = data.get("payload") or {}
    cc_id = str(inner.get("call_control_id") or "")
    # One-line per-webhook trace so the call's full lifecycle is
    # visible in the journal. ``hangup_cause`` is the most useful
    # snippet for the common case (call ended); other event types
    # have their own per-handler logging below.
    logger.info(
        "Telnyx webhook: event=%s call_control_id=%s extras=%s",
        event_type,
        cc_id[:24] + "…" if len(cc_id) > 24 else cc_id,
        {
            k: v
            for k, v in inner.items()
            if k in ("hangup_cause", "hangup_source", "reason", "state")
        },
    )
    if not cc_id:
        return
    session = find_session_by_call_control_id(cc_id)
    if session is None:
        logger.debug(
            "Webhook for unknown call_control_id %s (event %s)",
            cc_id,
            event_type,
        )
        return

    status = _STATUS_MAP.get(event_type)
    if status is not None:
        reason = ""
        if status is CallStatus.HUNG_UP:
            reason = str(inner.get("hangup_cause") or "")
        await session.push_event(CallStatusEvent(status=status, reason=reason))
        if status in (CallStatus.HUNG_UP, CallStatus.FAILED):
            _active_sessions.pop(session.call_id, None)
            _call_control_to_gilbert.pop(cc_id, None)
        return

    if event_type == "call.dtmf.received":
        digit = str(inner.get("digit") or "")
        if digit:
            await session.push_event(DtmfEvent(digit=digit))
        return

    if event_type.startswith("streaming."):
        # streaming.started / streaming.failed / streaming.stopped — we
        # mostly don't need to surface these to the brain, but logging
        # them helps when debugging carrier-side stream issues.
        if event_type == "streaming.failed":
            # Telnyx is inconsistent about which key carries the
            # actual failure reason — sometimes ``reason``, sometimes
            # ``description``, sometimes nested under ``meta``. Log
            # the whole inner payload so we can see what they
            # actually said.
            logger.warning(
                "Telnyx streaming.failed for call %s — full payload: %s",
                session.call_id,
                inner,
            )
            failure_text = (
                inner.get("reason")
                or inner.get("description")
                or inner.get("failure_reason")
                or (inner.get("meta") or {}).get("reason")
                or "no reason given"
            )
            await session.push_event(
                CallErrorEvent(
                    message=f"Telnyx stream failed: {failure_text}",
                    recoverable=False,
                )
            )
        return


# ── client_state ──────────────────────────────────────────────────────


def _encode_client_state(state: dict[str, Any]) -> str:
    """Telnyx requires ``client_state`` to be base64-encoded JSON.

    They echo it back on every webhook for the call, which is how we
    correlate webhook events to our internal call_id when the
    call_control_id sidecar happens to be missing.
    """
    raw = json.dumps(state).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")
