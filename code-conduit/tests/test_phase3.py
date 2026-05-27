"""Tests for the Phase 3 surface — notification bridge, Claude
Code subprocess backend, webhook receiver, WS handlers.

These layer on top of the Phase 1/2 fixtures in
``test_code_conduit_service.py`` — duplicating the stub backend
would invite drift, but importing it is fine because pytest
collects all files in the dir as one module set.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from gilbert.interfaces.coding_agent import (
    EVENT_KIND_ATTENTION,
    EVENT_KIND_DONE,
    EVENT_KIND_ERROR,
    EVENT_KIND_INFO,
    CodingAgentEvent,
    CodingConduitInboundEndpoint,
)
from gilbert.interfaces.notifications import (
    Notification,
    NotificationProvider,
    NotificationUrgency,
)

# Reuse the stub backend / recording bus from the Phase 1/2 file.
from .test_code_conduit_service import (  # type: ignore[import-untyped]
    _RecordingBus,
    _StubBackend,
)

# --- Recording NotificationProvider ----------------------------------------


class _RecordingNotifier(NotificationProvider):
    """Test double for NotificationProvider — captures every
    notify_user call so we can assert urgency + payload + addressee
    without spinning up the real notification service / its
    storage."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call: Exception | None = None

    async def notify_user(
        self,
        *,
        user_id: str,
        message: str,
        urgency: NotificationUrgency = NotificationUrgency.NORMAL,
        source: str = "system",
        source_ref: dict[str, Any] | None = None,
    ) -> Notification:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.calls.append(
            {
                "user_id": user_id,
                "message": message,
                "urgency": urgency,
                "source": source,
                "source_ref": source_ref,
            }
        )
        # Minimal stand-in — only the service's recorder is tested
        # here, not what the notification service does with the
        # record.
        from datetime import UTC, datetime

        return Notification(
            id="notif_stub",
            user_id=user_id,
            source=source,
            message=message,
            urgency=urgency,
            created_at=datetime.now(UTC),
            source_ref=source_ref,
        )


def _service_with_notifier(
    *,
    notify_user_id: str = "u_jeremy",
    aliases: str = "",
) -> tuple[Any, _StubBackend, _RecordingNotifier, _RecordingBus]:
    """Wire a CodeConduitService with the stub backend, recording
    notifier, and recording bus all installed by hand. Bypasses
    ``start()`` to keep the fixture cheap."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    backend = _StubBackend()
    notifier = _RecordingNotifier()
    bus = _RecordingBus()
    svc = CodeConduitService()
    svc._enabled = True
    svc._backend = backend
    svc._backend_name = "stub"
    svc._bus = bus
    svc._notifier = notifier
    svc._notify_user_id = notify_user_id
    svc._project_aliases = svc._parse_aliases(aliases)
    return svc, backend, notifier, bus


# --- Notification bridge ---------------------------------------------------


@pytest.mark.asyncio
async def test_done_event_fires_normal_urgency_notification() -> None:
    """``done`` events are notable but not interrupt-grade — they
    fan out as ``NotificationUrgency.NORMAL`` so the desktop
    badge bumps but doesn't sound or flash."""
    svc, _, notifier, _ = _service_with_notifier(
        aliases="gilbert=/abs/gilbert"
    )

    await svc._ingest_event(
        CodingAgentEvent(
            kind=EVENT_KIND_DONE,
            summary="Finished the test suite.",
            project_path="/abs/gilbert",
            session_id="s1",
            raw_type="session.idle",
        )
    )

    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["user_id"] == "u_jeremy"
    assert call["urgency"] == NotificationUrgency.NORMAL
    assert call["source"] == "code_conduit"
    # Project label gets prefixed so the notification panel shows
    # which project the message came from at a glance.
    assert "[the gilbert project]" in call["message"]
    assert "Finished the test suite." in call["message"]
    # source_ref deep-links the notification back to the event.
    assert call["source_ref"] == {
        "session_id": "s1",
        "project_path": "/abs/gilbert",
        "kind": EVENT_KIND_DONE,
        "raw_type": "session.idle",
    }


@pytest.mark.asyncio
async def test_error_and_attention_events_fire_urgent_notifications() -> None:
    """``error`` and ``attention`` are interrupt-grade — they map
    to ``URGENT`` so the desktop plays a sound + flashes, and push
    providers see the higher urgency for their per-route floors."""
    svc, _, notifier, _ = _service_with_notifier()

    await svc._ingest_event(
        CodingAgentEvent(kind=EVENT_KIND_ERROR, summary="rate limited")
    )
    await svc._ingest_event(
        CodingAgentEvent(
            kind=EVENT_KIND_ATTENTION, summary="needs permission to write"
        )
    )

    assert len(notifier.calls) == 2
    assert notifier.calls[0]["urgency"] == NotificationUrgency.URGENT
    assert notifier.calls[1]["urgency"] == NotificationUrgency.URGENT


@pytest.mark.asyncio
async def test_info_event_does_not_fire_notification() -> None:
    """``info`` events (tool calls, progress) MUST NOT spam the
    notification panel. They still land in the ring buffer (so
    ``code_recent_activity`` can surface them on demand) and on
    the bus, but no notification gets created."""
    svc, _, notifier, _ = _service_with_notifier()

    await svc._ingest_event(
        CodingAgentEvent(kind=EVENT_KIND_INFO, summary="Calling read_file")
    )

    assert notifier.calls == []
    # Still buffered for code_recent_activity.
    assert len(svc._recent_events) == 1


@pytest.mark.asyncio
async def test_notification_skipped_when_no_user_pinned() -> None:
    """Empty ``notify_user_id`` is the operator's "don't bother"
    signal — bus events still publish but no notifications fire.
    Defaults this way so a fresh install doesn't spam an
    accidentally-targeted user."""
    svc, _, notifier, _ = _service_with_notifier(notify_user_id="")

    await svc._ingest_event(
        CodingAgentEvent(kind=EVENT_KIND_DONE, summary="done")
    )

    assert notifier.calls == []


@pytest.mark.asyncio
async def test_notification_survives_notifier_failure() -> None:
    """A flaky notification service must NOT take down the inbound
    pipeline. The event still buffers + publishes; the
    notification just doesn't fire."""
    svc, _, notifier, bus = _service_with_notifier()
    notifier.raise_on_call = RuntimeError("notification service crashed")

    await svc._ingest_event(
        CodingAgentEvent(kind=EVENT_KIND_DONE, summary="done")
    )

    # Notifier call attempted but raised.
    assert notifier.calls == []
    # Bus + buffer are still intact.
    assert len(bus.events) == 1
    assert len(svc._recent_events) == 1


@pytest.mark.asyncio
async def test_notification_skipped_when_notifier_capability_absent() -> None:
    """Notifications service not installed → ``self._notifier``
    stays None. Notify path is a no-op rather than an
    AttributeError."""
    svc, _, _, _ = _service_with_notifier()
    svc._notifier = None  # simulate the missing-capability path

    await svc._ingest_event(
        CodingAgentEvent(kind=EVENT_KIND_ERROR, summary="x")
    )
    # No exception, event still buffered.
    assert len(svc._recent_events) == 1


# --- Webhook authentication / verify ---------------------------------------


def test_verify_webhook_secret_rejects_empty_secret() -> None:
    """No secret configured → endpoint is OFF. ``verify`` returns
    False even when the caller sent the right thing — the web
    route turns that into a 503, not a 401."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    assert svc.webhook_enabled is False
    assert svc.verify_webhook_secret("anything") is False
    assert svc.verify_webhook_secret("") is False


def test_verify_webhook_secret_constant_time_match() -> None:
    """Verify uses ``hmac.compare_digest`` semantics — equality
    check is timing-safe. We can't directly observe the timing
    in a unit test, but we can pin the contract that exact
    matches pass and anything else fails."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._webhook_secret = "shh-it-is-a-secret"
    assert svc.webhook_enabled is True
    assert svc.verify_webhook_secret("shh-it-is-a-secret") is True
    assert svc.verify_webhook_secret("shh-it-is-a-secrex") is False
    assert svc.verify_webhook_secret("") is False
    # Mismatched length — must not crash inside compare_digest.
    assert svc.verify_webhook_secret("nope") is False


# --- deliver_inbound_event (webhook path) ---------------------------------


@pytest.mark.asyncio
async def test_deliver_inbound_event_routes_through_same_path_as_pull() -> None:
    """Push-style events (Claude Code stop hook → webhook) must
    fan out the same way as pull-style events (OpenCode SSE):
    ring buffer + bus + notification. Pin that the webhook
    pipeline doesn't accidentally skip any of those."""
    svc, _, notifier, bus = _service_with_notifier()

    event = CodingAgentEvent(
        kind=EVENT_KIND_DONE,
        summary="Refactor complete.",
        raw_type="stop_hook",
    )
    await svc.deliver_inbound_event(event=event)

    assert len(svc._recent_events) == 1
    assert len(bus.events) == 1
    assert len(notifier.calls) == 1
    # Conforms to the inbound-endpoint capability protocol so the
    # web route's isinstance check passes.
    assert isinstance(svc, CodingConduitInboundEndpoint)


@pytest.mark.asyncio
async def test_deliver_inbound_event_no_op_when_service_disabled() -> None:
    """A webhook that fires after the service got disabled
    shouldn't crash — it should silently drop the event with an
    info log. (Authentication already happened at the route layer
    before we got here, so no security implication.)"""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    # Default state: _enabled is False.
    assert svc._enabled is False

    await svc.deliver_inbound_event(
        event=CodingAgentEvent(kind=EVENT_KIND_DONE, summary="drop me")
    )
    assert len(svc._recent_events) == 0


# --- Claude Code backend --------------------------------------------------


@pytest.mark.asyncio
async def test_claude_code_unavailable_when_binary_missing() -> None:
    """No ``claude`` on PATH AND no pinned binary_path → the
    backend reports available=False so the LLM tool surface
    skips it and the test_connection action fails with a
    readable error."""
    from gilbert_plugin_code_conduit.claude_code_backend import (
        ClaudeCodeBackend,
    )

    backend = ClaudeCodeBackend()
    # Use a definitely-not-there path so PATH resolution is bypassed.
    await backend.initialize({"binary_path": "/nonexistent/claude"})
    assert backend.available is False


@pytest.mark.asyncio
async def test_claude_code_send_raises_when_binary_missing() -> None:
    """``send_message`` must raise a friendly RuntimeError when
    the binary isn't reachable — not crash deep in subprocess
    with a FileNotFoundError. The conduit service catches
    RuntimeError specifically; bare exceptions surface as
    generic 'errored' messages to the user."""
    from gilbert_plugin_code_conduit.claude_code_backend import (
        ClaudeCodeBackend,
    )

    backend = ClaudeCodeBackend()
    await backend.initialize({"binary_path": "/nonexistent/claude"})
    with pytest.raises(RuntimeError, match="binary not found"):
        await backend.send_message(message="hi", project_path="")


@pytest.mark.asyncio
async def test_claude_code_send_invokes_subprocess_with_resume_arg() -> None:
    """End-to-end of the spawn flow: we shim
    asyncio.create_subprocess_exec to capture the argv + fake a
    successful exit, then assert the backend assembled the right
    command line (binary, ``-p``, message, ``--resume <id>``,
    extra_args) and parsed the stdout output."""
    from gilbert_plugin_code_conduit import claude_code_backend as ccb_mod

    backend = ccb_mod.ClaudeCodeBackend()
    # Pin a fake binary path. ``initialize`` checks os.access for an
    # operator-pinned path; we shim shutil.which-style resolution by
    # monkeypatching ``_resolve_binary`` directly so we never touch
    # the filesystem.
    await backend.initialize(
        {
            "binary_path": "/fake/claude",
            "default_session_id": "sess_main",
            "extra_args": "--model claude-opus-4-7",
        }
    )
    backend._resolve_binary = lambda: "/fake/claude"  # type: ignore[method-assign]

    captured_argv: list[str] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"Claude's response text", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        captured_argv.extend(args)
        return _FakeProc()

    ccb_mod.asyncio.create_subprocess_exec = fake_exec  # type: ignore[attr-defined]
    try:
        result = await backend.send_message(
            message="add tests",
            project_path="/abs/gilbert",
        )
    finally:
        # Restore real subprocess factory.
        import importlib

        importlib.reload(ccb_mod)

    assert captured_argv[0] == "/fake/claude"
    assert "-p" in captured_argv
    assert "add tests" in captured_argv
    # Resume target falls back to operator-configured default
    # when send_message doesn't get an explicit session_id.
    assert "--resume" in captured_argv
    assert "sess_main" in captured_argv
    # Extra args appended after the resume flag.
    assert "--model" in captured_argv
    assert "claude-opus-4-7" in captured_argv

    assert result.status == "sent"
    # Session id echoed back is the resume target (or the
    # placeholder when neither side supplied one).
    assert result.session_id == "sess_main"


@pytest.mark.asyncio
async def test_claude_code_send_omits_resume_when_new_session_requested() -> None:
    """``new_session=True`` is the user's escape hatch: even when
    the operator pinned a default session, a fresh request must
    spawn ``claude -p`` WITHOUT ``--resume``."""
    from gilbert_plugin_code_conduit import claude_code_backend as ccb_mod

    backend = ccb_mod.ClaudeCodeBackend()
    await backend.initialize(
        {"binary_path": "/fake/claude", "default_session_id": "sess_main"}
    )
    backend._resolve_binary = lambda: "/fake/claude"  # type: ignore[method-assign]

    captured: list[str] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        captured.extend(args)
        return _FakeProc()

    ccb_mod.asyncio.create_subprocess_exec = fake_exec  # type: ignore[attr-defined]
    try:
        await backend.send_message(
            message="clean slate",
            project_path="",
            new_session=True,
        )
    finally:
        import importlib

        importlib.reload(ccb_mod)

    # NO ``--resume`` in argv even though we had a default session.
    assert "--resume" not in captured


@pytest.mark.asyncio
async def test_claude_code_send_raises_on_nonzero_exit_code() -> None:
    """If ``claude`` exits nonzero, surface the stderr to the
    operator instead of pretending the send succeeded. The
    conduit service catches the exception and surfaces it to the
    LLM as a tool error."""
    from gilbert_plugin_code_conduit import claude_code_backend as ccb_mod

    backend = ccb_mod.ClaudeCodeBackend()
    await backend.initialize({"binary_path": "/fake/claude"})
    backend._resolve_binary = lambda: "/fake/claude"  # type: ignore[method-assign]

    class _FakeProc:
        returncode = 2

        async def communicate(self) -> tuple[bytes, bytes]:
            return (b"", b"unauthorized: missing API key")

    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProc:
        return _FakeProc()

    ccb_mod.asyncio.create_subprocess_exec = fake_exec  # type: ignore[attr-defined]
    try:
        with pytest.raises(RuntimeError, match="unauthorized"):
            await backend.send_message(message="hi", project_path="")
    finally:
        import importlib

        importlib.reload(ccb_mod)


@pytest.mark.asyncio
async def test_claude_code_stream_events_returns_immediately() -> None:
    """Claude Code is webhook-driven inbound — the pull-style
    stream_events should yield nothing and return cleanly so the
    service's event pump task exits without spinning."""
    from gilbert_plugin_code_conduit.claude_code_backend import (
        ClaudeCodeBackend,
    )

    backend = ClaudeCodeBackend()
    events: list[Any] = []
    async for ev in backend.stream_events():
        events.append(ev)
    assert events == []


def test_claude_code_backend_registered_in_registry() -> None:
    """The side-effect import in plugin.py must register the
    backend with the ABC's name → class map. Without this the
    service can't find ``claude_code`` when the operator selects
    it via the ``backend`` config param."""
    # Importing the module triggers registration.
    from gilbert_plugin_code_conduit import claude_code_backend  # noqa: F401

    from gilbert.interfaces.coding_agent import CodingAgentBackend

    registered = CodingAgentBackend.registered_backends()
    assert "claude_code" in registered
    # Both backends coexist; the service picks one at runtime.
    assert "opencode" in registered


# --- WS handlers (SPA backend) -------------------------------------------


@pytest.mark.asyncio
async def test_ws_events_list_returns_dicts_newest_first() -> None:
    """The /coding page polls this RPC. Must return the buffer
    newest-first as JSON-friendly dicts (not dataclasses) so the
    SPA renders the most recent activity at the top."""
    svc, _, _, _ = _service_with_notifier()
    for i in range(3):
        svc._recent_events.append(
            CodingAgentEvent(
                kind=EVENT_KIND_DONE,
                summary=f"event {i}",
            )
        )

    res = await svc._ws_events_list(MagicMock(), {"id": "frame_1", "payload": {}})
    assert res["type"] == "code.events.list.result"
    assert res["ref"] == "frame_1"
    assert res["enabled"] is True
    summaries = [e["summary"] for e in res["events"]]
    assert summaries == ["event 2", "event 1", "event 0"]
    # Every entry is a plain dict the frontend can JSON-render.
    for ev in res["events"]:
        assert isinstance(ev, dict)
        assert "kind" in ev
        assert "summary" in ev
        assert "raw_type" in ev


@pytest.mark.asyncio
async def test_ws_events_list_respects_kind_filter() -> None:
    """Filter chips on the /coding page set the ``kind`` payload —
    pin that the backend applies it server-side rather than
    relying on client-side filtering."""
    svc, _, _, _ = _service_with_notifier()
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_DONE, summary="ok")
    )
    svc._recent_events.append(
        CodingAgentEvent(kind=EVENT_KIND_ERROR, summary="bad")
    )

    res = await svc._ws_events_list(
        MagicMock(),
        {"id": "f", "payload": {"kind": "error"}},
    )
    assert len(res["events"]) == 1
    assert res["events"][0]["summary"] == "bad"


@pytest.mark.asyncio
async def test_ws_events_list_reports_disabled_state() -> None:
    """Operator hasn't enabled the service yet → the page renders
    a banner pointing them at Settings. Pin that the flag flows
    through the RPC."""
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    # Don't call _service_with_notifier — leave _enabled at default False.
    res = await svc._ws_events_list(MagicMock(), {"id": "f", "payload": {}})
    assert res["enabled"] is False
    assert res["events"] == []


@pytest.mark.asyncio
async def test_ws_send_relays_through_send_message() -> None:
    """The compose form's submit fires this RPC. Same parameter
    shape as the AI tool / slash command so all three paths
    hit the conduit identically."""
    svc, backend, _, _ = _service_with_notifier(
        aliases="gilbert=/abs/gilbert"
    )

    res = await svc._ws_send(
        MagicMock(),
        {
            "id": "f",
            "payload": {
                "message": "add tests",
                "project": "gilbert",
                "new_session": False,
            },
        },
    )
    assert res["type"] == "code.send.result"
    assert res["ok"] is True
    assert res["backend"] == "stub"
    # Backend got the alias-resolved path.
    assert backend.sent[0]["project_path"] == "/abs/gilbert"
    assert backend.sent[0]["message"] == "add tests"


@pytest.mark.asyncio
async def test_ws_send_returns_ok_false_on_empty_message() -> None:
    """Defensive: an empty submit shouldn't fire a no-op send.
    Return ok=False with an error string so the SPA renders an
    inline validation message rather than the success toast."""
    svc, _, _, _ = _service_with_notifier()
    res = await svc._ws_send(
        MagicMock(),
        {"id": "f", "payload": {"message": "   "}},
    )
    assert res["ok"] is False
    assert "required" in (res.get("error") or "").lower()


@pytest.mark.asyncio
async def test_ws_send_surfaces_backend_errors_as_ok_false() -> None:
    """A backend send failure (binary missing, network blip,
    daemon down) becomes ok=False + the error message. SPA
    renders the message in a red box; the AI tool's separate
    path translates the same RuntimeError into a friendly
    spoken apology."""
    svc, backend, _, _ = _service_with_notifier()
    backend._raise_on_send = RuntimeError("daemon unreachable")

    res = await svc._ws_send(
        MagicMock(),
        {"id": "f", "payload": {"message": "hi"}},
    )
    assert res["ok"] is False
    assert "daemon unreachable" in (res.get("error") or "")


# --- Webhook route (FastAPI integration) ---------------------------------
#
# These cover the core/web/routes/code_conduit_webhook.py route. We
# mount only that router on a fresh FastAPI app so we don't drag in
# the whole Gilbert lifespan + auth middleware just to test the
# 503/401/200 paths.


def _make_webhook_app(endpoint_svc: Any | None) -> Any:
    """Build a minimal FastAPI app that exposes the webhook router
    + a fake ``app.state.gilbert`` whose ``service_manager.get_capability``
    returns the supplied endpoint (or None to simulate "plugin not
    loaded")."""
    from fastapi import FastAPI

    from gilbert.web.routes.code_conduit_webhook import router

    app = FastAPI()
    app.include_router(router)

    class _FakeServiceManager:
        def get_capability(self, name: str) -> Any:
            assert name == "code_conduit"
            return endpoint_svc

    class _FakeGilbert:
        service_manager = _FakeServiceManager()

    app.state.gilbert = _FakeGilbert()
    return app


def test_webhook_returns_503_when_plugin_not_loaded() -> None:
    """No code-conduit service registered → 503. Distinct from
    401 because the credential check never had a chance to run."""
    from fastapi.testclient import TestClient

    app = _make_webhook_app(endpoint_svc=None)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={"X-Code-Conduit-Secret": "anything"},
        json={"kind": "done", "summary": "x"},
    )
    assert resp.status_code == 503


def test_webhook_returns_503_when_secret_not_configured() -> None:
    """Service is loaded but the operator hasn't set
    ``webhook_secret`` → endpoint is OFF. 503 again so the
    caller doesn't probe-and-guess secrets against a still-
    provisioning install."""
    from fastapi.testclient import TestClient
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    # _webhook_secret defaults to "".
    app = _make_webhook_app(endpoint_svc=svc)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={"X-Code-Conduit-Secret": "anything"},
        json={"kind": "done", "summary": "x"},
    )
    assert resp.status_code == 503


def test_webhook_returns_401_on_bad_secret() -> None:
    """Service + secret configured, but the caller sent the wrong
    one → 401. Don't echo what they sent in the response body."""
    from fastapi.testclient import TestClient
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._webhook_secret = "the-real-secret"
    app = _make_webhook_app(endpoint_svc=svc)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={"X-Code-Conduit-Secret": "wrong"},
        json={"kind": "done", "summary": "x"},
    )
    assert resp.status_code == 401
    assert "wrong" not in resp.text


def test_webhook_returns_400_on_non_json_body() -> None:
    from fastapi.testclient import TestClient
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._webhook_secret = "the-real-secret"
    app = _make_webhook_app(endpoint_svc=svc)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={
            "X-Code-Conduit-Secret": "the-real-secret",
            "Content-Type": "application/json",
        },
        content=b"not-json",
    )
    assert resp.status_code == 400


def test_webhook_returns_400_on_json_non_object() -> None:
    from fastapi.testclient import TestClient
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._webhook_secret = "the-real-secret"
    app = _make_webhook_app(endpoint_svc=svc)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={"X-Code-Conduit-Secret": "the-real-secret"},
        json=["not", "an", "object"],
    )
    assert resp.status_code == 400


def test_webhook_accepts_valid_payload_and_ingests() -> None:
    """Happy path: valid secret + JSON object → 200, event lands
    in the service's ring buffer + (when wired) fires through
    the bus + notification fan-out."""
    from fastapi.testclient import TestClient
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._enabled = True  # otherwise deliver_inbound_event is a no-op
    svc._webhook_secret = "the-real-secret"

    app = _make_webhook_app(endpoint_svc=svc)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={"X-Code-Conduit-Secret": "the-real-secret"},
        json={
            "kind": "done",
            "summary": "Claude finished writing tests.",
            "session_id": "sess_99",
            "project_path": "/abs/gilbert",
            "raw_type": "stop_hook",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["kind"] == "done"
    assert body["raw_type"] == "stop_hook"

    # Event landed in the ring buffer with the right shape.
    assert len(svc._recent_events) == 1
    event = svc._recent_events[0]
    assert event.kind == EVENT_KIND_DONE
    assert event.summary == "Claude finished writing tests."
    assert event.session_id == "sess_99"
    assert event.raw_type == "stop_hook"


def test_webhook_coerces_unknown_kind_to_info() -> None:
    """A caller sending ``kind="weird"`` must not be rejected —
    we coerce to info so the event lands in the activity feed
    with the original type preserved in ``raw_type``. Beats
    silently dropping a stop-hook payload from a daemon version
    that introduced a new severity name."""
    from fastapi.testclient import TestClient
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._enabled = True
    svc._webhook_secret = "x"
    app = _make_webhook_app(endpoint_svc=svc)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={"X-Code-Conduit-Secret": "x"},
        json={"kind": "ultraviolet", "summary": "?", "raw_type": "new_thing"},
    )
    assert resp.status_code == 200
    assert resp.json()["kind"] == "info"
    assert svc._recent_events[0].kind == EVENT_KIND_INFO
    # raw_type stays so the operator can grep the feed for what
    # actually fired.
    assert svc._recent_events[0].raw_type == "new_thing"


def test_webhook_defaults_missing_fields() -> None:
    """Minimum legal payload is just ``{}`` (with the right
    secret). Defaults to ``kind=info``, summary/detail empty,
    ``raw_type=webhook``. Pin that the parser doesn't require
    any fields — call-site is third-party scripts, we want them
    forgiving."""
    from fastapi.testclient import TestClient
    from gilbert_plugin_code_conduit.code_conduit_service import (
        CodeConduitService,
    )

    svc = CodeConduitService()
    svc._enabled = True
    svc._webhook_secret = "x"
    app = _make_webhook_app(endpoint_svc=svc)
    client = TestClient(app)
    resp = client.post(
        "/api/code-conduit/inbound",
        headers={"X-Code-Conduit-Secret": "x"},
        json={},
    )
    assert resp.status_code == 200
    event = svc._recent_events[0]
    assert event.kind == EVENT_KIND_INFO
    assert event.raw_type == "webhook"
