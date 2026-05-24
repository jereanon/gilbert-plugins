"""Voice-agent service — wake-word activated voice conversations.

Wires three existing capabilities together:

- ``WakeWordListener`` (from ``TranscriptionService``) — runs a
  continuous, locally-cheap detector on a mic stream. Fires
  ``WakeEvent`` when a configured keyword is heard.
- ``ConversationEngine`` (from ``VoiceBrainService``) — drives the
  LLM-turn loop / STT / TTS / barge-in / brain-tool dispatch.
- ``ConversationMessagePoster`` (from ``AIService``) — posts a
  summary message back into the user's chat conversation when a
  voice session ends, so the chat log reflects what happened.

Lifecycle (the wake-word-gated approach the user asked for):

```
Idle:        WakeWordDetector running (cheap, local)
  ↓ WakeEvent
Activated:   Open Scribe Live + start ConversationEngine
  ↓
Active turn-taking (engine runs, brain tools dispatch)
  ↓ idle_timeout reached
  OR brain calls end_conversation tool
  OR a "stop listening" wake phrase fires
  ↓
Close STT, post chat summary, back to Idle
```

This file is the **skeleton** for step 4 of the conversation-engine
refactor. The wiring is complete (wake-word → session → engine →
summary post). The one structural hole is the **mic source / speaker
sink**: Gilbert has a ``SpeakerBackend`` abstraction for audio out
but no symmetrical continuous-mic source today. The skeleton
currently registers itself, accepts config, and resolves the engine
capability — but ``start_session`` raises NotImplementedError until
a mic backend lands. The point of landing the skeleton now is to
validate that the conversation-engine abstractions are shaped
correctly for a non-phone consumer.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from gilbert.interfaces.ai import ConversationMessagePoster
from gilbert.interfaces.conversation import (
    AudioSink,
    BrainToolResult,
    ConversationConfig,
    ConversationContext,
    ConversationEngine,
    ConversationErrorEvent,
    ConversationOutcome,
    ConversationSession,
    ConversationStatus,
    ConversationStatusEvent,
    OpeningBehavior,
    OpeningPolicy,
    get_current_conversation_ctx,
)
from gilbert.interfaces.service import Service, ServiceInfo, ServiceResolver
from gilbert.interfaces.storage import StorageProvider
from gilbert.interfaces.tools import (
    ToolDefinition,
    ToolParameter,
    ToolParameterType,
)
from gilbert.interfaces.transcription import (
    AudioFormat as TranscriptionAudioFormat,
    AudioEncoding,
    WakeEvent,
    WakeWordConfig,
    WakeWordListener,
)

logger = logging.getLogger(__name__)


_COLLECTION = "voice_conversations"

# Fillers the engine speaks while the LLM is still thinking. Kept
# short — each one is a 1-3 word interjection so the TTS round-trip
# adds maybe a second of wall time. Variety so consecutive slow
# answers don't all open with "hmm." The list is randomly sampled
# per slow turn.
_DEFAULT_FILLER_PHRASES = [
    "Hmm.",
    "Hmm, let me check.",
    "One sec.",
    "Let me look that up.",
    "Give me a moment.",
    "Hmm, looking.",
    "Just a sec.",
]


_DEFAULT_SYSTEM_PROMPT = (
    "You are Gilbert, the user's personal AI assistant, responding to a "
    "voice interaction. Keep replies short — one or two sentences max. "
    "No markdown, no lists. Skip opening filler ('great question', "
    "'happy to') and just answer. Do NOT start with 'hmm' / 'let me "
    "check' / 'one sec' — the runtime handles that automatically when "
    "a tool call is going to make you slow. If you open with a filler "
    "of your own, the user hears it TWICE. Just give them the answer."
    "\n\n"
    "The conversation stays OPEN by default. Acknowledgements like "
    "'okay', 'thanks', 'got it', 'that makes sense' mean the user "
    "received your last reply and may follow up — they are NOT goodbyes. "
    "Stay quiet on a bare acknowledgement (return an empty message with "
    "no tools) and wait for the next real turn. Only call "
    "``end_conversation`` when the user EXPLICITLY says goodbye / "
    "asks to stop. If you're unsure, ask 'anything else?' instead of "
    "ending."
)


# ── Brain-tool provider for the voice-agent modality ─────────────────


class VoiceAgentBrainToolProvider:
    """Implements ``BrainToolProvider``. One tool — ``end_conversation``
    — which is the voice-agent equivalent of phone's ``hang_up``.

    Future tools will probably include ``set_reminder``, ``play_music``,
    ``query_calendar``, etc. — but those should NOT live here; they
    should come from whichever Gilbert service owns each capability,
    aggregated. For the skeleton, just ``end_conversation``.
    """

    def get_brain_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="end_conversation",
                description=(
                    "End this voice interaction. Use ONLY when the user "
                    "EXPLICITLY signals they're done — saying \"bye\", "
                    "\"that's all\", \"I'm done\", \"goodbye\", \"talk to "
                    "you later\", or directly asking you to stop / end / "
                    "close the session. "
                    "\n\n"
                    "Do NOT end on simple acknowledgements like \"okay\", "
                    "\"got it\", \"thanks\", \"that makes sense\", "
                    "\"interesting\", \"cool\". Those mean the user "
                    "received your last answer and may have a follow-up. "
                    "When you're unsure whether the user is done, ASK "
                    "(\"Anything else?\") rather than ending. "
                    "\n\n"
                    "Voice conversations default to staying OPEN — silence "
                    "is fine, the user can speak again whenever. Better to "
                    "leave the line open and be available than to hang up "
                    "prematurely and force the user to start a new session. "
                    "\n\n"
                    "Bookkeeping only — does NOT produce a goodbye line, "
                    "you say that yourself in the same turn's message "
                    "content."
                ),
                parameters=[
                    ToolParameter(
                        name="summary",
                        type=ToolParameterType.STRING,
                        description=(
                            "One-line summary of what was accomplished. "
                            "Posted into the user's chat conversation."
                        ),
                    ),
                ],
            ),
        ]

    async def handle_brain_tool(
        self,
        name: str,
        args: dict[str, Any],
        ctx: ConversationContext,
    ) -> BrainToolResult:
        if name == "end_conversation":
            summary = str(args.get("summary") or "").strip()
            if summary:
                ctx.outcome["session_summary"] = summary
                await ctx.record_turn("system", f"(summary: {summary})")
            return BrainToolResult.END_CONVERSATION
        logger.warning("voice-agent brain emitted unknown tool: %s", name)
        return BrainToolResult.OK


# ── Conversation session — concrete impl for wake-word modality ──────


class _BrowserAudioSink:
    """Buffers TTS bytes per utterance and ships each completed clip
    to the browser tab via the standard ``speaker.browser.play`` event
    on the event bus.

    The engine writes 20ms chunks at 50fps via ``write()``. We
    accumulate them in a ``bytearray``. ``flush()`` (called by the
    engine at end-of-utterance) base64-encodes the whole clip as a
    ``data:`` URL and emits the bus event the existing
    ``useBrowserSpeaker`` SPA hook is already listening for.
    ``clear()`` discards anything buffered (mid-utterance barge-in,
    though barge-in is unavailable in turn-taking mode anyway).

    Real-time bytes-over-WS playback is a future iteration — turning
    each utterance into a single MP3 keeps the demo simple and reuses
    the BrowserSpeaker plumbing that's been working for months.
    """

    def __init__(
        self,
        *,
        bus: Any,
        user_id: str,
        session_id: str,
        mime: str = "audio/mpeg",
    ) -> None:
        self._bus = bus
        self._user_id = user_id
        self._session_id = session_id
        self._mime = mime
        self._buffer = bytearray()
        self._utterances_sent = 0

    async def write(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    async def clear(self) -> None:
        self._buffer.clear()

    async def flush(self) -> None:
        if not self._buffer:
            return
        import base64

        from gilbert.interfaces.events import Event

        data_b64 = base64.b64encode(bytes(self._buffer)).decode("ascii")
        url = f"data:{self._mime};base64,{data_b64}"
        self._utterances_sent += 1
        logger.info(
            "BrowserAudioSink flush — session=%s utterance=%d bytes=%d",
            self._session_id,
            self._utterances_sent,
            len(self._buffer),
        )
        await self._bus.publish(
            Event(
                event_type="speaker.browser.play",
                data={
                    "user_id": self._user_id,
                    "conversation_id": "",
                    "url": url,
                    "title": f"Gilbert (voice-agent {self._session_id})",
                    "volume": 80,
                    "announce": False,
                    "position_seconds": 0,
                    "kind": "voice_agent_turn",
                },
                source="voice_agent",
            )
        )
        self._buffer.clear()


@dataclass
class _VoiceAgentSession(ConversationSession):
    """Concrete ``ConversationSession`` for a voice-agent interaction.

    Holds the audio queues + event queue the engine consumes. The
    plugin's start_session method (TODO) feeds inbound chunks via
    ``push_audio_in``; ``push_event`` is used to drive the status
    transition from PENDING → ACTIVE when the wake-word fires, and
    from ACTIVE → ENDED on idle-timeout.
    """

    _audio_in_queue: asyncio.Queue[bytes] = field(
        default_factory=lambda: asyncio.Queue(maxsize=500)
    )
    _events_queue: asyncio.Queue[Any] = field(
        default_factory=lambda: asyncio.Queue(maxsize=200)
    )
    closed: bool = False

    async def push_audio_in(self, chunk: bytes) -> None:
        try:
            self._audio_in_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            try:
                self._audio_in_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._audio_in_queue.put_nowait(chunk)

    async def push_event(self, ev: Any) -> None:
        try:
            self._events_queue.put_nowait(ev)
        except asyncio.QueueFull:
            pass

    async def _audio_in_iter(self) -> AsyncIterator[bytes]:
        while not self.closed:
            try:
                chunk = await asyncio.wait_for(
                    self._audio_in_queue.get(), timeout=1.0
                )
                yield chunk
            except TimeoutError:
                continue

    async def _events_iter(self) -> AsyncIterator[Any]:
        while not self.closed:
            try:
                ev = await asyncio.wait_for(
                    self._events_queue.get(), timeout=1.0
                )
                yield ev
                if isinstance(ev, ConversationStatusEvent) and ev.status in (
                    ConversationStatus.ENDED,
                    ConversationStatus.FAILED,
                ):
                    return
            except TimeoutError:
                continue

    async def end_session(self) -> None:
        if self.closed:
            return
        # Just push the terminal event — the iterator notices the
        # ``ConversationStatus.ENDED`` discriminator and exits its
        # generator naturally. Don't flip ``self.closed`` here: that
        # would race the iterator's ``while not self.closed`` check
        # and could swallow the event before it's yielded. The
        # iterator sets a local end-flag via its terminal-event
        # ``return`` instead.
        try:
            self._events_queue.put_nowait(
                ConversationStatusEvent(status=ConversationStatus.ENDED)
            )
        except asyncio.QueueFull:
            pass


# ── Per-WS-connection active-session tracker ────────────────────────


@dataclass
class _ActiveSession:
    """One in-flight voice-agent session. Tracks the conversation
    session object so audio chunks from the SPA can be routed to its
    inbound queue, plus the engine task so cleanup can cancel it.

    Conversational mode adds a small state machine:

    - ``active``: audio flows to both the engine (STT path) and the
      wake-word detector. Brain responds normally.
    - ``dormant``: audio flows ONLY to the wake detector. Engine sits
      idle (no transcripts, no LLM calls, no TTS). Recovers via
      ``"Hey Gilbert"`` wake-word firing.

    ``last_user_activity_ts`` is the timestamp of the most recent
    moment we became "idle and waiting for the user." The silence
    monitor watches it to decide when to drop ``active → dormant``.

    ``responding`` is True while we're NOT idle — i.e. between the
    moment the user finishes speaking and the moment Gilbert finishes
    speaking. That covers LLM thinking time + TTS synthesis + TTS
    playback. The silence monitor short-circuits while
    ``responding`` is True, so a long answer can't trigger dormancy
    mid-sentence. Default ``True`` so the SPEAK_FIRST opener can run
    without the silence monitor racing against it.
    """

    conversation_id: str
    user_id: str
    conn_id: str
    session: "_VoiceAgentSession"
    task: asyncio.Task[Any] | None = None

    # Conversational-mode wiring. ``turn_based`` sessions leave these
    # all None/unused.
    mode: str = "turn_based"                  # "turn_based" | "conversational"
    state: str = "active"                     # "active" | "dormant"
    last_user_activity_ts: float = 0.0
    responding: bool = True                   # see docstring
    wake_detector: Any = None                 # WakeWordDetector | None
    wake_task: asyncio.Task[Any] | None = None
    silence_task: asyncio.Task[Any] | None = None


# ── Persisted record shape ───────────────────────────────────────────


@dataclass
class _VoiceConversationRecord:
    """Stored in the ``voice_conversations`` collection.

    Parallel to phone-call's ``_CallRecord`` but tighter — voice-agent
    sessions don't have caller-ID, hang_up reason, or DTMF. The
    chat-side summary is posted via ``ConversationMessagePoster``;
    this record is the system-of-record for the full transcript.
    """

    conversation_id: str
    user_id: str
    originating_conversation_id: str = ""
    started_at: str = ""
    ended_at: str = ""
    duration_seconds: float = 0.0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    outcome: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "originating_conversation_id": self.originating_conversation_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_seconds": self.duration_seconds,
            "transcript": self.transcript,
            "outcome": self.outcome,
        }


# ── The service ──────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class VoiceAgentService(Service):
    """Wake-word activated voice-conversation service.

    Capability provided: ``voice_agent`` (single, plugin-scoped).
    Capabilities consumed: ``voice_brain``, ``speech_to_text``
    (specifically ``WakeWordListener``), ``ai_chat``
    (specifically ``ConversationMessagePoster`` for chat summary
    posting), ``entity_storage``, ``event_bus``.

    Toggleable — disabled by default. Even when enabled, the service
    needs a mic source to actually run a session (TODO). The
    structural wiring is here to validate the engine abstractions
    against a second consumer.
    """

    def __init__(self) -> None:
        self._resolver: ServiceResolver | None = None
        self._enabled: bool = False
        self._config: dict[str, Any] = {}
        self._voice_brain: ConversationEngine | None = None
        self._wake_listener: WakeWordListener | None = None
        self._message_poster: ConversationMessagePoster | None = None
        self._storage: StorageProvider | None = None
        self._bus: Any = None
        # Active session task. None when in wake-listen mode.
        self._session_task: asyncio.Task[None] | None = None
        # Per-WS-connection active session. Keyed by conn.connection_id.
        # One voice-agent session at a time per browser tab.
        self._sessions: dict[str, _ActiveSession] = {}

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="voice_agent",
            # ``ai_tools`` advertises the ToolProvider implementation
            # below — the AI service discovers ``end_conversation`` and
            # makes it available to LLM turns whose ContextVar puts us
            # inside an active voice session.
            capabilities=frozenset({"voice_agent", "ws_handlers", "ai_tools"}),
            requires=frozenset(
                {
                    "voice_brain",
                    "speech_to_text",  # for WakeWordListener
                    "ai_chat",  # for ConversationMessagePoster
                    "entity_storage",
                    "event_bus",
                }
            ),
            optional=frozenset({"configuration"}),
            toggleable=True,
            toggle_description="Wake-word voice conversations.",
        )

    # --- ToolProvider — voice-session brain tools as Gilbert AI tools ----
    #
    # When voice-agent runs the engine in ``use_full_ai_service`` mode,
    # the LLM gets access to the entire Gilbert tool ecosystem
    # (knowledge.search, MCP tools, agent dispatch, scheduler, …) via
    # ``AIService.chat()``. The session-control tools that used to live
    # in ``VoiceAgentBrainToolProvider`` (the engine-private brain-tool
    # dispatch path) now live HERE — as regular Gilbert tools the AI
    # service discovers + dispatches like anything else.
    #
    # Visibility gating: ``get_tools`` returns ``[]`` when there's no
    # active voice ``ConversationContext``, so ``end_conversation``
    # doesn't pollute regular chat tool lists. The engine sets the
    # ContextVar before invoking ai.chat().

    @property
    def tool_provider_name(self) -> str:
        return "voice_agent"

    def get_tools(
        self, user_ctx: Any = None
    ) -> list[ToolDefinition]:
        # Only surface ``end_conversation`` when we're inside an
        # active voice session. Regular chat turns never see this
        # tool — that prevents the LLM from accidentally calling
        # "end the conversation" in a context where there's no
        # conversation to end.
        if get_current_conversation_ctx() is None:
            return []
        return [
            ToolDefinition(
                name="end_conversation",
                description=(
                    "End the active voice conversation. Use ONLY when "
                    "the user EXPLICITLY signals they're done — saying "
                    "\"bye\", \"goodbye\", \"I'm done\", \"talk to you "
                    "later\", or directly asking you to stop / end / "
                    "close the session. "
                    "\n\n"
                    "Do NOT end on simple acknowledgements like \"okay\", "
                    "\"got it\", \"thanks\", \"that makes sense\", "
                    "\"interesting\". Those mean the user received "
                    "your last answer and may have a follow-up. When "
                    "unsure, ASK (\"Anything else?\") rather than ending. "
                    "\n\n"
                    "Voice conversations default to staying OPEN — "
                    "silence is fine. Better to leave the line open "
                    "than to hang up prematurely. The tool itself is "
                    "bookkeeping — say a goodbye line in your message "
                    "content of the same turn."
                ),
                parameters=[
                    ToolParameter(
                        name="summary",
                        type=ToolParameterType.STRING,
                        description=(
                            "One-line summary of what was accomplished. "
                            "Posted into the user's chat conversation."
                        ),
                    ),
                ],
            ),
        ]

    async def execute_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> str:
        if name == "end_conversation":
            ctx = get_current_conversation_ctx()
            if ctx is None:
                return "(no active voice conversation to end)"
            summary = str(arguments.get("summary") or "").strip()
            if summary:
                ctx.outcome["session_summary"] = summary
                await ctx.record_turn("system", f"(summary: {summary})")
            # The engine inspects this flag after each ai.chat() round
            # and terminates the session when it's set.
            ctx.outcome["end_requested"] = True
            return f"OK, ending the conversation. Summary recorded: {summary}"
        raise KeyError(f"voice_agent has no tool {name!r}")

    # --- WS handlers (the SPA's voice-agent page talks to these) ----

    def get_ws_handlers(self) -> dict[str, Any]:
        """Three RPCs the SPA's voice-agent page uses to drive a session.

        - ``voice_agent.start_session(audio_format)`` — open a session,
          return ``session_id``. The session starts ACTIVE immediately
          (wake-word gating comes in a follow-up).
        - ``voice_agent.send_audio_chunk(session_id, audio_b64)`` —
          base64 PCM_S16LE 16 kHz mono chunks (whatever the SPA's
          AudioWorklet captures).
        - ``voice_agent.end_session(session_id)`` — clean stop. Also
          fires automatically on WS connection close.
        """
        return {
            "voice_agent.start_session": self._ws_start_session,
            "voice_agent.send_audio_chunk": self._ws_send_audio_chunk,
            "voice_agent.end_session": self._ws_end_session,
        }

    async def _ws_start_session(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        # The SPA's ``rpc()`` matches replies by ``frame.ref`` echoing
        # the original frame's ``id``. The WS dispatch layer doesn't
        # auto-inject this — handlers have to echo it themselves on
        # every response or the SPA's pending promise hits the 15-second
        # RPC timeout.
        ref = frame.get("id")
        if not self._enabled:
            return {"ref": ref, "ok": False, "error": "voice-agent service disabled"}
        if self._voice_brain is None:
            return {"ref": ref, "ok": False, "error": "voice_brain unavailable"}
        user_id = conn.user_id or ""
        if not user_id:
            return {"ref": ref, "ok": False, "error": "unauthenticated connection"}

        # One session per WS connection. If a stale one is still
        # around (browser refresh, dropped task), tear it down before
        # spawning a new one.
        old = self._sessions.pop(conn.connection_id, None)
        if old is not None and old.task is not None:
            old.task.cancel()

        originating_conv_id = str(frame.get("originating_conversation_id") or "")
        # Mode: "turn_based" (default, original behaviour) or
        # "conversational" (silence-detection → wake-word dormant
        # state → resume on "Hey Gilbert"). Anything else falls back
        # to turn_based so a malformed client request doesn't break
        # the session.
        mode = str(frame.get("mode") or "turn_based")
        if mode not in ("turn_based", "conversational"):
            mode = "turn_based"
        conversation_id = f"vc_{uuid.uuid4().hex[:12]}"
        log = logger.getChild(f"voice:{conversation_id}")

        # Build the session — audio_in/audio_out wired to the
        # browser via the bus.
        session = _VoiceAgentSession(
            session_id=conversation_id,
            audio_in=None,  # type: ignore[arg-type]  — set below
            audio_out=_BrowserAudioSink(
                bus=self._bus,
                user_id=user_id,
                session_id=conversation_id,
                mime="audio/mpeg",
            ),
            events=None,  # type: ignore[arg-type]
        )
        session.audio_in = session._audio_in_iter()  # type: ignore[assignment]
        session.events = session._events_iter()  # type: ignore[assignment]

        # Spawn the session task. We don't await it here; the
        # ``send_audio_chunk`` handler pushes into the session's
        # queue while the engine consumes.
        active = _ActiveSession(
            conversation_id=conversation_id,
            user_id=user_id,
            conn_id=conn.connection_id,
            session=session,
            mode=mode,
        )
        self._sessions[conn.connection_id] = active

        # Conversational-mode wiring: open a wake-word detector + spawn
        # two helper tasks (wake-event consumer + silence monitor).
        # Failure to open the wake detector is logged but doesn't fail
        # the session — degrade to turn_based behaviour.
        if mode == "conversational":
            try:
                wake_cfg = WakeWordConfig(
                    keywords=["hey_gilbert"],
                    format=TranscriptionAudioFormat(
                        encoding=AudioEncoding.PCM_S16LE,
                        sample_rate=16000,
                        channels=1,
                    ),
                    sensitivity=0.5,
                )
                # Try the TranscriptionService capability first (the
                # "right" path — uses the user's configured default).
                # When the user hasn't set ``transcription.wake_word_backend``
                # in Settings, that fails with "no backend available";
                # fall back to instantiating ``openwakeword`` directly
                # from the registered-backend registry so the voice-
                # agent works out of the box.
                active.wake_detector = await self._open_wake_detector(wake_cfg)
                active.wake_task = asyncio.create_task(
                    self._consume_wake_events(active),
                    name=f"voice-agent-wake:{conversation_id}",
                )
                active.silence_task = asyncio.create_task(
                    self._monitor_silence(active),
                    name=f"voice-agent-silence:{conversation_id}",
                )
                log.info("conversational mode: wake detector + silence monitor armed")
            except Exception:
                log.exception(
                    "Failed to open wake-word detector — "
                    "falling back to turn_based behaviour for this session"
                )
                active.mode = "turn_based"

        # Connection-drop cleanup. Mirrors TranscriptionService.
        def _on_close(cid: str = conn.connection_id) -> None:
            asyncio.create_task(self._teardown_session_by_conn(cid))

        conn.add_close_callback(_on_close)

        active.task = asyncio.create_task(
            self._run_voice_session(
                active=active,
                originating_conversation_id=originating_conv_id,
            ),
            name=f"voice-agent-brain:{conversation_id}",
        )
        active.task.add_done_callback(
            lambda _t, cid=conn.connection_id: self._sessions.pop(cid, None)
        )

        log.info(
            "voice-agent session opened (conn=%s user=%s originating_conv=%r)",
            conn.connection_id,
            user_id,
            originating_conv_id,
        )

        # Mark the session ACTIVE so the engine fires its opening
        # policy (SPEAK_FIRST). Done after task creation so the
        # status loop is running when the event arrives.
        await session.push_event(
            ConversationStatusEvent(status=ConversationStatus.ACTIVE)
        )

        return {"ref": ref, "ok": True, "session_id": conversation_id}

    async def _ws_send_audio_chunk(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        import base64

        ref = frame.get("id")
        sid = frame.get("session_id")
        b64 = frame.get("audio_b64")
        if not isinstance(sid, str) or not isinstance(b64, str):
            return {"ref": ref, "ok": False, "error": "missing session_id or audio_b64"}
        active = self._sessions.get(conn.connection_id)
        if active is None or active.conversation_id != sid:
            return {"ref": ref, "ok": False, "error": "unknown session"}
        try:
            chunk = base64.b64decode(b64)
        except Exception:
            return {"ref": ref, "ok": False, "error": "invalid base64"}

        # Diagnostic: log every 100th chunk with the current routing
        # decision so we can tell at a glance whether the SPA is
        # still sending audio, whether the wake detector is being
        # fed, and whether the engine is. A 100-chunk cadence ≈ 1
        # log line every 8.5 seconds (chunks are ~85 ms at the SPA's
        # downsample rate).
        active.session._inbound_count = (  # type: ignore[attr-defined]
            getattr(active.session, "_inbound_count", 0) + 1
        )
        inbound_count = active.session._inbound_count  # type: ignore[attr-defined]
        if inbound_count % 100 == 0:
            logger.info(
                "voice-agent WS in: session=%s count=%d state=%s mode=%s "
                "bytes=%d → wake=%s engine=%s",
                sid,
                inbound_count,
                active.state,
                active.mode,
                len(chunk),
                "yes" if (active.mode == "conversational" and active.wake_detector) else "no",
                "no (dormant)" if (active.mode == "conversational" and active.state == "dormant") else "yes",
            )

        # Conversational mode: ALWAYS feed the wake detector (cheap,
        # local) so "Hey Gilbert" can recover from dormant state.
        # In DORMANT, the user's REAL audio should NOT reach the
        # brain — that's the whole point of dormancy. BUT we still
        # have to keep the engine's STT stream alive, because
        # ElevenLabs Scribe Realtime closes its WebSocket after
        # ~15s of no audio. Once that closes, the engine's listen
        # loop exits and even after wake recovery, no transcripts
        # ever reach the brain again. Symptom: "Gilbert came alive
        # one time, then stops responding to subsequent wake words."
        #
        # Solution: in dormant, swap the real audio for an
        # equivalently-sized silent buffer before pushing to the
        # engine. Scribe sees a steady stream of (silent) frames
        # and keeps the WS open. The wake detector still gets the
        # REAL audio so "Hey Gilbert" still wakes us. On wake →
        # active, real audio resumes flowing and Scribe is still
        # warm and ready to transcribe.
        if active.mode == "conversational" and active.wake_detector is not None:
            try:
                await active.wake_detector.send(chunk)
            except Exception:
                logger.debug("wake detector send failed", exc_info=True)
            if active.state == "dormant":
                silent = b"\x00" * len(chunk)
                await active.session.push_audio_in(silent)
                return {"ref": ref, "ok": True}

        await active.session.push_audio_in(chunk)
        return {"ref": ref, "ok": True}

    async def _ws_end_session(
        self, conn: Any, frame: dict[str, Any]
    ) -> dict[str, Any]:
        ref = frame.get("id")
        sid = frame.get("session_id")
        active = self._sessions.get(conn.connection_id)
        if active is None or (sid is not None and active.conversation_id != sid):
            return {"ref": ref, "ok": False, "error": "unknown session"}
        await active.session.end_session()
        return {"ref": ref, "ok": True}

    async def _teardown_session_by_conn(self, conn_id: str) -> None:
        active = self._sessions.get(conn_id)
        if active is None:
            return
        # Cancel the conversational helpers first so they don't fire
        # state transitions during shutdown.
        if active.wake_task is not None:
            active.wake_task.cancel()
        if active.silence_task is not None:
            active.silence_task.cancel()
        if active.wake_detector is not None:
            try:
                await active.wake_detector.close()
            except Exception:
                logger.debug("wake detector close failed", exc_info=True)
        await active.session.end_session()

    # --- Conversational-mode helpers ---------------------------------

    async def _open_wake_detector(self, config: WakeWordConfig) -> Any:
        """Open a wake-word detector, preferring the user's configured
        default but falling back to a direct ``openwakeword`` instance
        from the backend registry so conversational mode works out of
        the box without a separate Settings configuration step.

        Both paths return a ``WakeWordDetector`` the caller drives
        via ``send()`` / ``events()`` / ``close()``.
        """
        # Path 1: TranscriptionService default (respects whatever the
        # user has wired up in Settings).
        if self._wake_listener is not None:
            try:
                return await self._wake_listener.open_detector(config)
            except RuntimeError as exc:
                # The TranscriptionService raises ``no transcription
                # backend available for wake_word`` when no default is
                # configured. Anything else is a real error we don't
                # want to swallow.
                if "wake_word" not in str(exc).lower():
                    raise
                logger.info(
                    "TranscriptionService has no default wake-word backend; "
                    "trying registered backends directly"
                )

        # Path 2: walk the WakeWordBackend registry and use openwakeword
        # if it's loaded. The plugin registers via ``__init_subclass__``
        # at import time, so all that's needed is that the openwakeword
        # plugin loaded (which we already log on startup).
        from gilbert.interfaces.transcription import WakeWordBackend

        registered = WakeWordBackend.registered_backends()
        # Prefer "openwakeword" specifically since that's the one with
        # the ``hey_gilbert.onnx`` model bundled. Fall through to
        # whatever's available.
        for name in ("openwakeword", *registered.keys()):
            backend_cls = registered.get(name)
            if backend_cls is None:
                continue
            backend = backend_cls()
            await backend.initialize({})
            logger.info(
                "voice-agent: opened wake-word detector via direct %r backend",
                name,
            )
            return await backend.open_detector(config)

        raise RuntimeError(
            "no wake-word backend registered (is the openwakeword "
            "plugin loaded?)"
        )

    _SILENCE_THRESHOLD_SECONDS = 10.0
    """How long without a user transcript before we drop ``active``
    sessions into ``dormant`` (wake-word listening) mode."""

    async def _publish_state_change(
        self,
        active: _ActiveSession,
        new_state: str,
        reason: str = "",
    ) -> None:
        """Emit a ``voice_agent.state_changed`` bus event so the SPA
        can update its UI (e.g. show "Listening for 'Hey Gilbert'…"
        when dormant). Same per-user visibility filter as other
        voice_agent events."""
        if self._bus is None:
            return
        from gilbert.interfaces.events import Event

        await self._bus.publish(
            Event(
                event_type="voice_agent.state_changed",
                data={
                    "user_id": active.user_id,
                    "session_id": active.conversation_id,
                    "state": new_state,
                    "reason": reason,
                },
                source="voice_agent",
            )
        )

    async def _transition_to_dormant(
        self, active: _ActiveSession, reason: str = "silence"
    ) -> None:
        if active.state == "dormant":
            return
        active.state = "dormant"
        logger.info(
            "voice-agent session %s → dormant (%s)",
            active.conversation_id,
            reason,
        )
        # Drain any half-buffered audio toward STT so the engine
        # doesn't accidentally process leftover speech after we go
        # dormant. The audio_in queue's bounded size keeps this from
        # being unbounded work.
        try:
            while True:
                active.session._audio_in_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        await self._publish_state_change(active, "dormant", reason)

    async def _transition_to_active(
        self, active: _ActiveSession, reason: str = "wake_word"
    ) -> None:
        if active.state == "active":
            return
        active.state = "active"
        # Wake recovery: the user just signalled they want to talk
        # (said "Hey Gilbert"). Clear ``responding`` and reset the
        # idle timestamp so the silence monitor gives them the full
        # threshold to start their question. If they don't, we'll
        # drop back to dormant cleanly.
        active.responding = False
        active.last_user_activity_ts = asyncio.get_event_loop().time()
        logger.info(
            "voice-agent session %s → active (%s) — "
            "responding=False, timer reset",
            active.conversation_id,
            reason,
        )
        await self._publish_state_change(active, "active", reason)

    async def _monitor_silence(self, active: _ActiveSession) -> None:
        """Watch ``last_user_activity_ts``; drop to dormant after
        ``_SILENCE_THRESHOLD_SECONDS`` of nothing.

        Started once per conversational session, cancelled at
        teardown. Polls every 1s — finer-grained doesn't gain
        anything since the threshold is 10s.
        """
        log = logger.getChild(f"silence:{active.conversation_id}")
        try:
            while not active.session.closed:
                await asyncio.sleep(1.0)
                if active.state != "active":
                    continue
                if active.responding:
                    # LLM is thinking or Gilbert is mid-response.
                    # We're not idle. Don't count toward dormancy.
                    continue
                if active.last_user_activity_ts == 0.0:
                    # Haven't established a baseline yet (initial
                    # opener still in flight). Wait for
                    # ``on_speaking_done`` to arm the countdown.
                    continue
                elapsed = (
                    asyncio.get_event_loop().time() - active.last_user_activity_ts
                )
                if elapsed >= self._SILENCE_THRESHOLD_SECONDS:
                    await self._transition_to_dormant(
                        active, reason=f"{int(elapsed)}s silence"
                    )
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("silence monitor crashed")

    async def _consume_wake_events(self, active: _ActiveSession) -> None:
        """Consume ``WakeEvent``s from the detector and switch
        dormant → active on a hit."""
        log = logger.getChild(f"wake:{active.conversation_id}")
        if active.wake_detector is None:
            return
        try:
            async for ev in active.wake_detector.events():
                log.info(
                    "wake event fired — keyword=%r confidence=%s",
                    ev.keyword,
                    ev.confidence,
                )
                if active.state == "dormant":
                    await self._transition_to_active(active, reason=ev.keyword)
                # In active mode, wake events are no-ops — the user
                # said "Hey Gilbert" but the line was already live.
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("wake event consumer crashed")

    @property
    def config_namespace(self) -> str:
        return "voice_agent"

    @property
    def config_category(self) -> str:
        return "speech"

    # --- Lifecycle ---------------------------------------------------

    async def start(self, resolver: ServiceResolver) -> None:
        self._resolver = resolver

        storage = resolver.get_capability("entity_storage")
        if isinstance(storage, StorageProvider):
            self._storage = storage
        brain_svc = resolver.get_capability("voice_brain")
        if isinstance(brain_svc, ConversationEngine):
            self._voice_brain = brain_svc
        stt_svc = resolver.get_capability("speech_to_text")
        if isinstance(stt_svc, WakeWordListener):
            self._wake_listener = stt_svc
        ai = resolver.get_capability("ai_chat")
        if isinstance(ai, ConversationMessagePoster):
            self._message_poster = ai
        # Event bus for ``speaker.browser.play`` (TTS playback) + any
        # future ``voice_agent.*`` server-initiated events.
        from gilbert.interfaces.events import EventBusProvider

        bus_svc = resolver.get_capability("event_bus")
        if isinstance(bus_svc, EventBusProvider):
            self._bus = bus_svc.bus

        # Pull config — same pattern as PhoneCallService.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        # First-run: nothing in the DB yet means "use the default."
        # voice-agent ships ``enabled: true`` in plugin.yaml; mirror
        # that here as the fall-through default so a fresh install
        # exposes the feature without requiring a trip to /settings.
        # Explicit ``enabled: false`` via the UI still wins.
        if not section.get("enabled", True):
            logger.info("Voice-agent service disabled (set explicitly)")
            return

        self._enabled = True
        self._config = dict(section)
        logger.info(
            "Voice-agent service started "
            "(brain=%s wake=%s poster=%s, mic_source=TODO)",
            "✓" if self._voice_brain else "✗",
            "✓" if self._wake_listener else "✗",
            "✓" if self._message_poster else "✗",
        )

        # TODO: kick off the wake-word listening loop here. Blocked on
        # a MicBackend abstraction or wiring up the existing
        # browser-mic streaming flow as the audio source.

    async def stop(self) -> None:
        if self._wake_task is not None:
            self._wake_task.cancel()
        if self._session_task is not None:
            self._session_task.cancel()

    # --- Session lifecycle (the actual conversation) -----------------

    async def _run_voice_session(
        self,
        *,
        active: _ActiveSession,
        originating_conversation_id: str,
    ) -> str:
        """Drive one voice-agent conversation through the engine.

        Invoked as a background task by ``_ws_start_session`` — the
        SPA pumps audio chunks via ``send_audio_chunk`` while the
        engine runs here. Returns when:

        - The brain calls ``end_conversation``
        - The session's idle-timeout watchdog fires
        - The SPA explicitly ends the session
        - The WS connection drops

        Persists a per-session record and (when an originating chat
        was supplied) posts a one-line summary back into that chat
        via ``ConversationMessagePoster``.
        """
        if self._voice_brain is None:
            raise RuntimeError("voice_agent: voice_brain not available")

        log = logger.getChild(f"voice:{active.conversation_id}")
        session = active.session

        record = _VoiceConversationRecord(
            conversation_id=active.conversation_id,
            user_id=active.user_id,
            originating_conversation_id=originating_conversation_id,
            started_at=_now_iso(),
        )
        await self._save_record(record)

        async def _on_transcript_turn(
            who: str, text: str, ts_seconds: float
        ) -> None:
            # Diagnostic: confirms STT is still committing turns after
            # a wake recovery. If "them" transcripts stop arriving
            # post-wake, this log goes silent and we know to look at
            # the STT stream / engine pump rather than the brain.
            logger.info(
                "voice-agent transcript: session=%s who=%s "
                "state=%s mode=%s responding=%s chars=%d",
                active.conversation_id,
                who,
                active.state,
                active.mode,
                active.responding,
                len(text),
            )
            record.transcript.append(
                {"who": who, "text": text, "ts": ts_seconds}
            )
            await self._save_record(record)
            # Conversational mode: the user finishing a turn marks
            # the start of "Gilbert is responding" — LLM is about
            # to think, then TTS will synthesize and play back. The
            # silence monitor skips the countdown while
            # ``responding`` is True, so long thinking time +
            # long TTS playback can't trigger dormancy mid-answer.
            # ``on_speaking_done`` flips this back to False at
            # end-of-playback. ``us`` transcripts fire when Gilbert
            # STARTS speaking, so they're inside the responding
            # window — no separate handling needed.
            if who == "them" and active.mode == "conversational":
                active.responding = True
                logger.debug(
                    "voice-agent session %s → responding=True (user turn done)",
                    active.conversation_id,
                )
            # Live-mirror the turn back to the user's voice-agent
            # browser tab. Publishing via the bus instead of holding
            # a ref to the conn keeps the service decoupled from
            # WS connection lifecycle — if the user refreshes mid-
            # conversation the new connection still gets future
            # turns via the subscription. ``user_id`` in data is what
            # the WS visibility filter uses to scope the event to
            # the right tab.
            if self._bus is not None:
                from gilbert.interfaces.events import Event

                await self._bus.publish(
                    Event(
                        event_type="voice_agent.transcript_turn",
                        data={
                            "user_id": active.user_id,
                            "session_id": active.conversation_id,
                            "who": who,
                            "text": text,
                            "ts": ts_seconds,
                        },
                        source="voice_agent",
                    )
                )

        async def _on_speaking_done() -> None:
            """Fired by the engine after each TTS playback completes.
            Marks the start of an idle "user's turn" window — the
            silence countdown begins from now, and ``responding``
            flips back to False so the monitor will actually run.
            """
            if active.mode == "conversational":
                active.responding = False
                active.last_user_activity_ts = (
                    asyncio.get_event_loop().time()
                )
                logger.debug(
                    "voice-agent session %s → responding=False "
                    "(Gilbert done speaking, silence countdown armed)",
                    active.conversation_id,
                )

        async def _on_status_change(
            status: ConversationStatus, reason: str
        ) -> None:
            log.info("voice-agent status: %s (reason=%r)", status.value, reason)

        # Engine configuration tuned for browser audio:
        # - TTS: MP3 (browser can play via HTMLAudioElement data URL).
        # - STT: 16 kHz PCM (what the SPA's mic captures cleanly).
        # - Audio input format: PCM_S16LE 16 kHz (skip ulaw decode).
        from gilbert.interfaces.transcription import (
            AudioEncoding as _STTAudioEncoding,
        )
        from gilbert.interfaces.transcription import (
            AudioFormat as _STTAudioFormat,
        )
        from gilbert.interfaces.tts import AudioFormat as _TTSAudioFormat

        # Priming message so the SPEAK_FIRST opener has something to
        # respond to. The Anthropic Messages API rejects requests with
        # ``messages=[]`` (400 "at least one message is required"), so
        # the engine's first ``_think_and_speak`` would crash on
        # session start without this. We use a synthetic user-role
        # cue rather than baking it into the system prompt so the
        # LLM treats it as "the user just signalled they want to
        # talk" rather than as an instruction.
        from gilbert.interfaces.ai import Message as _Message
        from gilbert.interfaces.ai import MessageRole as _MessageRole

        priming = [
            _Message(
                role=_MessageRole.USER,
                content=(
                    "(SYSTEM) The user just activated voice mode. Greet "
                    "them briefly — one short sentence — and let them "
                    "know you're listening."
                ),
            )
        ]

        config = ConversationConfig(
            system_prompt=str(
                self._config.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT
            ),
            brain_tool_provider=VoiceAgentBrainToolProvider(),
            opening_policy=OpeningPolicy(
                behavior=OpeningBehavior.SPEAK_FIRST,
                fallback_timeout_seconds=1.0,
            ),
            # NOTE: ``max_conversation_seconds`` is the engine's hard
            # cap — if the conversation runs longer than this, the
            # watchdog forces it to end. NOT the same as
            # ``idle_timeout_seconds`` (which would be silence-based).
            # Default to 5 min so the watchdog isn't a routine hit;
            # idle handling is a TODO.
            max_conversation_seconds=int(
                self._config.get("max_conversation_seconds", 300) or 300
            ),
            priming_messages=priming,
            on_status_change=_on_status_change,
            on_transcript_turn=_on_transcript_turn,
            on_speaking_done=_on_speaking_done,
            # Filler ("hmm, let me check…") played by the engine if
            # the LLM hasn't returned within ``filler_threshold_seconds``.
            # Without this, a knowledge-tool lookup that takes 5-10s
            # leaves the user staring at silence wondering if they
            # were heard. The engine plays one of these phrases at
            # the threshold so the user knows we're working on it.
            #
            # 3.0s is calibrated against real-world Anthropic Sonnet
            # latency: a tool-free Q&A ("is the sky blue") tends to
            # finish in 1.5-2.5s end-to-end (system-prompt processing
            # + TTFB + 30-50 tokens of generation), so 3.0s skips the
            # filler for those. Tool-using turns blow past 3s the
            # moment knowledge.search or any MCP tool is involved,
            # so they DO get a filler. The engine also suppresses
            # the filler on the opener (first turn) entirely — no
            # user is waiting for it and the cold-cache first call
            # almost always exceeds the threshold.
            filler_threshold_seconds=float(
                self._config.get("filler_threshold_seconds", 3.0)
                or 3.0
            ),
            filler_phrases=list(
                self._config.get("filler_phrases")
                or _DEFAULT_FILLER_PHRASES
            ),
            tts_output_format=_TTSAudioFormat.MP3,
            tts_output_mime="audio/mpeg",
            # Browser plays the whole MP3 in one shot from a data URL,
            # so the engine doesn't need to pace chunks at carrier
            # rate. Without this the engine's 20ms-per-160-byte loop
            # treats MP3 bytes like mulaw and stretches a 22-second
            # clip into 44 seconds of buffering time.
            tts_realtime_pacing=False,
            # Full Gilbert tool ecosystem: knowledge.search, MCP tools,
            # agent dispatch, scheduler, calendar, etc. The engine
            # uses ``AIProvider.chat()`` (multi-round, tool-aggregating)
            # instead of ``complete_one_shot`` (single-round, brain-
            # tools-only). End-of-conversation is signalled via the
            # ``end_conversation`` Gilbert tool which lives on this
            # service's own ToolProvider; the tool flips
            # ``ctx.outcome["end_requested"]`` and the engine notices
            # after each chat() round.
            use_full_ai_service=True,
            audio_input_format=_STTAudioFormat(
                encoding=_STTAudioEncoding.PCM_S16LE,
                sample_rate=16000,
                channels=1,
            ),
            stt_audio_format=_STTAudioFormat(
                encoding=_STTAudioEncoding.PCM_S16LE,
                sample_rate=16000,
                channels=1,
            ),
        )

        outcome: ConversationOutcome | None = None
        try:
            outcome = await self._voice_brain.run_conversation(session, config)
        except Exception:
            log.exception("voice_brain.run_conversation crashed")

        # Cleanup + chat-side summary.
        record.ended_at = _now_iso()
        try:
            started = datetime.fromisoformat(
                record.started_at.replace("Z", "+00:00")
            )
            ended = datetime.fromisoformat(
                record.ended_at.replace("Z", "+00:00")
            )
            record.duration_seconds = (ended - started).total_seconds()
        except Exception:
            pass
        if outcome is not None:
            record.outcome.update(outcome.outcome)
        await self._save_record(record)

        if (
            self._message_poster is not None
            and originating_conversation_id
            and outcome is not None
        ):
            summary_text = self._format_summary(record, outcome)
            try:
                await self._message_poster.append_assistant_message(
                    conversation_id=originating_conversation_id,
                    content=summary_text,
                )
            except Exception:
                log.debug("chat-summary post failed", exc_info=True)

        # Notify the SPA so it can flip its UI from "active" back
        # to "idle", tear down mic capture, and stop pumping audio.
        # Without this the SPA stays in active mode after the brain
        # ended the session — looks like the conversation is still
        # going from the user's perspective.
        if self._bus is not None:
            from gilbert.interfaces.events import Event

            await self._bus.publish(
                Event(
                    event_type="voice_agent.session_ended",
                    data={
                        "user_id": active.user_id,
                        "session_id": active.conversation_id,
                        "reason": (
                            "end_conversation"
                            if outcome and outcome.outcome.get("end_requested")
                            else "closed"
                        ),
                        "summary": str(
                            outcome.outcome.get("session_summary") or ""
                        )
                        if outcome
                        else "",
                    },
                    source="voice_agent",
                )
            )

        return active.conversation_id

    # --- Persistence + summary --------------------------------------

    async def _save_record(self, record: _VoiceConversationRecord) -> None:
        if self._storage is None:
            return
        await self._storage.backend.put(
            _COLLECTION, record.conversation_id, record.to_dict()
        )

    def _format_summary(
        self,
        record: _VoiceConversationRecord,
        outcome: ConversationOutcome,
    ) -> str:
        """Build the chat-side summary message. Conversational tone,
        no transcript dump — the full transcript lives on the record."""
        bits: list[str] = []
        bits.append(f"(voice conversation ended, {record.duration_seconds:.0f}s)")
        summary = outcome.outcome.get("session_summary")
        if summary:
            bits.append(str(summary))
        return " ".join(bits)
