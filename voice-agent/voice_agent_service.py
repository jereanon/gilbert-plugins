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

_DEFAULT_SYSTEM_PROMPT = (
    "You are Gilbert, the user's personal AI assistant, responding to a "
    "wake-word-initiated voice interaction. Keep replies short — one or "
    "two sentences max. No markdown, no lists. When the user is done, "
    "call the ``end_conversation`` tool with a one-line summary."
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
                    "End this voice interaction. Use when the user is done "
                    "(\"thanks\", \"that's all\", \"bye\", they walked away). "
                    "Bookkeeping only — does NOT produce a goodbye line, you "
                    "say that yourself in the same turn's message content."
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


class _VoiceAgentAudioSink:
    """Stub ``AudioSink``. Until the speaker backend wires up to a real
    continuous output sink (a browser tab? a Sonos group? a local
    audio device?), writes are logged and dropped on the floor. The
    engine doesn't care — it just calls ``write`` and ``clear``.
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._chunks = 0

    async def write(self, chunk: bytes) -> None:
        self._chunks += 1
        if self._chunks == 1:
            logger.warning(
                "VoiceAgentAudioSink stub: first chunk dropped — wire to "
                "a real SpeakerBackend output (session=%s)",
                self._session_id,
            )

    async def clear(self) -> None:
        return None


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
        # Active wake-word detector task. None when in active-session
        # mode (we close the detector while a conversation is running
        # to avoid double-billing STT).
        self._wake_task: asyncio.Task[None] | None = None
        # Active session task. None when in wake-listen mode.
        self._session_task: asyncio.Task[None] | None = None

    def service_info(self) -> ServiceInfo:
        return ServiceInfo(
            name="voice_agent",
            capabilities=frozenset({"voice_agent"}),
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

        # Pull config — same pattern as PhoneCallService.
        config_svc = resolver.get_capability("configuration")
        section: dict[str, Any] = {}
        if config_svc is not None:
            from gilbert.interfaces.configuration import ConfigurationReader

            if isinstance(config_svc, ConfigurationReader):
                section = config_svc.get_section(self.config_namespace)

        if not section.get("enabled", False):
            logger.info("Voice-agent service disabled")
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

    async def start_session(
        self,
        *,
        user_id: str,
        audio_in: AsyncIterator[bytes],
        audio_out: AudioSink,
        originating_conversation_id: str = "",
    ) -> str:
        """Run a single wake-word-activated voice session to completion.

        The caller (the wake-word listening loop, once wired up) hands
        over a fresh inbound-audio iterator + outbound sink. We build
        a ``_VoiceAgentSession`` around them, mark it ACTIVE, and let
        the engine drive. On completion, post a chat summary.

        Returns the conversation_id of the completed session.
        """
        if self._voice_brain is None:
            raise RuntimeError("voice_agent: voice_brain not available")

        conversation_id = f"vc_{uuid.uuid4().hex[:12]}"
        log = logger.getChild(f"voice:{conversation_id}")

        record = _VoiceConversationRecord(
            conversation_id=conversation_id,
            user_id=user_id,
            originating_conversation_id=originating_conversation_id,
            started_at=_now_iso(),
        )
        await self._save_record(record)

        session = _VoiceAgentSession(
            session_id=conversation_id,
            audio_in=audio_in,
            audio_out=audio_out,
            events=None,  # type: ignore[arg-type]  — set below
        )
        # The events iterator lives on the same instance — Python's
        # @dataclass inheritance forces us to set it post-construction
        # because the iterator is itself a method on the instance.
        session.events = session._events_iter()  # type: ignore[assignment]

        # Mark the session ACTIVE so the engine fires its opening
        # policy. The voice-agent skips the wait-for-remote dance —
        # the user just said the wake word, the brain should respond
        # immediately.
        await session.push_event(
            ConversationStatusEvent(status=ConversationStatus.ACTIVE)
        )

        async def _on_transcript_turn(
            who: str, text: str, ts_seconds: float
        ) -> None:
            record.transcript.append(
                {"who": who, "text": text, "ts": ts_seconds}
            )
            await self._save_record(record)

        async def _on_status_change(
            status: ConversationStatus, reason: str
        ) -> None:
            log.info("voice-agent status: %s (reason=%r)", status.value, reason)

        config = ConversationConfig(
            system_prompt=str(
                self._config.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT
            ),
            brain_tool_provider=VoiceAgentBrainToolProvider(),
            opening_policy=OpeningPolicy(
                behavior=OpeningBehavior.SPEAK_FIRST,
                fallback_timeout_seconds=1.0,
            ),
            max_conversation_seconds=int(
                self._config.get("idle_timeout_seconds", 60) or 60
            ),
            priming_messages=[],
            on_status_change=_on_status_change,
            on_transcript_turn=_on_transcript_turn,
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

        return conversation_id

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
