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


# --- Phase 2 — SSE event stream --------------------------------------------


def test_kind_for_event_type_known_mappings() -> None:
    """Pin the OpenCode event-type → severity bucket table. A
    daemon change that renames events will break here loudly,
    rather than silently downgrading every notification to
    ``info`` (which is what the prefix fallback would do)."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    from gilbert.interfaces.coding_agent import (
        EVENT_KIND_ATTENTION,
        EVENT_KIND_DONE,
        EVENT_KIND_ERROR,
        EVENT_KIND_INFO,
    )

    assert OpenCodeBackend._kind_for_event_type("session.idle") == EVENT_KIND_DONE
    assert (
        OpenCodeBackend._kind_for_event_type("message.updated") == EVENT_KIND_DONE
    )
    assert (
        OpenCodeBackend._kind_for_event_type("session.error") == EVENT_KIND_ERROR
    )
    assert (
        OpenCodeBackend._kind_for_event_type("permission.requested")
        == EVENT_KIND_ATTENTION
    )
    # Unknown event types → info (the safe default — they don't
    # interrupt the user via TTS).
    assert (
        OpenCodeBackend._kind_for_event_type("tool.use") == EVENT_KIND_INFO
    )
    assert OpenCodeBackend._kind_for_event_type("") == EVENT_KIND_INFO


def test_kind_for_event_type_prefix_sweep_catches_new_variants() -> None:
    """Future OpenCode releases may add new error / permission
    event names. The prefix sweep keeps the kind mapping
    forward-compatible — anything with 'error' or starting with
    'permission.' buckets correctly without a SKILL update."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    from gilbert.interfaces.coding_agent import (
        EVENT_KIND_ATTENTION,
        EVENT_KIND_ERROR,
    )

    assert (
        OpenCodeBackend._kind_for_event_type("new.subsystem.error")
        == EVENT_KIND_ERROR
    )
    assert (
        OpenCodeBackend._kind_for_event_type("permission.future_variant")
        == EVENT_KIND_ATTENTION
    )


def test_sse_frame_to_event_extracts_known_fields() -> None:
    """The SSE frame parser pulls session_id, project_path, and
    summary out of the JSON payload's known field aliases."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    from gilbert.interfaces.coding_agent import EVENT_KIND_DONE

    event = OpenCodeBackend._sse_frame_to_event(
        "session.idle",
        '{"sessionId": "sess_x", "projectPath": "/p", '
        '"title": "Refactor complete", '
        '"timestamp": "2026-05-27T05:00:00Z"}',
    )
    assert event.kind == EVENT_KIND_DONE
    assert event.session_id == "sess_x"
    assert event.project_path == "/p"
    assert event.summary == "Refactor complete"
    assert event.timestamp == "2026-05-27T05:00:00Z"
    assert event.raw_type == "session.idle"


def test_sse_frame_to_event_uses_type_field_when_event_line_missing() -> None:
    """Some OpenCode versions only set the ``data:`` line with a
    ``type`` field inside the JSON. The parser falls back to that
    so we still bucket by severity correctly."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    from gilbert.interfaces.coding_agent import EVENT_KIND_ERROR

    event = OpenCodeBackend._sse_frame_to_event(
        "",
        '{"type": "session.error", "message": "model API timeout"}',
    )
    assert event.kind == EVENT_KIND_ERROR
    assert event.raw_type == "session.error"
    assert event.summary == "model API timeout"


def test_sse_frame_to_event_handles_non_json_data() -> None:
    """If a daemon emits a plain-string data line (older versions
    did), we still produce a usable event — the string lands in
    ``detail`` so the SPA feed shows something useful and the
    summary falls back to the default per-severity label."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    from gilbert.interfaces.coding_agent import EVENT_KIND_DONE

    event = OpenCodeBackend._sse_frame_to_event(
        "session.idle",
        "raw plain-text payload",
    )
    assert event.kind == EVENT_KIND_DONE
    assert event.detail == "raw plain-text payload"
    # Fallback voice-friendly label for ``done`` when no payload
    # title was readable.
    assert event.summary == "Coding agent finished."


def test_sse_frame_to_event_default_summary_per_kind() -> None:
    """When the payload has nothing readable, the per-kind default
    summaries are TTS-friendly one-liners. They're what the user
    hears on the glasses when an event fires."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    from gilbert.interfaces.coding_agent import (
        EVENT_KIND_ATTENTION,
        EVENT_KIND_DONE,
        EVENT_KIND_ERROR,
    )

    done = OpenCodeBackend._sse_frame_to_event("session.idle", "{}")
    err = OpenCodeBackend._sse_frame_to_event("session.error", "{}")
    att = OpenCodeBackend._sse_frame_to_event("permission.requested", "{}")
    assert done.kind == EVENT_KIND_DONE
    assert "finished" in done.summary.lower()
    assert err.kind == EVENT_KIND_ERROR
    assert "errored" in err.summary.lower()
    assert att.kind == EVENT_KIND_ATTENTION
    assert "waiting" in att.summary.lower()


@pytest.mark.asyncio
async def test_stream_events_parses_multiple_sse_frames() -> None:
    """Wire-format integration test: drive a fake SSE response
    through the parser and assert it produces the right sequence
    of CodingAgentEvents. Uses MockTransport so no real network
    is touched."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    from gilbert.interfaces.coding_agent import (
        EVENT_KIND_DONE,
        EVENT_KIND_ERROR,
        EVENT_KIND_INFO,
    )

    # Standard SSE format: each line ends \n, frames separated
    # by an extra blank line. httpx's aiter_lines splits on \n.
    sse_body = (
        b': keep-alive comment\n'
        b'\n'
        b'event: session.idle\n'
        b'data: {"sessionId": "s1", "title": "Done."}\n'
        b'\n'
        b'event: tool.use\n'
        b'data: {"sessionId": "s1", "title": "Calling read_file"}\n'
        b'\n'
        b'event: session.error\n'
        b'data: {"sessionId": "s1", "message": "rate limited"}\n'
        b'\n'
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        # Stream the body chunk-by-chunk to simulate a real SSE
        # connection. httpx.MockTransport accepts a bytes body
        # and feeds aiter_lines as if it were a stream.
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse_body,
        )

    backend = OpenCodeBackend()
    await backend.initialize({"server_url": "http://x.test", "server_password": "pw"})
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://x.test",
        auth=("opencode", "pw"),
        transport=httpx.MockTransport(_handler),
    )

    events: list = []
    async for ev in backend.stream_events():
        events.append(ev)
        if len(events) >= 3:
            # Don't loop forever — the mock body has exactly 3
            # frames, but the reconnect loop would try to
            # restart after the response ends.
            break

    assert [e.kind for e in events] == [
        EVENT_KIND_DONE,
        EVENT_KIND_INFO,
        EVENT_KIND_ERROR,
    ]
    assert events[0].session_id == "s1"
    assert events[0].summary == "Done."
    assert events[2].summary == "rate limited"

    await backend.close()


@pytest.mark.asyncio
async def test_stream_events_returns_silently_when_unconfigured() -> None:
    """No password → no client → stream_events returns immediately
    rather than crashing. Matches the pattern in other read methods
    so the service's pump task can spin up before the operator
    fills in Settings without exploding."""
    from gilbert_plugin_code_conduit.opencode_backend import OpenCodeBackend

    backend = OpenCodeBackend()
    await backend.initialize({"server_url": "http://x.test"})  # no pw
    events = []
    async for ev in backend.stream_events():
        events.append(ev)
    assert events == []
