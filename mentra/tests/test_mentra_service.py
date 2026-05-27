"""MentraService end-to-end test.

After the voice_brain refactor, MentraService is a thin transport —
it admits glasses sessions via webhook, wires the WebSocket to a
``_MentraConversationSession``, builds a ``_MentraAudioSink``, and
hands the whole thing to the ``voice_brain`` ``ConversationEngine``
capability. The engine owns the conversation loop (transcription →
AI → TTS → barge-in / echo suppression / VAD).

These tests drive the handoff itself: webhook arrives → resolver
returns the engine stub → ``run_conversation(session, config)`` is
called with the right shape. The engine's internal behaviour is
covered by ``tests/unit/test_voice_brain.py`` in core; we don't
re-verify it here.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from gilbert.interfaces.audio_blob import AudioBlob
from gilbert.interfaces.conversation import (
    ConversationConfig,
    ConversationOutcome,
    ConversationSession,
    ConversationStatus,
)

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


class _FakeVoiceBrain:
    """Captures the (session, config) tuple that would have been
    handed to the real ConversationEngine. Lets tests assert what
    the plugin built without spinning up the actual engine."""

    def __init__(self) -> None:
        self.calls: list[tuple[ConversationSession, ConversationConfig]] = []
        # Set by tests to control how long ``run_conversation``
        # blocks. Default ``None`` returns immediately with an
        # ENDED outcome (simulating "engine ran briefly, then we
        # told it to stop"). Set to an ``asyncio.Event`` to keep
        # the engine task alive until the test wants it to end.
        self.hold_until: asyncio.Event | None = None

    async def run_conversation(
        self,
        session: ConversationSession,
        config: ConversationConfig,
    ) -> ConversationOutcome:
        self.calls.append((session, config))
        if self.hold_until is not None:
            try:
                await self.hold_until.wait()
            except asyncio.CancelledError:
                # Service shutdown cancels the engine task — that's
                # an expected path; surface it as a "FAILED" outcome
                # without raising so the wrapping ``await`` returns.
                return ConversationOutcome(
                    final_status=ConversationStatus.FAILED,
                    duration_seconds=0.0,
                    outcome={},
                    failure_reason="cancelled",
                )
        return ConversationOutcome(
            final_status=ConversationStatus.ENDED,
            duration_seconds=0.0,
            outcome={},
        )


class _FakeBlobStore:
    """Captures ``register()`` calls so tests can verify the sink
    actually emitted bytes when the engine wrote audio. ``fetch``
    is never called in these tests (the route's tests cover that)."""

    def __init__(self) -> None:
        self.registered: list[tuple[bytes, str]] = []
        self._counter = 0

    def register(
        self, data: bytes, mime: str, *, ttl_seconds: float = 60.0
    ) -> str:
        self.registered.append((bytes(data), mime))
        self._counter += 1
        return f"blob_{self._counter:04d}"

    def fetch(self, blob_id: str) -> AudioBlob | None:
        return None


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
        self._on_binary: Any = None

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
        self._on_binary = handler

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
    public_base_url: str = "https://gilbert.example.com",
    mapping_email: str = "alice@example.com",
    mapping_user_id: str = "usr_alice",
    brain: _FakeVoiceBrain | None = None,
    blob_store: _FakeBlobStore | None = None,
) -> tuple[Any, _FakeStorage, _Bus, _FakeVoiceBrain, _FakeBlobStore]:
    from gilbert_plugin_mentra.mentra_service import MentraService

    svc = MentraService()
    storage = _FakeStorage()
    bus = _Bus()
    brain = brain or _FakeVoiceBrain()
    blob_store = blob_store or _FakeBlobStore()

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
            "public_base_url": public_base_url,
            "display_duration_ms": 8000,
        }
    )
    resolver = _Resolver(
        entity_storage=storage,
        voice_brain=brain,
        audio_blob_store=blob_store,
        event_bus=_BusProvider(bus),
        configuration=cfg,
    )
    await svc.start(resolver)
    return svc, storage, bus, brain, blob_store


async def _drive_session_admit(
    svc: Any, monkeypatch: Any
) -> _FakeTransport:
    """Helper: kick off a session_request, inject the connection_ack
    so connect() resolves, return the fake transport. Used by every
    test that needs a live admitted session."""
    fake = _FakeTransport()
    from gilbert_plugin_mentra import mentra_service as ms

    monkeypatch.setattr(ms, "WebSocketTransport", lambda **_kwargs: fake)

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
    result = await task
    assert result.status == "success", result.message
    return fake


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

    from gilbert_plugin_mentra.mentra_service import MentraService

    async def _run() -> None:
        svc = MentraService()
        storage = _FakeStorage()
        cfg = _Cfg({"enabled": False})
        resolver = _Resolver(
            entity_storage=storage,
            voice_brain=_FakeVoiceBrain(),
            audio_blob_store=_FakeBlobStore(),
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
    svc, *_ = await _start_service(
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
    svc, *_ = await _start_service()
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
async def test_session_request_without_voice_brain_capability_refused(
    monkeypatch: Any,
) -> None:
    """The whole point of the refactor: without ``voice_brain``
    capability resolvable, the service can't run a conversation. We
    refuse the session at admit time rather than admitting a session
    that silently does nothing."""
    from gilbert_plugin_mentra.mentra_service import MentraService

    svc = MentraService()
    storage = _FakeStorage()
    cfg = _Cfg(
        {
            "enabled": True,
            "api_key": "k",
            "package_name": "p",
            "public_base_url": "https://gilbert.example.com",
        }
    )
    resolver = _Resolver(
        entity_storage=storage,
        # NO voice_brain capability
        audio_blob_store=_FakeBlobStore(),
        configuration=cfg,
    )
    # Pre-seed mapping so the user lookup succeeds and we hit the
    # capability check.
    await storage.backend.put(
        "mentra_user_mappings",
        "map_x",
        {
            "mentra_user_id": "alice@example.com",
            "gilbert_user_id": "usr_alice",
            "display_name": "Alice",
            "roles": ["user"],
        },
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
    assert "voice_brain" in result.message


@pytest.mark.asyncio
async def test_stop_request_clears_session_registry(monkeypatch: Any) -> None:
    """A ``stop_request`` for a known session must drop it from the
    registry, cancel the engine task, AND publish
    ``mentra.session_stopped`` on the bus."""
    brain = _FakeVoiceBrain()
    # Hold the engine inside run_conversation until the test
    # explicitly releases it. Without this the engine task would
    # exit immediately and we'd race the "cancel on stop" check.
    brain.hold_until = asyncio.Event()

    svc, _, bus, _, _ = await _start_service(brain=brain)
    await _drive_session_admit(svc, monkeypatch)
    # Sanity: session is registered + engine task is running.
    assert "sess_001" in svc._sessions  # noqa: SLF001
    assert "sess_001" in svc._engine_tasks  # noqa: SLF001
    assert not svc._engine_tasks["sess_001"].done()  # noqa: SLF001

    # Stop_request.
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
    assert "sess_001" not in svc._engine_tasks  # noqa: SLF001
    event_types = [getattr(e, "event_type", "") for e in bus.events]
    assert "mentra.session_stopped" in event_types
    # Release the brain so any pending awaits unwind cleanly.
    brain.hold_until.set()


@pytest.mark.asyncio
async def test_session_admit_hands_session_to_voice_brain(
    monkeypatch: Any,
) -> None:
    """Headline test — after a successful admit, the service must
    have constructed a ConversationSession + ConversationConfig and
    handed both to ``voice_brain.run_conversation``."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)

    await _drive_session_admit(svc, monkeypatch)

    # Let the engine task get scheduled.
    for _ in range(3):
        await asyncio.sleep(0)

    assert len(brain.calls) == 1
    session, config = brain.calls[0]
    # Session shape: id matches what the webhook supplied, audio I/O
    # iterators are present, events iterator is present.
    assert session.session_id == "sess_001"
    assert session.audio_in is not None
    assert session.audio_out is not None
    assert session.events is not None
    # Config shape: full Gilbert tool ecosystem mode, MP3 output for
    # cloud-fetchable bytes, no realtime pacing (Mentra Cloud fetches
    # whole clip), internal STT disabled (Mentra Cloud handles
    # transcription server-side), inject queue wired so the synthetic
    # turn loop picks up cloud transcripts.
    from gilbert.interfaces.tts import AudioFormat as _TTSAudioFormat

    assert config.use_full_ai_service is True
    assert config.source == "mentra"
    assert config.tts_output_format == _TTSAudioFormat.MP3
    assert config.tts_realtime_pacing is False
    assert config.disable_internal_stt is True
    assert config.inject_synthetic_user_turn_queue is not None
    # Opening policy fires the welcome via the LLM rather than a
    # bespoke welcome string in the plugin.
    from gilbert.interfaces.conversation import OpeningBehavior

    assert config.opening_policy.behavior == OpeningBehavior.SPEAK_FIRST

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_session_admit_pushes_active_event_to_engine(
    monkeypatch: Any,
) -> None:
    """The plugin must push ``ConversationStatus.ACTIVE`` to the
    engine's events iterator so the SPEAK_FIRST opening policy
    fires immediately. Without that nudge the engine sits in
    PENDING forever and the user hears nothing on admit."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)

    await _drive_session_admit(svc, monkeypatch)
    for _ in range(3):
        await asyncio.sleep(0)

    session, _ = brain.calls[0]
    # Read the first event off the iterator — should be ACTIVE.
    events_iter = session.events
    ev = await asyncio.wait_for(events_iter.__anext__(), timeout=1.0)
    from gilbert.interfaces.conversation import (
        ConversationStatusEvent,
    )

    assert isinstance(ev, ConversationStatusEvent)
    assert ev.status == ConversationStatus.ACTIVE

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_cloud_transcription_finals_enqueued_for_engine(
    monkeypatch: Any,
) -> None:
    """Mentra Cloud handles STT server-side and ships finalised
    transcripts via the ``transcription`` JSON stream. The plugin
    must pull those out and push into the engine's
    ``inject_synthetic_user_turn_queue`` so the engine's synthetic
    turn loop sees them as user turns.

    Regression test for the production failure where raw PCM mic
    chunks never arrived (Mentra Cloud doesn't ship binary frames
    on Mentra Live + iOS), so the engine's internal STT pump
    starved and Gilbert silently stopped responding after the
    welcome greeting."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)
    fake = await _drive_session_admit(svc, monkeypatch)

    # Let the engine task wire up subscriptions.
    for _ in range(3):
        await asyncio.sleep(0)

    # Inject a final transcription via the cloud's data_stream path
    # (mirrors how the real cloud delivers transcription events).
    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {
                "text": "what time is it",
                "isFinal": True,
            },
        }
    )
    for _ in range(3):
        await asyncio.sleep(0)

    # The plugin should have pushed the text into the inject queue
    # that was handed to the engine via ConversationConfig.
    _, config = brain.calls[0]
    queue = config.inject_synthetic_user_turn_queue
    text = queue.get_nowait()
    assert text == "what time is it"

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_echo_during_gilbert_playback_is_dropped(
    monkeypatch: Any,
) -> None:
    """Glasses speaker bleeds into glasses mic → Mentra Cloud
    transcribes Gilbert's own voice → engine treats it as a user
    turn → infinite self-talk loop. Regression test for the live
    failure observed end-to-end on the glasses.

    Mute window opens when the engine fires ``on_llm_turn``
    (Gilbert is about to speak); transcripts arriving during the
    window are dropped at the plugin layer before they reach the
    engine's inject queue."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)
    fake = await _drive_session_admit(svc, monkeypatch)
    for _ in range(3):
        await asyncio.sleep(0)

    _, config = brain.calls[0]
    queue = config.inject_synthetic_user_turn_queue
    assert config.on_llm_turn is not None

    # Engine signals "about to speak a 100-char reply" — that arms
    # the mute window in the plugin.
    await config.on_llm_turn("hello! " * 14, [])

    # Simulate Mentra Cloud sending a transcript of Gilbert's own
    # voice while the playback is still in flight.
    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "hello hello hello", "isFinal": True},
        }
    )
    for _ in range(3):
        await asyncio.sleep(0)

    # The transcript was dropped — the engine's queue stayed empty.
    assert queue.empty()

    # Engine signals playback complete — mute clears (modulo a
    # small post-roll). After a beat, a fresh user utterance flows
    # through normally.
    assert config.on_speaking_done is not None
    await config.on_speaking_done()
    # Wait past the 0.5s post-roll buffer.
    await asyncio.sleep(0.6)

    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "actually what's on my calendar", "isFinal": True},
        }
    )
    for _ in range(3):
        await asyncio.sleep(0)

    text = queue.get_nowait()
    assert text == "actually what's on my calendar"

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_user_transcript_recorded_to_debug_ring_buffer(
    monkeypatch: Any,
) -> None:
    """The phone-side debug webview surfaces a per-user event ring
    buffer (``MentraDebugProvider.get_recent_events``). With
    ``disable_internal_stt=True`` the engine's STT loop never fires
    its own ``on_transcript_turn("them", ...)``, so the plugin's
    transcription handler is the only place a "what the user said"
    event gets logged. Verify it does so."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)
    fake = await _drive_session_admit(svc, monkeypatch)
    for _ in range(3):
        await asyncio.sleep(0)

    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "what time is it", "isFinal": True},
        }
    )
    for _ in range(3):
        await asyncio.sleep(0)

    events = svc.get_recent_events("alice@example.com", limit=50)
    final = [e for e in events if e["kind"] == "transcription_final"]
    assert len(final) == 1
    assert "what time is it" in final[0]["message"]

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_echo_suppressed_transcript_surfaced_in_debug_buffer(
    monkeypatch: Any,
) -> None:
    """Echo-suppressed transcripts must show up in the debug webview
    with a distinct ``kind`` so the user can SEE that the mic
    caught something but we ignored it because Gilbert was talking.
    Without this, an over-aggressive mute window would look like
    "Gilbert just ignored my second utterance" — frustrating to
    debug."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)
    fake = await _drive_session_admit(svc, monkeypatch)
    for _ in range(3):
        await asyncio.sleep(0)

    _, config = brain.calls[0]
    # Arm the mute window.
    await config.on_llm_turn("hello hello", [])

    # Echo arrives during the mute.
    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "hello", "isFinal": True},
        }
    )
    for _ in range(3):
        await asyncio.sleep(0)

    events = svc.get_recent_events("alice@example.com", limit=50)
    suppressed = [e for e in events if e["kind"] == "transcription_suppressed"]
    assert len(suppressed) == 1
    assert "echo suppressed" in suppressed[0]["message"].lower()
    assert "hello" in suppressed[0]["message"]

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_partial_transcription_does_not_enqueue(
    monkeypatch: Any,
) -> None:
    """Only ``isFinal=True`` transcriptions hit the engine. Partials
    would re-trigger the LLM on every keystroke and double-spend
    tokens."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)
    fake = await _drive_session_admit(svc, monkeypatch)
    for _ in range(3):
        await asyncio.sleep(0)

    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "what tim", "isFinal": False},
        }
    )
    await fake.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {"text": "what time", "isFinal": False},
        }
    )
    for _ in range(3):
        await asyncio.sleep(0)

    _, config = brain.calls[0]
    queue = config.inject_synthetic_user_turn_queue
    assert queue.empty()

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_sink_writes_register_blob_and_play_url(
    monkeypatch: Any,
) -> None:
    """When the engine writes audio bytes to ``audio_out`` and then
    flushes, the sink must register the bytes with the blob store
    and call ``speaker.play_url`` with the right URL shape."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    blob_store = _FakeBlobStore()
    svc, _, _, _, _ = await _start_service(
        brain=brain, blob_store=blob_store
    )
    fake = await _drive_session_admit(svc, monkeypatch)

    for _ in range(3):
        await asyncio.sleep(0)

    conv_session, _ = brain.calls[0]
    # Engine writes one utterance worth of bytes, then flushes.
    await conv_session.audio_out.write(b"FAKE_MP3_BYTES")
    await conv_session.audio_out.flush()

    # Blob store got the bytes.
    assert len(blob_store.registered) == 1
    assert blob_store.registered[0][0] == b"FAKE_MP3_BYTES"
    assert blob_store.registered[0][1] == "audio/mpeg"

    # Speaker manager got an audio_play_request with the right URL
    # shape (absolute, contains /api/audio-blob/, ends with the
    # blob id the store handed out).
    audio_frames = [
        f for f in fake.sent if f.get("type") == "audio_play_request"
    ]
    assert len(audio_frames) == 1
    url = audio_frames[0]["audioUrl"]
    assert url.startswith("https://gilbert.example.com/api/audio-blob/")
    assert url.endswith("/blob_0001")
    # Track 2 is the TTS track convention.
    assert audio_frames[0]["trackId"] == 2

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_sink_clear_stops_active_playback(
    monkeypatch: Any,
) -> None:
    """The engine fires ``audio_out.clear()`` on barge-in. The sink
    must drop its buffer AND tell Mentra Cloud to stop the in-flight
    playback — otherwise the user hears Gilbert finish his sentence
    after interrupting."""
    brain = _FakeVoiceBrain()
    brain.hold_until = asyncio.Event()
    svc, _, _, _, _ = await _start_service(brain=brain)
    fake = await _drive_session_admit(svc, monkeypatch)
    for _ in range(3):
        await asyncio.sleep(0)

    conv_session, _ = brain.calls[0]
    await conv_session.audio_out.clear()

    # audio_stop_request frame should have been sent to the cloud.
    stop_frames = [
        f for f in fake.sent if f.get("type") == "audio_stop_request"
    ]
    assert len(stop_frames) >= 1

    brain.hold_until.set()


@pytest.mark.asyncio
async def test_unknown_webhook_type_returns_error() -> None:
    svc, *_ = await _start_service()
    result = await svc.deliver_webhook_event(
        {"type": "asteroid_impact_warning", "sessionId": "sess_001"}
    )
    assert result.status == "error"
    assert "unknown" in result.message.lower()


def test_summarize_for_display_trims_long_replies() -> None:
    """Long AI replies get cut to roughly ``max_chars`` with an
    ellipsis so the glasses display stays readable. Sentence
    boundaries are preferred when one's close to the cut."""
    from gilbert_plugin_mentra.mentra_service import _summarize_for_display

    short = "Quick reply."
    assert _summarize_for_display(short) == "Quick reply."

    sentences = (
        "First sentence is here and stretches a bit further. "
        "Second one is also kind of long. "
        "Third sentence we never want shown."
    )
    out = _summarize_for_display(sentences, max_chars=80)
    assert len(out) <= 82  # max_chars plus the ellipsis suffix
    assert out.endswith("…")


def test_noop_brain_tool_provider_satisfies_engine_contract() -> None:
    """The engine accepts the noop provider via ``ConversationConfig``
    construction. The provider's methods aren't called in
    use_full_ai_service mode but the type has to satisfy the
    dataclass field annotation."""
    from gilbert_plugin_mentra.mentra_service import _NoopBrainToolProvider

    p = _NoopBrainToolProvider()
    assert p.get_brain_tools() == []
