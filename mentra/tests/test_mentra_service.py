"""MentraService end-to-end test.

Drives the full webhook → session → AI dispatch loop with all the
external dependencies stubbed: in-memory storage with a pre-populated
mapping row, a fake AIProvider whose ``chat`` returns a canned
response, and a fake transport so we can inject a transcription
event and watch the response come back as a display_event frame.
"""

from __future__ import annotations

from typing import Any

import pytest

# ── Stubs ───────────────────────────────────────────────────────────


class _FakeBackend:
    """Minimal StorageBackend with the put/get/query subset MentraService uses."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def put(
        self, collection: str, entity_id: str, data: dict[str, Any]
    ) -> None:
        self.rows[(collection, entity_id)] = dict(data)

    async def get(
        self, collection: str, entity_id: str
    ) -> dict[str, Any] | None:
        return self.rows.get((collection, entity_id))

    async def delete(self, collection: str, entity_id: str) -> None:
        self.rows.pop((collection, entity_id), None)

    async def query(self, q: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for (col, _), row in self.rows.items():
            if col != q.collection:
                continue
            keep = True
            for f in q.filters or []:
                if str(row.get(f.field) or "") != str(f.value):
                    keep = False
                    break
            if keep:
                out.append(row)
        return out[: q.limit] if q.limit else out

    async def delete_query(self, q: Any) -> int:
        return 0


class _FakeStorage:
    def __init__(self) -> None:
        self._inner = _FakeBackend()

    @property
    def backend(self) -> _FakeBackend:
        return self._inner

    @property
    def raw_backend(self) -> _FakeBackend:
        return self._inner

    def create_namespaced(self, namespace: str) -> _FakeBackend:
        return self._inner


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, ev: Any) -> None:
        self.events.append(ev)

    def subscribe(self, *_a: Any, **_kw: Any) -> Any:
        return lambda: None


class _BusProvider:
    def __init__(self, bus: _Bus) -> None:
        self.bus = bus


class _FakeAI:
    """Stand-in for AIProvider. Captures the last call + returns a
    canned response so we can verify the dispatch."""

    def __init__(self, response_text: str = "ok") -> None:
        self.calls: list[dict[str, Any]] = []
        self._response_text = response_text

    async def chat(self, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))

        class _Result:
            response_text = self._response_text
            conversation_id = ""
            ui_blocks: list[dict[str, Any]] = []
            tool_usage: list[dict[str, Any]] = []
            attachments: list[Any] = []
            rounds: list[dict[str, Any]] = []
            interrupted = False
            model = ""
            turn_usage = None

        return _Result()


class _Resolver:
    def __init__(self, **caps: Any) -> None:
        self._caps = caps

    def get_capability(self, name: str) -> Any:
        return self._caps.get(name)

    def get_all(self, name: str) -> list[Any]:
        v = self._caps.get(name)
        return [v] if v is not None else []

    def require_capability(self, name: str) -> Any:
        v = self._caps.get(name)
        if v is None:
            raise RuntimeError(f"capability missing: {name}")
        return v


class _Cfg:
    def __init__(self, section: dict[str, Any]) -> None:
        self._section = section

    def get(self, path: str) -> Any:
        return None

    def get_section(self, name: str) -> dict[str, Any]:
        return dict(self._section) if name == "mentra" else {}

    def get_section_safe(self, name: str) -> dict[str, Any]:
        return self.get_section(name)

    async def set(self, path: str, value: Any) -> dict[str, Any]:
        return {}


class _FakeTransport:
    """In-memory transport — same shape as test_session.py's fake.
    Duplicated here to keep test files self-contained."""

    def __init__(self) -> None:
        from gilbert_plugin_mentra.session.transport import TransportState

        self._state = TransportState.CLOSED
        self.sent: list[dict[str, Any]] = []
        self._on_text: Any = None
        self._on_close: Any = None
        self._on_error: Any = None

    @property
    def ready_state(self) -> Any:
        return self._state

    @property
    def is_open(self) -> bool:
        from gilbert_plugin_mentra.session.transport import TransportState

        return self._state is TransportState.OPEN

    async def connect(self) -> None:
        from gilbert_plugin_mentra.session.transport import TransportState

        self._state = TransportState.OPEN

    async def send(self, data: str) -> None:
        import json

        self.sent.append(json.loads(data))

    async def send_binary(self, data: bytes) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        from gilbert_plugin_mentra.session.transport import TransportState

        self._state = TransportState.CLOSED

    def on_text(self, handler: Any) -> None:
        self._on_text = handler

    def on_binary(self, handler: Any) -> None:
        pass

    def on_close(self, handler: Any) -> None:
        self._on_close = handler

    def on_error(self, handler: Any) -> None:
        self._on_error = handler

    async def inject(self, frame: dict[str, Any]) -> None:
        import json

        if self._on_text is not None:
            await self._on_text(json.dumps(frame))


# ── Fixtures ────────────────────────────────────────────────────────


async def _start_service(
    *,
    enabled: bool = True,
    api_key: str = "key_test",
    package_name: str = "com.example.gilbert",
    mapping_email: str = "alice@example.com",
    mapping_user_id: str = "usr_alice",
    ai: _FakeAI | None = None,
) -> tuple[Any, _FakeStorage, _Bus, _FakeAI]:
    from gilbert_plugin_mentra.mentra_service import MentraService

    svc = MentraService()
    storage = _FakeStorage()
    bus = _Bus()
    ai = ai or _FakeAI(response_text="hello from gilbert")

    # Pre-seed the mapping row.
    if mapping_email and mapping_user_id:
        await storage.backend.put(
            "mentra_user_mappings",
            f"map_{mapping_user_id}",
            {
                "mentra_user_id": mapping_email,
                "gilbert_user_id": mapping_user_id,
                "display_name": "Alice",
                "roles": ["user"],
            },
        )

    cfg = _Cfg(
        {
            "enabled": enabled,
            "api_key": api_key,
            "package_name": package_name,
            "tts_via_cloud": True,
            "display_duration_ms": 8000,
        }
    )
    resolver = _Resolver(
        entity_storage=storage,
        ai_chat=ai,
        event_bus=_BusProvider(bus),
        configuration=cfg,
    )
    await svc.start(resolver)
    return svc, storage, bus, ai


# ── Tests ───────────────────────────────────────────────────────────


def test_service_satisfies_mentra_webhook_endpoint_protocol() -> None:
    """The MentraWebhookEndpoint Protocol must be runtime-checkable
    against the service — otherwise the webhook route would silently
    refuse to dispatch."""
    from gilbert_plugin_mentra.mentra_service import MentraService

    from gilbert.interfaces.mentra import MentraWebhookEndpoint

    svc = MentraService()
    assert isinstance(svc, MentraWebhookEndpoint)


def test_disabled_service_returns_error_on_webhook() -> None:
    """``enabled=False`` in config keeps the service inert. The
    webhook capability is still resolvable but every call returns
    an error response — same posture as the messaging plugin."""
    import asyncio

    from gilbert_plugin_mentra.mentra_service import MentraService

    async def _run() -> None:
        svc = MentraService()
        storage = _FakeStorage()
        ai = _FakeAI()
        cfg = _Cfg({"enabled": False})
        resolver = _Resolver(
            entity_storage=storage,
            ai_chat=ai,
            configuration=cfg,
        )
        await svc.start(resolver)
        result = await svc.deliver_webhook_event(
            {
                "type": "session_request",
                "sessionId": "sess_001",
                "userId": "alice@example.com",
                "timestamp": "2099-01-01T00:00:00Z",
                "websocketUrl": "wss://cloud.mentra.glass/app-ws",
            }
        )
        assert result.status == "error"
        assert "disabled" in result.message

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_unmapped_user_refused() -> None:
    """If the Mentra user_id has no mapping row, the service refuses
    rather than auto-creating a Gilbert user."""
    svc, _, _, _ = await _start_service(
        mapping_email="", mapping_user_id=""
    )
    result = await svc.deliver_webhook_event(
        {
            "type": "session_request",
            "sessionId": "sess_001",
            "userId": "unknown@example.com",
            "timestamp": "2099-01-01T00:00:00Z",
            "websocketUrl": "wss://cloud.mentra.glass/app-ws",
        }
    )
    assert result.status == "error"
    assert "mapping" in result.message.lower()


@pytest.mark.asyncio
async def test_session_request_with_missing_websocket_url_refused() -> None:
    """Cloud must supply ``websocketUrl`` (or a deprecated alias) —
    without it we have nowhere to dial back."""
    svc, _, _, _ = await _start_service()
    result = await svc.deliver_webhook_event(
        {
            "type": "session_request",
            "sessionId": "sess_001",
            "userId": "alice@example.com",
            "timestamp": "2099-01-01T00:00:00Z",
            # no websocketUrl
        }
    )
    assert result.status == "error"
    assert "websocketUrl" in result.message


@pytest.mark.asyncio
async def test_stop_request_clears_session_registry(monkeypatch: Any) -> None:
    """A ``stop_request`` for a known session must drop it from the
    registry AND publish ``mentra.session_stopped`` on the bus."""
    # Patch WebSocketTransport to use our fake so connect() succeeds
    # in-process without trying a real socket.
    svc, _, bus, _ = await _start_service()

    fake = _FakeTransport()
    from gilbert_plugin_mentra import mentra_service as ms

    monkeypatch.setattr(
        ms, "WebSocketTransport", lambda **_kwargs: fake
    )

    import asyncio

    # Kick off session_request, inject ack so connect resolves.
    session_task = asyncio.create_task(
        svc.deliver_webhook_event(
            {
                "type": "session_request",
                "sessionId": "sess_001",
                "userId": "alice@example.com",
                "timestamp": "2099-01-01T00:00:00Z",
                "websocketUrl": "wss://cloud.mentra.glass/app-ws",
            }
        )
    )
    # Yield so the session sends connection_init + starts waiting for ack.
    for _ in range(3):
        await asyncio.sleep(0)
    await fake.inject(
        {"type": "tpa_connection_ack", "sessionId": "sess_001"}
    )
    result = await session_task
    assert result.status == "success"
    # Sanity: session is registered.
    assert "sess_001" in svc._sessions  # noqa: SLF001

    # Now the stop_request.
    stop = await svc.deliver_webhook_event(
        {
            "type": "stop_request",
            "sessionId": "sess_001",
            "userId": "alice@example.com",
            "timestamp": "2099-01-01T00:00:00Z",
            "reason": "user_disabled",
        }
    )
    assert stop.status == "success"
    assert "sess_001" not in svc._sessions  # noqa: SLF001
    event_types = [getattr(e, "event_type", "") for e in bus.events]
    assert "mentra.session_stopped" in event_types


@pytest.mark.asyncio
async def test_final_transcription_dispatches_to_ai_with_mapped_user(
    monkeypatch: Any,
) -> None:
    """The headline test — final transcription on the WS triggers
    AIService.chat with the mapped Gilbert UserContext, and the
    response gets rendered to the glasses display + spoken via TTS."""
    svc, _, _, ai = await _start_service()

    fake = _FakeTransport()
    from gilbert_plugin_mentra import mentra_service as ms

    monkeypatch.setattr(ms, "WebSocketTransport", lambda **_kwargs: fake)

    import asyncio

    session_task = asyncio.create_task(
        svc.deliver_webhook_event(
            {
                "type": "session_request",
                "sessionId": "sess_001",
                "userId": "alice@example.com",
                "timestamp": "2099-01-01T00:00:00Z",
                "websocketUrl": "wss://cloud.mentra.glass/app-ws",
            }
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)
    await fake.inject(
        {"type": "tpa_connection_ack", "sessionId": "sess_001"}
    )
    await session_task

    # Inject a final transcription — should drive the AI chat.
    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "what's on my schedule?", "isFinal": True},
        }
    )
    assert len(ai.calls) == 1
    call = ai.calls[-1]
    assert call["user_message"] == "what's on my schedule?"
    ctx = call["user_ctx"]
    assert ctx.user_id == "usr_alice"
    assert ctx.email == "alice@example.com"
    assert ctx.provider == "mentra"

    # Reply was sent to the glasses — display + TTS.
    display_frames = [
        f for f in fake.sent if f.get("type") == "display_event"
    ]
    audio_frames = [
        f for f in fake.sent if f.get("type") == "audio_play_request"
    ]
    # First display frame is the "Gilbert ready" welcome from the
    # session start; the reply frame is the second one.
    assert len(display_frames) >= 2
    reply_text = display_frames[-1]["layout"]["text"]
    assert "hello from gilbert" in reply_text
    assert len(audio_frames) == 1
    assert audio_frames[-1]["text"] == "hello from gilbert"


@pytest.mark.asyncio
async def test_partial_transcription_does_not_dispatch(
    monkeypatch: Any,
) -> None:
    """Only ``isFinal=True`` transcriptions hit the AI. Partials
    are noise during the user's utterance."""
    svc, _, _, ai = await _start_service()

    fake = _FakeTransport()
    from gilbert_plugin_mentra import mentra_service as ms

    monkeypatch.setattr(ms, "WebSocketTransport", lambda **_kwargs: fake)

    import asyncio

    task = asyncio.create_task(
        svc.deliver_webhook_event(
            {
                "type": "session_request",
                "sessionId": "sess_001",
                "userId": "alice@example.com",
                "timestamp": "2099-01-01T00:00:00Z",
                "websocketUrl": "wss://cloud.mentra.glass/app-ws",
            }
        )
    )
    for _ in range(3):
        await asyncio.sleep(0)
    await fake.inject(
        {"type": "tpa_connection_ack", "sessionId": "sess_001"}
    )
    await task

    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "wha", "isFinal": False},
        }
    )
    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "what's", "isFinal": False},
        }
    )
    assert ai.calls == []


@pytest.mark.asyncio
async def test_unknown_webhook_type_returns_error() -> None:
    svc, _, _, _ = await _start_service()
    result = await svc.deliver_webhook_event(
        {"type": "asteroid_impact_warning", "sessionId": "sess_001"}
    )
    assert result.status == "error"
    assert "unknown" in result.message.lower()


def test_summarize_for_display_trims_long_replies() -> None:
    """Long AI replies get cut to roughly ``max_chars`` with an
    ellipsis so the glasses display stays readable. Sentence
    boundaries are preferred when one's close to the cut."""
    from gilbert_plugin_mentra.mentra_service import (
        _summarize_for_display,
    )

    short = "All good."
    assert _summarize_for_display(short) == "All good."

    long_text = (
        "First sentence. Second sentence. Third sentence that goes on "
        "and on with extra padding. Fourth sentence. Fifth."
    )
    out = _summarize_for_display(long_text, max_chars=80)
    assert out.endswith("…")
    assert len(out) <= 82  # max_chars + ellipsis + maybe trailing space


def test_parse_session_request_accepts_deprecated_aliases() -> None:
    """Legacy MentraOS/AugmentOS websocket-url field names must
    resolve too — the cloud still ships them for back-compat."""
    from gilbert_plugin_mentra.mentra_service import _parse_session_request

    req = _parse_session_request(
        {
            "sessionId": "sess_001",
            "userId": "alice@example.com",
            "timestamp": "2099-01-01T00:00:00Z",
            "mentraOSWebsocketUrl": "wss://cloud.mentra.glass/legacy",
        }
    )
    assert req.resolved_websocket_url == "wss://cloud.mentra.glass/legacy"
