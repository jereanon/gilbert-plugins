"""Session lifecycle tests with a fake Transport.

Verifies that the full chain — handshake → subscription update →
inbound data_stream → manager-level handler — fires correctly end
to end. The real WebSocketTransport is swapped out; tests drive
inbound frames via the fake's ``_inject_text`` method.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# ── Fake transport ──────────────────────────────────────────────────


class _FakeTransport:
    """In-memory Transport that captures sends and lets tests
    inject inbound frames."""

    def __init__(self) -> None:
        from gilbert_plugin_mentra.session.transport import TransportState

        self._state = TransportState.CLOSED
        self.sent: list[dict[str, Any]] = []
        self.binary_sent: list[bytes] = []
        self._on_text: Any = None
        self._on_binary: Any = None
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
        self.sent.append(json.loads(data))

    async def send_binary(self, data: bytes) -> None:
        self.binary_sent.append(data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        from gilbert_plugin_mentra.session.transport import TransportState

        self._state = TransportState.CLOSED
        if self._on_close is not None:
            await self._on_close(code, reason)

    def on_text(self, handler: Any) -> None:
        self._on_text = handler

    def on_binary(self, handler: Any) -> None:
        self._on_binary = handler

    def on_close(self, handler: Any) -> None:
        self._on_close = handler

    def on_error(self, handler: Any) -> None:
        self._on_error = handler

    async def inject(self, frame: dict[str, Any]) -> None:
        """Test helper — drive an inbound frame as if Mentra Cloud sent it."""
        if self._on_text is not None:
            await self._on_text(json.dumps(frame))


def _make_session(transport: _FakeTransport) -> Any:
    from gilbert_plugin_mentra.session.session import (
        MentraSession,
        MentraSessionConfig,
    )

    cfg = MentraSessionConfig(
        package_name="com.example.gilbert",
        api_key="key_test",
        session_id="sess_001",
        user_id="user@example.com",
        gilbert_user_id="usr_alice",
        # ``speak()`` requires an absolute base URL — Mentra Cloud
        # fetches ``<base>/api/tts?...`` server-side, so a relative
        # path would just fail. Set a plausible value so the session
        # tests exercise the full URL-building path.
        public_base_url="https://gilbert.example.com",
    )
    return MentraSession(config=cfg, transport=transport)


# ── Tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_connect_sends_connection_init_and_waits_for_ack() -> None:
    """The handshake: WS opens → app sends tpa_connection_init →
    cloud responds with tpa_connection_ack → connect() returns."""
    transport = _FakeTransport()
    session = _make_session(transport)

    # ``connect()`` would block forever waiting for the ack, so
    # schedule it as a task and inject the ack from the test body.
    connect_task = asyncio.create_task(session.connect())
    # Yield once so the connect-init frame gets sent.
    await asyncio.sleep(0)

    sent_types = [f.get("type") for f in transport.sent]
    assert "tpa_connection_init" in sent_types

    await transport.inject(
        {
            "type": "tpa_connection_ack",
            "sessionId": "sess_001",
            "settings": [],
            "mentraosSettings": {"timezone": "America/Denver"},
            "capabilities": {
                "modelName": "G1",
                "hasDisplay": True,
                "hasMicrophone": True,
                "hasSpeaker": True,
                "hasIMU": True,
                "hasButton": True,
            },
        }
    )
    await connect_task
    assert session.is_connected
    caps = session.capabilities
    assert caps is not None
    assert caps.model_name == "G1"
    assert caps.has_display is True
    assert caps.has_camera is False


@pytest.mark.asyncio
async def test_transcription_handler_receives_final_only_when_subscribed() -> None:
    """Registering an on_transcription handler must add the
    ``transcription`` stream subscription AND surface inbound
    transcription data_stream payloads as TranscriptionData."""
    transport = _FakeTransport()
    session = _make_session(transport)

    received: list[Any] = []

    async def _on_t(data: Any) -> None:
        received.append(data)

    session.transcription.on_transcription(_on_t)

    # Drive the handshake.
    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    # Subscription update should have fired with ``transcription``.
    sub_frames = [
        f for f in transport.sent if f.get("type") == "subscription_update"
    ]
    assert sub_frames, "expected a subscription_update frame"
    assert "transcription" in sub_frames[-1]["subscriptions"]

    # Inbound transcription event.
    await transport.inject(
        {
            "type": "data_stream",
            "streamType": "transcription",
            "data": {
                "text": "hello gilbert",
                "isFinal": True,
                "confidence": 0.97,
                "transcribeLanguage": "en-US",
            },
        }
    )
    assert len(received) == 1
    assert received[0].text == "hello gilbert"
    assert received[0].is_final is True
    assert received[0].confidence == pytest.approx(0.97)


@pytest.mark.asyncio
async def test_stream_router_prefix_matches_language_tagged_streams() -> None:
    """Registering for the ``transcription`` prefix matches inbound
    ``transcription:en-US`` events too. Critical for the multilingual
    case the upstream SDK supports."""
    transport = _FakeTransport()
    session = _make_session(transport)

    received: list[Any] = []

    async def _on_t(data: Any) -> None:
        received.append(data)

    session.transcription.on_transcription(_on_t)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await transport.inject(
        {
            "type": "data_stream",
            "streamType": "transcription:en-US",
            "data": {"text": "language-tagged", "isFinal": True},
        }
    )
    assert len(received) == 1
    assert received[0].text == "language-tagged"


@pytest.mark.asyncio
async def test_button_press_handler_receives_short_and_long_presses() -> None:
    transport = _FakeTransport()
    session = _make_session(transport)

    received: list[Any] = []

    async def _on_b(data: Any) -> None:
        received.append(data)

    session.button.on_button_press(_on_b)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await transport.inject(
        {
            "type": "data_stream",
            "streamType": "button_press",
            "data": {"buttonId": "side", "pressType": "short"},
        }
    )
    await transport.inject(
        {
            "type": "data_stream",
            "streamType": "button_press",
            "data": {"buttonId": "side", "pressType": "long"},
        }
    )
    assert [b.press_type for b in received] == ["short", "long"]


@pytest.mark.asyncio
async def test_display_show_text_wall_emits_display_event_frame() -> None:
    """``session.display.show_text_wall("hi")`` must produce a
    display_event frame with the right layout payload."""
    transport = _FakeTransport()
    session = _make_session(transport)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await session.display.show_text_wall("hi", duration_ms=4000)
    display_frames = [
        f for f in transport.sent if f.get("type") == "display_event"
    ]
    assert len(display_frames) == 1
    df = display_frames[-1]
    assert df["packageName"] == "com.example.gilbert"
    assert df["view"] == "main"
    assert df["durationMs"] == 4000
    assert df["layout"] == {"layoutType": "text_wall", "text": "hi"}


@pytest.mark.asyncio
async def test_dashboard_write_to_main_emits_dashboard_content_update() -> None:
    transport = _FakeTransport()
    session = _make_session(transport)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await session.dashboard.write_to_main("📅 3pm dentist")
    dash_frames = [
        f
        for f in transport.sent
        if f.get("type") == "dashboard_content_update"
    ]
    assert len(dash_frames) == 1
    df = dash_frames[-1]
    assert df["content"] == "📅 3pm dentist"
    assert df["modes"] == ["main"]


@pytest.mark.asyncio
async def test_speaker_speak_emits_audio_play_request_with_tts_url() -> None:
    """The cloud's TTS path is URL-based — ``speak()`` must build a
    ``/api/tts?text=...`` URL and ship it as ``audioUrl``, NOT pass
    text inline. Regression test for the first deploy where inline-
    text frames were accepted at the WS layer but silently dropped
    by the cloud's audio router (no audio_play_response, no audio,
    no error frame back)."""
    from urllib.parse import parse_qs, urlparse

    transport = _FakeTransport()
    session = _make_session(transport)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await session.speaker.speak("good morning")
    aud_frames = [
        f
        for f in transport.sent
        if f.get("type") == "audio_play_request"
    ]
    assert len(aud_frames) == 1
    af = aud_frames[-1]
    # No inline text — must be in the audioUrl query param.
    assert "text" not in af
    # URL must be absolute (with the configured base) so Mentra Cloud
    # has a host to fetch from — relative paths are silently dropped
    # on the cloud side. Regression test for the second audio-output
    # hunt where ``/api/tts?...`` worked from curl but speak() didn't
    # play through the glasses because the cloud got "/api/tts?..."
    # with no host.
    assert af["audioUrl"].startswith("https://gilbert.example.com/api/tts?")
    parsed = urlparse(af["audioUrl"])
    qs = parse_qs(parsed.query)
    assert qs["text"] == ["good morning"]
    # Required wire fields the cloud will silently drop the frame
    # without.
    assert af["sessionId"] == "sess_001"
    assert af["packageName"] == "com.example.gilbert"
    assert af["requestId"].startswith("audio_req_")
    assert af["volume"] == 1.0
    assert af["stopOtherAudio"] is False
    # Track 2 is the dedicated TTS track per the upstream SDK
    # convention — keeps speech from preempting music on track 0.
    # (We briefly defaulted to 0 during the audio-output hunt
    # before discovering the real culprit was a missing
    # ``/api/tts`` endpoint on the app side.)
    assert af["trackId"] == 2


@pytest.mark.asyncio
async def test_speaker_play_url_passes_required_wire_fields() -> None:
    transport = _FakeTransport()
    session = _make_session(transport)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await session.speaker.play_url(
        "https://cdn.example.com/clip.mp3",
        volume=0.5,
        stop_other_audio=True,
    )
    af = [
        f for f in transport.sent if f.get("type") == "audio_play_request"
    ][-1]
    assert af["audioUrl"] == "https://cdn.example.com/clip.mp3"
    assert af["volume"] == 0.5
    assert af["stopOtherAudio"] is True
    assert af["trackId"] == 0  # default = speaker track


@pytest.mark.asyncio
async def test_speaker_speak_skips_when_public_base_url_unset() -> None:
    """An operator who hasn't configured ``public_base_url`` would
    otherwise get silent failure — the cloud drops relative-path
    fetches. The manager must skip the call (no frame sent) and log
    a warning so the misconfiguration shows up in logs."""
    from gilbert_plugin_mentra.session.session import (
        MentraSession,
        MentraSessionConfig,
    )

    transport = _FakeTransport()
    cfg = MentraSessionConfig(
        package_name="com.example.gilbert",
        api_key="key_test",
        session_id="sess_001",
        user_id="user@example.com",
        gilbert_user_id="usr_alice",
        public_base_url="",  # explicit empty → speak() should skip
    )
    session = MentraSession(config=cfg, transport=transport)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await session.speaker.speak("nothing to see here")
    aud_frames = [
        f for f in transport.sent if f.get("type") == "audio_play_request"
    ]
    assert aud_frames == []


@pytest.mark.asyncio
async def test_speaker_speak_with_voice_settings_serializes_to_query() -> None:
    """Voice settings go in the URL as a JSON-encoded query param so
    the cloud's TTS proxy can parse them back."""
    import json
    from urllib.parse import parse_qs, urlparse

    transport = _FakeTransport()
    session = _make_session(transport)
    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await session.speaker.speak(
        "hello",
        voice_id="voice_abc",
        voice_settings={"stability": 0.6, "speed": 1.2},
    )
    af = [
        f for f in transport.sent if f.get("type") == "audio_play_request"
    ][-1]
    qs = parse_qs(urlparse(af["audioUrl"]).query)
    assert qs["voice_id"] == ["voice_abc"]
    parsed_settings = json.loads(qs["voice_settings"][0])
    assert parsed_settings == {"stability": 0.6, "speed": 1.2}


@pytest.mark.asyncio
async def test_app_stopped_event_closes_transport_and_fires_handler() -> None:
    """When the cloud sends ``app_stopped``, the session must close
    the transport and notify any ``on_stopped`` listeners — the
    plugin uses this to clean up its session registry."""
    transport = _FakeTransport()
    session = _make_session(transport)

    stopped_reasons: list[str] = []

    async def _on_stop(reason: str) -> None:
        stopped_reasons.append(reason)

    session.on_stopped(_on_stop)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject({"type": "tpa_connection_ack", "sessionId": "sess_001"})
    await connect_task

    await transport.inject(
        {"type": "app_stopped", "reason": "user_disabled"}
    )
    assert stopped_reasons == ["user_disabled"]
    assert not session.is_connected


@pytest.mark.asyncio
async def test_connection_error_unblocks_pending_connect() -> None:
    """``CONNECTION_ERROR`` (auth failure, invalid package name)
    must abort a pending ``connect()`` rather than hanging
    forever waiting for the ack that's never coming."""
    transport = _FakeTransport()
    session = _make_session(transport)

    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject(
        {
            "type": "tpa_connection_error",
            "message": "invalid api key",
            "code": "AUTH_FAILED",
        }
    )
    # connect() returns (the ack-wait event was set even though we
    # didn't actually connect) — service layer reads is_connected
    # to detect the failed state.
    await asyncio.wait_for(connect_task, timeout=1.0)
    assert session.is_connected is False
