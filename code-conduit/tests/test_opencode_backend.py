"""Tests for OpenCodeBackend — the HTTP client speaking ``opencode serve``.

httpx.MockTransport lets us assert exactly what requests get sent
and stub the responses, so we exercise the real client code without
needing a live ``opencode serve`` daemon.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest


@pytest.mark.asyncio
async def test_initialize_without_password_marks_unavailable() -> None:
    """The OpenCode daemon requires HTTP Basic auth — no password
    means we can't send anything. Backend reports available=False
    instead of failing later with a confusing 401, so the
    'test_connection' button and the LLM tool surface the right
    error early."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    backend = OpenCodeBackend()
    await backend.initialize({"server_url": "http://example.invalid"})
    assert backend.available is False
    await backend.close()


@pytest.mark.asyncio
async def test_send_message_creates_session_and_posts_prompt_async() -> None:
    """The happy path: no session_id supplied, so the backend
    creates one via POST /session then fires POST
    /session/{id}/prompt_async. Returns immediately with the
    minted session id — the voice loop relies on this being
    fast."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    calls: list[tuple[str, str, dict[str, Any]]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == "/session":
            return httpx.Response(200, json={"id": "sess_abc123"})
        if (
            request.method == "POST"
            and request.url.path == "/session/sess_abc123/prompt_async"
        ):
            return httpx.Response(204)
        return httpx.Response(404)

    backend = OpenCodeBackend()
    await backend.initialize(
        {"server_url": "http://opencode.test", "server_password": "pw"}
    )
    # Swap the auto-built AsyncClient for one driven by our handler.
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://opencode.test",
        auth=("opencode", "pw"),
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        message="add error handling",
        project_path="/Users/jeremy/projects/gilbert",
    )
    assert result.session_id == "sess_abc123"
    assert result.status == "sent"
    assert result.project_path == "/Users/jeremy/projects/gilbert"

    # Two HTTP calls: create-session then prompt_async.
    assert len(calls) == 2
    assert calls[0][:2] == ("POST", "/session")
    # Defensive multi-key field sends — verifies we pass whichever
    # field the daemon recognises.
    assert calls[0][2].get("projectPath") == "/Users/jeremy/projects/gilbert"
    assert calls[1][:2] == ("POST", "/session/sess_abc123/prompt_async")
    assert calls[1][2] == {"prompt": "add error handling"}

    await backend.close()


@pytest.mark.asyncio
async def test_send_message_reuses_supplied_session_id() -> None:
    """When session_id is supplied, we skip session-create and go
    straight to prompt_async. Required so the voice flow can pin
    follow-up turns to the same OpenCode session."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    calls: list[tuple[str, str]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(204)

    backend = OpenCodeBackend()
    await backend.initialize({"server_url": "http://x.test", "server_password": "pw"})
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://x.test",
        auth=("opencode", "pw"),
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        message="follow up",
        project_path="/p",
        session_id="sess_existing",
    )
    assert result.session_id == "sess_existing"
    assert calls == [("POST", "/session/sess_existing/prompt_async")]

    await backend.close()


@pytest.mark.asyncio
async def test_send_message_with_new_session_forces_create_even_when_id_supplied() -> None:
    """``new_session=True`` is the user's escape hatch: 'forget what
    we were doing, ask Claude a fresh question.' Must mint a new
    session even if a stale id is in flight."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    calls: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "sess_fresh"})
        return httpx.Response(204)

    backend = OpenCodeBackend()
    await backend.initialize({"server_url": "http://x.test", "server_password": "pw"})
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://x.test",
        auth=("opencode", "pw"),
        transport=httpx.MockTransport(_handler),
    )

    result = await backend.send_message(
        message="clean slate",
        project_path="/p",
        session_id="sess_stale",
        new_session=True,
    )
    assert result.session_id == "sess_fresh"
    # New session created, then prompt fired against the fresh id —
    # never touched the stale one.
    assert calls == [
        "POST /session",
        "POST /session/sess_fresh/prompt_async",
    ]

    await backend.close()


@pytest.mark.asyncio
async def test_send_message_raises_when_backend_unconfigured() -> None:
    """Calling send_message before initialize (or after init with no
    password) must raise a clear RuntimeError rather than crash
    deep in httpx with an unhelpful None error."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    backend = OpenCodeBackend()
    await backend.initialize({"server_url": "http://x.test"})  # no password
    with pytest.raises(RuntimeError, match="not configured"):
        await backend.send_message(message="hi", project_path="/p")
    await backend.close()


@pytest.mark.asyncio
async def test_list_sessions_tolerates_wrapped_response() -> None:
    """Some OpenCode versions return a bare list; others wrap it in
    ``{sessions: [...]}``. The parser must accept both shapes — we
    don't want to break when the daemon updates."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "sessions": [
                    {
                        "id": "sess_1",
                        "projectPath": "/p1",
                        "title": "Refactor",
                        "updatedAt": "2026-05-26T20:00:00Z",
                    },
                    {
                        "sessionId": "sess_2",  # alt id key
                        "cwd": "/p2",           # alt path key
                        "title": "Bug fix",
                        "lastUpdated": "2026-05-25T18:00:00Z",  # alt ts key
                    },
                ]
            },
        )

    backend = OpenCodeBackend()
    await backend.initialize({"server_url": "http://x.test", "server_password": "pw"})
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://x.test",
        auth=("opencode", "pw"),
        transport=httpx.MockTransport(_handler),
    )

    sessions = await backend.list_sessions()
    assert len(sessions) == 2
    assert sessions[0].session_id == "sess_1"
    assert sessions[0].project_path == "/p1"
    assert sessions[0].title == "Refactor"
    # Alt key names round-trip correctly.
    assert sessions[1].session_id == "sess_2"
    assert sessions[1].project_path == "/p2"
    assert sessions[1].last_updated == "2026-05-25T18:00:00Z"

    await backend.close()


@pytest.mark.asyncio
async def test_list_sessions_returns_empty_when_unavailable() -> None:
    """An unconfigured backend lists nothing rather than crashing —
    matches the pattern in other Gilbert backends (vision, ocr)
    where the read methods are safe to call before init."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    backend = OpenCodeBackend()
    sessions = await backend.list_sessions()
    assert sessions == []


def test_extract_session_id_handles_known_key_variants() -> None:
    """Pure-function check — the response parser knows about ``id``,
    ``sessionId``, ``session_id``, and recurses into ``data`` /
    ``session`` wrappers. Pin every accepted shape so a future
    daemon change can extend without breaking existing ones."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    assert OpenCodeBackend._extract_session_id({"id": "a"}) == "a"
    assert OpenCodeBackend._extract_session_id({"sessionId": "b"}) == "b"
    assert OpenCodeBackend._extract_session_id({"session_id": "c"}) == "c"
    assert OpenCodeBackend._extract_session_id({"data": {"id": "d"}}) == "d"
    assert OpenCodeBackend._extract_session_id({"session": {"id": "e"}}) == "e"
    # No key match returns "" rather than raising — callers check
    # for empty and raise a friendly RuntimeError themselves.
    assert OpenCodeBackend._extract_session_id({"weird": "f"}) == ""
    assert OpenCodeBackend._extract_session_id("not a dict") == ""
