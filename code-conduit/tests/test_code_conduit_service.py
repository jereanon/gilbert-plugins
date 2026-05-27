"""Tests for CodeConduitService — the conduit layer that wraps the
backend, owns alias resolution, and exposes the AI tool surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from gilbert.interfaces.coding_agent import (
    CodingAgentBackend,
    CodingAgentSendResult,
    CodingAgentSession,
    CodingConduitProvider,
)
from gilbert.interfaces.configuration import (
    ConfigActionProvider,
    Configurable,
)
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

    @property
    def available(self) -> bool:
        return self._available


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


def test_get_tools_returns_code_send_when_enabled() -> None:
    svc, _ = _service_with_stub()
    tools = svc.get_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "code_send"
    # Slash command set, with help text (rule 8 of validate-architecture).
    assert t.slash_command == "send"
    assert t.slash_group == "code"
    assert t.slash_help


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
