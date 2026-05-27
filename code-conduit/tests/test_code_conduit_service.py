"""Tests for CodeConduitService — the conduit layer that wraps the
backend, owns alias resolution, and exposes the AI tool surface."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest

from gilbert.interfaces.coding_agent import (
    EVENT_KIND_DONE,
    EVENT_KIND_ERROR,
    EVENT_KIND_INFO,
    CodingAgentBackend,
    CodingAgentEvent,
    CodingAgentSendResult,
    CodingAgentSession,
    CodingConduitProvider,
)
from gilbert.interfaces.configuration import (
    ConfigActionProvider,
    Configurable,
)
from gilbert.interfaces.events import Event, EventBus
from gilbert.interfaces.tools import ToolProvider

# --- Test double — a backend the service can drive without HTTP ──────────


@dataclass
class _StubBackend(CodingAgentBackend):
    backend_name = "stub"

    def __init__(self) -> None:
        self._available = True
        self._sessions: list[CodingAgentSession] = []
        self._raise_on_send: Exception | None = None
        self.sent: list[dict[str, Any]] = []
        self.initialized_with: dict[str, Any] = {}
        self.closed = False
        # Queue of events the test-driven stream_events generator
        # will yield. Tests push events here and the service's
        # event pump picks them up. Setting ``_stream_done``
        # closes the iterator cleanly so the pump task terminates
        # without waiting forever.
        self._event_queue: asyncio.Queue[CodingAgentEvent | None] = asyncio.Queue()

    async def initialize(self, config: dict[str, Any]) -> None:
        self.initialized_with = dict(config)

    async def close(self) -> None:
        self.closed = True

    async def send_message(
        self,
        *,
        message: str,
        project_path: str,
        session_id: str = "",
        new_session: bool = False,
    ) -> CodingAgentSendResult:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        self.sent.append(
            {
                "message": message,
                "project_path": project_path,
                "session_id": session_id,
                "new_session": new_session,
            }
        )
        return CodingAgentSendResult(
            session_id=session_id or "sess_new",
            project_path=project_path,
            status="sent",
        )

    async def list_sessions(
        self,
        *,
        project_path: str = "",
        limit: int = 20,
    ) -> list[CodingAgentSession]:
        return list(self._sessions[:limit])

    async def stream_events(self) -> AsyncIterator[CodingAgentEvent]:
        """Yield events the test scripted via ``push_event`` /
        ``end_stream``. Returns cleanly when the sentinel ``None``
        arrives so the service's event pump terminates without
        cancellation noise in test logs."""
        while True:
            item = await self._event_queue.get()
            if item is None:
                return
            yield item

    def push_event(self, event: CodingAgentEvent) -> None:
        """Test helper: enqueue an event for the stream to yield."""
        self._event_queue.put_nowait(event)

    def end_stream(self) -> None:
        """Test helper: signal end-of-stream so the pump task exits."""
        self._event_queue.put_nowait(None)

    @property
    def available(self) -> bool:
        return self._available


class _RecordingBus(EventBus):
    """Tiny EventBus implementation that records every publish.
    Lets tests assert that ``code.notification`` events fire with
    the right severity + payload without spinning up the real bus
    service."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def subscribe(self, event_type, handler):  # type: ignore[no-untyped-def]
        # Not exercised by these tests; satisfy the ABC.
        return lambda: None

    async def publish(self, event: Event) -> None:
        self.events.append(event)

    def subscribe_pattern(self, pattern, handler):  # type: ignore[no-untyped-def]
        return lambda: None


# --- Helpers -----------------------------------------------------------------


def _service_with_stub(
    *,
    aliases: str = "",
    default_alias: str = "",
) -> tuple[Any, _StubBackend]:
    """Instantiate the service with a stub backend pre-installed.
    Bypasses ``start()`` so we don't need to plumb a ServiceResolver
    just to exercise the tool / send path."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    backend = _StubBackend()
    svc = CodeConduitService()
    svc._enabled = True
    svc._backend = backend
    svc._backend_name = "stub"
    svc._default_project_alias = default_alias
    svc._project_aliases = svc._parse_aliases(aliases)
    return svc, backend


# --- Protocol conformance ----------------------------------------------------


def test_service_implements_required_protocols() -> None:
    """Per validate-architecture rule 2b: a partial Protocol
    implementation gets silently filtered. Pin that we satisfy all
    three the conduit advertises."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    assert isinstance(svc, Configurable)
    assert isinstance(svc, ToolProvider)
    assert isinstance(svc, ConfigActionProvider)
    # The capability surface — other services discover us via this.
    assert isinstance(svc, CodingConduitProvider)


def test_service_info_advertises_correct_caps() -> None:
    """Capability strings here MUST match what consumers look up
    via resolver.get_capability — typos here silently break feature
    wiring (rule 11 of validate-architecture)."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    info = CodeConduitService().service_info()
    assert info.name == "code_conduit"
    assert "code_conduit" in info.capabilities
    assert "ai_tools" in info.capabilities
    assert info.toggleable is True


def test_slash_namespace_is_set() -> None:
    """Per std-plugins/CLAUDE.md, plugin Services with tools MUST
    pick a short, voice-friendly slash_namespace — otherwise the
    fallback is the directory name ("code-conduit") which is ugly
    and stretches the slash autocomplete UI."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    assert CodeConduitService.slash_namespace == "code"


# --- Project alias parsing --------------------------------------------------


def test_parse_aliases_ignores_blanks_and_comments() -> None:
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    raw = """
    # Project shortcuts — used by /code send
    gilbert=/Users/jeremy/projects/gilbert
    mentra =/Users/jeremy/projects/mentra

    # comment with leading whitespace too
        ignored=/path
    """
    parsed = CodeConduitService._parse_aliases(raw)
    assert parsed == {
        "gilbert": "/Users/jeremy/projects/gilbert",
        "mentra": "/Users/jeremy/projects/mentra",
        "ignored": "/path",
    }


def test_parse_aliases_skips_malformed_lines() -> None:
    """A line without '=' is operator error — log + skip rather
    than aborting the entire alias map."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    parsed = CodeConduitService._parse_aliases("ok=/a\nbroken line no equals\nother=/b")
    assert parsed == {"ok": "/a", "other": "/b"}


def test_resolve_project_uses_alias_then_default_then_passthrough() -> None:
    """Three-tier resolution: explicit alias > configured default >
    pass-through. Pass-through lets the backend handle exotic
    inputs the conduit doesn't know about."""
    svc, _ = _service_with_stub(
        aliases="gilbert=/abs/gilbert\nmentra=/abs/mentra",
        default_alias="gilbert",
    )
    # Known alias resolves.
    assert svc._resolve_project("mentra") == "/abs/mentra"
    # Empty input falls back to the default.
    assert svc._resolve_project("") == "/abs/gilbert"
    # Absolute path passes through untouched.
    assert svc._resolve_project("/elsewhere/repo") == "/elsewhere/repo"
    # Unknown alias: passes through verbatim, backend decides.
    assert svc._resolve_project("unknown") == "unknown"


# --- Tool surface -----------------------------------------------------------


@pytest.mark.asyncio
async def test_code_send_tool_relays_message_verbatim() -> None:
    """CRITICAL: Gilbert is a conduit. The tool MUST pass the
    user's text through unchanged — no paraphrasing, no
    'cleanup'. Operator-visible misbehavior would be e.g. the LLM
    rewording 'add error handling' into 'please add appropriate
    error handling to the file' and corrupting intent."""
    svc, backend = _service_with_stub(
        aliases="gilbert=/abs/gilbert", default_alias="gilbert"
    )

    result = await svc.execute_tool(
        "code_send",
        {
            "message": "add error handling to the auth flow",
            "project": "gilbert",
        },
    )

    assert len(backend.sent) == 1
    # Verbatim message AND resolved path:
    assert backend.sent[0]["message"] == "add error handling to the auth flow"
    assert backend.sent[0]["project_path"] == "/abs/gilbert"
    # Voice-friendly confirmation that names the project.
    assert "sent" in result.lower()
    assert "gilbert" in result.lower()


@pytest.mark.asyncio
async def test_code_send_tool_falls_back_to_default_project() -> None:
    """User said "tell Claude to do X" with no project name. The
    LLM omits ``project``; we apply the operator-configured
    default alias instead of erroring."""
    svc, backend = _service_with_stub(
        aliases="gilbert=/abs/gilbert", default_alias="gilbert"
    )

    await svc.execute_tool("code_send", {"message": "ship it"})

    assert backend.sent[0]["project_path"] == "/abs/gilbert"


@pytest.mark.asyncio
async def test_code_send_tool_passes_new_session_flag() -> None:
    """The 'forget what we were doing' escape hatch must reach the
    backend. Without this wiring, the user can't actually start a
    clean session via voice."""
    svc, backend = _service_with_stub(
        aliases="gilbert=/abs/gilbert", default_alias="gilbert"
    )

    await svc.execute_tool(
        "code_send",
        {"message": "fresh start", "new_session": True},
    )
    assert backend.sent[0]["new_session"] is True


@pytest.mark.asyncio
async def test_code_send_tool_empty_message_returns_friendly_error() -> None:
    """Empty message is the LLM's job to catch, but defend in
    depth — we shouldn't fire a no-op send to the coding agent
    and pretend it succeeded."""
    svc, backend = _service_with_stub()
    result = await svc.execute_tool("code_send", {"message": "   "})
    assert "actual message" in result.lower()
    assert backend.sent == []


@pytest.mark.asyncio
async def test_code_send_tool_surfaces_backend_runtime_errors() -> None:
    """If the backend says 'not configured' (or any RuntimeError),
    we surface it to the LLM as plain text so the spoken reply
    can apologize naturally rather than blowing up the voice
    turn with a traceback."""
    svc, backend = _service_with_stub()
    backend._raise_on_send = RuntimeError("not configured")

    result = await svc.execute_tool("code_send", {"message": "hi"})
    assert "couldn't reach" in result.lower() or "couldn't" in result.lower()


@pytest.mark.asyncio
async def test_code_send_tool_unknown_name_raises_keyerror() -> None:
    """Defensive — the AI service's tool dispatcher uses KeyError
    to fall through to other providers. Any other exception would
    surface to the user as a backend crash."""
    svc, _ = _service_with_stub()
    with pytest.raises(KeyError):
        await svc.execute_tool("not_a_real_tool", {})


def test_get_tools_returns_empty_when_disabled() -> None:
    """Service disabled (or backend missing) → no tools surface to
    the LLM, so it doesn't try to call something that can't
    work."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    # Default state: not enabled.
    assert svc.get_tools() == []


def test_get_tools_returns_phase1_and_phase2_tools_when_enabled() -> None:
    svc, _ = _service_with_stub()
    tools = svc.get_tools()
    by_name = {t.name: t for t in tools}
    # Phase 1 — outbound relay.
    send = by_name["code_send"]
    assert send.slash_command == "send"
    assert send.slash_group == "code"
    assert send.slash_help
    # Phase 2 — inbound activity summary.
    recent = by_name["code_recent_activity"]
    assert recent.slash_command == "recent"
    assert recent.slash_group == "code"
    assert recent.slash_help
    assert len(tools) == 2


# --- ConfigAction (Test connection button) ----------------------------------


@pytest.mark.asyncio
async def test_test_connection_action_reports_ok_when_backend_reachable() -> None:
    svc, backend = _service_with_stub()
    backend._sessions = [
        CodingAgentSession(session_id="s1"),
    ]
    result = await svc.invoke_config_action("test_connection", {})
    assert result.status == "ok"
    assert "stub" in result.message.lower()


@pytest.mark.asyncio
async def test_test_connection_reports_error_when_backend_unavailable() -> None:
    svc, backend = _service_with_stub()
    backend._available = False
    result = await svc.invoke_config_action("test_connection", {})
    assert result.status == "error"


@pytest.mark.asyncio
async def test_test_connection_reports_error_on_backend_exception() -> None:
    """Network failure mid-test must surface as an actionable
    error toast, not a raised exception."""
    svc, backend = _service_with_stub()

    async def _raise(**_: Any) -> list[CodingAgentSession]:
        raise ConnectionError("connection refused")

    backend.list_sessions = _raise  # type: ignore[method-assign]

    result = await svc.invoke_config_action("test_connection", {})
    assert result.status == "error"
    assert "connection refused" in result.message


@pytest.mark.asyncio
async def test_invoke_unknown_action_returns_error_not_raise() -> None:
    svc, _ = _service_with_stub()
    result = await svc.invoke_config_action("not_real", {})
    assert result.status == "error"


# --- Configurable surface ---------------------------------------------------


def test_config_params_includes_backend_settings_forwarded() -> None:
    """The active backend's ConfigParams MUST surface under
    ``settings.<key>`` on the parent service — same pattern as
    VisionService. Without this, the operator can't fill in the
    backend's URL/password from the Settings UI."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._backend_name = "opencode"
    keys = {p.key for p in svc.config_params()}
    assert "backend" in keys
    assert "default_project_alias" in keys
    assert "project_aliases" in keys
    assert "settings.server_url" in keys
    assert "settings.server_password" in keys


def test_config_params_marks_password_sensitive() -> None:
    """Sensitive flag preserves the masking behavior in the
    Settings UI — surface the operator-set password as `****` in
    later page loads."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._backend_name = "opencode"
    pw_param = next(
        p for p in svc.config_params() if p.key == "settings.server_password"
    )
    assert pw_param.sensitive is True


@pytest.mark.asyncio
async def test_on_config_changed_refreshes_aliases_and_default() -> None:
    """Live config edits to the alias map / default must take
    effect without a restart (per rule that restart_required=False
    fields reload via on_config_changed)."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    await svc.on_config_changed(
        {
            "default_project_alias": "gilbert",
            "project_aliases": "gilbert=/abs/g\nmentra=/abs/m",
        }
    )
    assert svc._default_project_alias == "gilbert"
    assert svc._project_aliases == {"gilbert": "/abs/g", "mentra": "/abs/m"}


# --- Phase 2 — inbound event pump --------------------------------------------
#
# These pin the contract for the "agent did a thing" channel:
# CodingAgentEvent flows from backend.stream_events() into the
# service's ring buffer AND onto the event bus as
# `code.notification`. Voice-brain / Mentra / push-notification
# subscribers consume those bus events; the ring buffer backs the
# code_recent_activity AI tool.


def _service_with_stub_and_bus(
    *,
    aliases: str = "",
    default_alias: str = "",
) -> tuple[Any, _StubBackend, _RecordingBus]:
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    backend = _StubBackend()
    bus = _RecordingBus()
    svc = CodeConduitService()
    svc._enabled = True
    svc._backend = backend
    svc._backend_name = "stub"
    svc._bus = bus
    svc._default_project_alias = default_alias
    svc._project_aliases = svc._parse_aliases(aliases)
    return svc, backend, bus


async def _wait_for_pump_to_drain(
    svc: Any,
    backend: _StubBackend,
    *,
    expected_events: int,
    timeout_s: float = 1.0,
) -> None:
    """Drive the event pump until ``expected_events`` events have
    been buffered, OR the timeout trips. Avoids ``asyncio.sleep``
    races by polling the ring buffer at a tight interval."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    while len(svc._recent_events) < expected_events:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(
                f"Pump only drained {len(svc._recent_events)} of "
                f"{expected_events} events within {timeout_s}s"
            )
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_event_pump_buffers_events_into_ring() -> None:
    """The pump consumes backend.stream_events() and appends every
    event to the ring buffer. Backs the code_recent_activity tool
    and the future /coding SPA feed."""
    svc, backend, _ = _service_with_stub_and_bus()
    pump = asyncio.create_task(svc._run_event_pump())

    backend.push_event(
        CodingAgentEvent(
            session_id="s1",
            kind=EVENT_KIND_DONE,
            summary="Finished the test suite.",
            raw_type="session.idle",
        )
    )
    backend.push_event(
        CodingAgentEvent(
            session_id="s1",
            kind=EVENT_KIND_INFO,
            summary="Calling tool: read_file",
            raw_type="tool.use",
        )
    )

    await _wait_for_pump_to_drain(svc, backend, expected_events=2)
    backend.end_stream()
    await asyncio.wait_for(pump, timeout=1.0)

    kinds = [e.kind for e in svc._recent_events]
    assert kinds == [EVENT_KIND_DONE, EVENT_KIND_INFO]


@pytest.mark.asyncio
async def test_event_pump_publishes_code_notification_bus_events() -> None:
    """Each event gets republished as a ``code.notification`` bus
    event so voice-brain / Mentra / push-notification subscribers
    can surface it. The data dict carries every field a subscriber
    needs to format without round-tripping through the service."""
    svc, backend, bus = _service_with_stub_and_bus(
        aliases="gilbert=/abs/gilbert"
    )
    pump = asyncio.create_task(svc._run_event_pump())

    backend.push_event(
        CodingAgentEvent(
            session_id="s1",
            project_path="/abs/gilbert",
            kind=EVENT_KIND_DONE,
            summary="All tests green.",
            detail="Ran 412 tests in 6s.",
            timestamp="2026-05-27T05:00:00Z",
            raw_type="session.idle",
        )
    )

    await _wait_for_pump_to_drain(svc, backend, expected_events=1)
    backend.end_stream()
    await asyncio.wait_for(pump, timeout=1.0)

    assert len(bus.events) == 1
    ev = bus.events[0]
    assert ev.event_type == "code.notification"
    assert ev.source == "code_conduit"
    data = ev.data
    assert data["kind"] == EVENT_KIND_DONE
    assert data["summary"] == "All tests green."
    assert data["session_id"] == "s1"
    assert data["project_path"] == "/abs/gilbert"
    assert data["raw_type"] == "session.idle"
    assert data["backend"] == "stub"


@pytest.mark.asyncio
async def test_event_pump_without_bus_still_buffers_events() -> None:
    """No event_bus capability available (operator hasn't enabled
    the bus, or the service is running in a minimal config) MUST
    NOT break the ring buffer — the code_recent_activity tool
    still works."""
    svc, backend, _ = _service_with_stub_and_bus()
    svc._bus = None  # simulate the missing-capability path
    pump = asyncio.create_task(svc._run_event_pump())

    backend.push_event(
        CodingAgentEvent(kind=EVENT_KIND_DONE, summary="Done.")
    )
    await _wait_for_pump_to_drain(svc, backend, expected_events=1)
    backend.end_stream()
    await asyncio.wait_for(pump, timeout=1.0)

    assert len(svc._recent_events) == 1


@pytest.mark.asyncio
async def test_event_pump_survives_bus_publish_failure() -> None:
    """A subscriber raising during publish (or the bus itself
    erroring) must NOT take down the inbound channel. Other
    notifications keep flowing; the only effect is one missed bus
    publish, which is logged but still buffered."""
    svc, backend, bus = _service_with_stub_and_bus()

    # Replace publish with a sometimes-throwing version. First call
    # raises; subsequent calls succeed.
    call_count = 0
    original_publish = bus.publish

    async def flaky_publish(event: Event) -> None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("subscriber blew up")
        await original_publish(event)

    bus.publish = flaky_publish  # type: ignore[method-assign]

    pump = asyncio.create_task(svc._run_event_pump())
    backend.push_event(CodingAgentEvent(kind=EVENT_KIND_DONE, summary="One"))
    backend.push_event(CodingAgentEvent(kind=EVENT_KIND_DONE, summary="Two"))
    await _wait_for_pump_to_drain(svc, backend, expected_events=2)
    backend.end_stream()
    await asyncio.wait_for(pump, timeout=1.0)

    # Both events buffered locally even though the first publish
    # raised — the inbound channel doesn't depend on a healthy bus.
    assert len(svc._recent_events) == 2
    assert call_count == 2


# --- Recent-activity tool ---------------------------------------------------


@pytest.mark.asyncio
async def test_code_recent_activity_tool_filters_info_by_default() -> None:
    """Default view: only the "notable" kinds (done / error /
    attention). Excludes ``info`` (tool calls / progress) so the
    TTS summary doesn't read out every tool call the agent made."""
    svc, _, _ = _service_with_stub_and_bus()
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_INFO, summary="tool A")
    )
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_DONE, summary="finished feature X")
    )
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_INFO, summary="tool B")
    )
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_ERROR, summary="test failed")
    )

    result = await svc.execute_tool("code_recent_activity", {})
    assert "tool A" not in result
    assert "tool B" not in result
    assert "finished feature X" in result
    assert "test failed" in result
    # Headline counts the bucket distribution.
    assert "1 done" in result.lower()
    assert "1 error" in result.lower()


@pytest.mark.asyncio
async def test_code_recent_activity_tool_kind_filter_includes_info_explicitly() -> None:
    """Passing kind='info' debugs the default-hidden noise. Useful
    when the user asks 'what tool calls has it been making?'."""
    svc, _, _ = _service_with_stub_and_bus()
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_INFO, summary="tool A")
    )
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_DONE, summary="done X")
    )

    result = await svc.execute_tool(
        "code_recent_activity", {"kind": "info"}
    )
    assert "tool A" in result
    assert "done X" not in result


@pytest.mark.asyncio
async def test_code_recent_activity_tool_friendly_when_empty() -> None:
    """No notable events → TTS-friendly empty message, not a raw
    empty string."""
    svc, _, _ = _service_with_stub_and_bus()
    result = await svc.execute_tool("code_recent_activity", {})
    assert "nothing notable" in result.lower()


@pytest.mark.asyncio
async def test_code_recent_activity_tool_respects_limit() -> None:
    """``limit=N`` clamps the visible slice. Used to make the
    spoken reply fit a TTS turn — five short bullets, not fifty."""
    svc, _, _ = _service_with_stub_and_bus()
    for i in range(20):
        svc._recent_events.append(
            CodingAgentEvent(kind=EVENT_KIND_DONE, summary=f"event {i}")
        )

    result = await svc.execute_tool("code_recent_activity", {"limit": 3})
    # Headline + 3 bullets = 4 lines total.
    assert result.count("\n") == 3
    assert "event 19" in result  # newest first


# --- recent_events() public accessor ----------------------------------------


def test_recent_events_returns_newest_first() -> None:
    """The public accessor (for the future SPA feed) returns
    most-recent-first, opposite of the deque's append order."""
    svc, _, _ = _service_with_stub_and_bus()
    for i in range(3):
        svc._recent_events.append(
            CodingAgentEvent(kind=EVENT_KIND_DONE, summary=f"event {i}")
        )

    out = svc.recent_events(limit=10)
    assert [e.summary for e in out] == ["event 2", "event 1", "event 0"]


def test_recent_events_kind_filter() -> None:
    svc, _, _ = _service_with_stub_and_bus()
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_DONE, summary="d1")
    )
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_ERROR, summary="e1")
    )
    out = svc.recent_events(kind=EVENT_KIND_ERROR)
    assert [e.summary for e in out] == ["e1"]


# --- Lifecycle --------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_cancels_event_pump_cleanly() -> None:
    """Service stop() must cancel the in-flight SSE consumer
    before closing the backend — otherwise the pump task races
    with backend.close() and surfaces noisy 'client closed'
    warnings."""
    svc, backend, _ = _service_with_stub_and_bus()
    # Wire a real pump task as start() would.
    svc._event_pump_task = asyncio.create_task(svc._run_event_pump())
    # Give the pump a moment to enter its first await on the queue.
    await asyncio.sleep(0.01)

    await svc.stop()

    assert svc._event_pump_task is None
    assert backend.closed is True
