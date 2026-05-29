"""Voice-agent service tests — skeleton-level coverage.

The service is structurally complete (resolves capabilities, registers
correctly, runs a session through the engine) but the actual mic
source is still TODO. These tests pin the SHAPE of the contract:

- The brain-tool provider implements ``BrainToolProvider``.
- ``end_conversation`` returns ``END_CONVERSATION`` so the engine
  knows to terminate.
- The session implements ``ConversationSession`` and pushes terminal
  events on ``end_session``.
"""

from __future__ import annotations

import pytest

from gilbert.interfaces.conversation import (
    BrainToolProviderRT,
    BrainToolResult,
    ConversationContext,
    ConversationStatus,
    ConversationStatusEvent,
)


def test_brain_tool_provider_satisfies_protocol() -> None:
    """``VoiceAgentBrainToolProvider`` is the kind of object the engine
    accepts. The runtime-checkable Protocol catches drift."""
    from gilbert_plugin_voice_agent.voice_agent_service import (
        VoiceAgentBrainToolProvider,
    )

    assert isinstance(VoiceAgentBrainToolProvider(), BrainToolProviderRT)


def test_brain_tool_provider_exposes_end_conversation() -> None:
    """The voice-agent's one brain tool. Without ``end_conversation``
    the engine can only exit on watchdog timeout — useless for
    interactive sessions where the user says 'thanks, bye'."""
    from gilbert_plugin_voice_agent.voice_agent_service import (
        VoiceAgentBrainToolProvider,
    )

    tools = VoiceAgentBrainToolProvider().get_brain_tools()
    by_name = {t.name: t for t in tools}
    assert "end_conversation" in by_name


@pytest.mark.asyncio
async def test_end_conversation_returns_end_signal() -> None:
    """The whole point of ``end_conversation`` is for the engine's
    dispatch loop to see ``END_CONVERSATION`` and bail. Regression
    guard against a future refactor that changes the return value."""
    from gilbert_plugin_voice_agent.voice_agent_service import (
        VoiceAgentBrainToolProvider,
    )

    provider = VoiceAgentBrainToolProvider()
    outcome: dict = {}
    turns: list[tuple[str, str]] = []

    async def _record(who: str, text: str) -> None:
        turns.append((who, text))

    async def _publish(event: str, data: dict) -> None:
        pass

    ctx = ConversationContext(
        session=None,  # type: ignore[arg-type]
        outcome=outcome,
        record_turn=_record,
        publish_event=_publish,
    )
    result = await provider.handle_brain_tool(
        "end_conversation", {"summary": "set kitchen timer for 10 min"}, ctx
    )
    assert result == BrainToolResult.END_CONVERSATION
    assert outcome["session_summary"] == "set kitchen timer for 10 min"
    assert any("set kitchen timer" in text for _, text in turns)


@pytest.mark.asyncio
async def test_session_end_pushes_terminal_event() -> None:
    """The engine drives the session via ``session.events`` — when the
    plugin calls ``end_session``, the events iterator MUST emit a
    terminal ``ConversationStatusEvent`` so the engine's status loop
    sees it and exits. Without this the engine would never wind down
    when, say, the idle-timeout fires."""
    from gilbert_plugin_voice_agent.voice_agent_service import (
        _VoiceAgentSession,
    )

    async def _empty_iter():
        return
        yield  # unreachable — makes this an async generator

    session = _VoiceAgentSession(
        session_id="vc_test",
        audio_in=_empty_iter(),
        audio_out=None,  # type: ignore[arg-type]
        events=None,  # type: ignore[arg-type]
    )
    session.events = session._events_iter()  # type: ignore[assignment]

    await session.end_session()
    # The terminal event should be sitting in the queue now.
    events = []
    async for ev in session.events:
        events.append(ev)
    assert any(
        isinstance(e, ConversationStatusEvent)
        and e.status == ConversationStatus.ENDED
        for e in events
    )


def test_parse_noise_words_accepts_comma_string() -> None:
    """The Settings UI surfaces a STRING ConfigParam for noise_words
    so users can edit a comma list. The parser must accept that form
    AND the programmatic list form (used by plugin.yaml defaults)."""
    from gilbert_plugin_voice_agent.voice_agent_service import (
        _parse_noise_words,
    )

    out = _parse_noise_words("uh, hmm, yeah")
    assert "uh" in out
    assert "hmm" in out
    assert "yeah" in out
    assert len(out) == 3


def test_parse_noise_words_handles_list() -> None:
    """Programmatic list form (plugin.yaml or test fixture)."""
    from gilbert_plugin_voice_agent.voice_agent_service import (
        _parse_noise_words,
    )

    out = _parse_noise_words(["UH", "  Hmm  ", "yeah"])
    # Normalized to lowercase + stripped of whitespace.
    assert out == frozenset({"uh", "hmm", "yeah"})


def test_parse_noise_words_empty_falls_back_to_defaults() -> None:
    """Empty string / None should NOT silently disable the gate —
    falls back to the bundled default set so a half-cleared config
    field doesn't open the floodgates."""
    from gilbert_plugin_voice_agent.voice_agent_service import (
        _DEFAULT_NOISE_WORDS,
        _parse_noise_words,
    )

    out_none = _parse_noise_words(None)
    out_empty = _parse_noise_words("")
    assert out_none == out_empty
    # Every default appears in the parsed set.
    for w in _DEFAULT_NOISE_WORDS:
        assert w.lower() in out_none
