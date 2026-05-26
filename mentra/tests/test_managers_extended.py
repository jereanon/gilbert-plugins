"""Tests for the v1.1 managers (LED, location, mic, camera) + the
binary-frame routing path + reconnect-handling.

Each test drives the manager via a fake transport (re-use the shape
from ``test_session.py``) and asserts on the captured outbound
frames + the dataclasses surfaced to handlers.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

# ── Shared fake transport ──────────────────────────────────────────


class _FakeTransport:
    """Same shape as the fake in ``test_session.py`` — lifted into a
    separate file so this suite doesn't depend on the other file
    being imported."""

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

    async def inject_text(self, frame: dict[str, Any]) -> None:
        if self._on_text is not None:
            await self._on_text(json.dumps(frame))

    async def inject_binary(self, data: bytes) -> None:
        if self._on_binary is not None:
            await self._on_binary(data)


async def _make_connected_session() -> tuple[Any, _FakeTransport]:
    """Build a session + fake transport, run the handshake, return."""
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
    )
    transport = _FakeTransport()
    session = MentraSession(config=cfg, transport=transport)
    connect_task = asyncio.create_task(session.connect())
    await asyncio.sleep(0)
    await transport.inject_text(
        {
            "type": "tpa_connection_ack",
            "sessionId": "sess_001",
            "capabilities": {
                "hasCamera": True,
                "hasDisplay": True,
                "hasMicrophone": True,
                "hasLight": True,
            },
        }
    )
    await connect_task
    return session, transport


# ── LedManager ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_led_set_color_emits_rgb_led_control_with_defaults() -> None:
    session, transport = await _make_connected_session()
    await session.led.set_color("red")
    frames = [f for f in transport.sent if f.get("type") == "rgb_led_control"]
    assert len(frames) == 1
    frame = frames[-1]
    assert frame["action"] == "on"
    assert frame["color"] == "red"
    assert frame["ontime"] == 1000
    assert frame["offtime"] == 0
    assert frame["count"] == 1
    assert frame["packageName"] == "com.example.gilbert"
    assert frame["sessionId"] == "sess_001"
    assert frame["requestId"].startswith("led_req_")


@pytest.mark.asyncio
async def test_led_blink_emits_pattern_with_counts() -> None:
    from gilbert_plugin_mentra.session.managers.led import LedColor

    session, transport = await _make_connected_session()
    await session.led.blink(LedColor.GREEN, on_time_ms=300, off_time_ms=200, count=4)
    frame = [f for f in transport.sent if f.get("type") == "rgb_led_control"][-1]
    assert frame["color"] == "green"
    assert frame["ontime"] == 300
    assert frame["offtime"] == 200
    assert frame["count"] == 4


@pytest.mark.asyncio
async def test_led_turn_off_emits_action_off() -> None:
    session, transport = await _make_connected_session()
    await session.led.turn_off()
    frame = [f for f in transport.sent if f.get("type") == "rgb_led_control"][-1]
    assert frame["action"] == "off"
    # Color / timing fields should NOT be set on an off command —
    # the cloud rejects mixed-mode frames.
    assert "color" not in frame
    assert "ontime" not in frame


# ── LocationManager ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_location_on_update_subscribes_and_fires() -> None:
    session, transport = await _make_connected_session()
    received: list[Any] = []

    async def _on_loc(data: Any) -> None:
        received.append(data)

    session.location.on_location_update(_on_loc)
    # Subscription frames go out via a fire-and-forget task — yield
    # so it lands on the wire before we inspect.
    await asyncio.sleep(0)
    sub = [f for f in transport.sent if f.get("type") == "subscription_update"][-1]
    assert "location_stream" in sub["subscriptions"]

    await transport.inject_text(
        {
            "type": "data_stream",
            "streamType": "location_stream",
            "data": {"lat": 39.74, "lng": -104.99, "accuracy": 12.5},
        }
    )
    assert len(received) == 1
    assert received[0].lat == pytest.approx(39.74)
    assert received[0].lng == pytest.approx(-104.99)
    assert received[0].accuracy == pytest.approx(12.5)
    # Cache populated.
    assert session.location.lat == pytest.approx(39.74)
    assert session.location.accuracy_m == pytest.approx(12.5)


@pytest.mark.asyncio
async def test_location_request_update_resolves_via_correlation_id() -> None:
    """One-shot poll: send a LOCATION_POLL_REQUEST with a correlation
    id; the cloud responds via a ``location_update`` data stream
    matching that id, and our Future resolves."""
    session, transport = await _make_connected_session()

    async def _wait_and_inject() -> None:
        # Wait for the poll request to land on the wire.
        for _ in range(20):
            polls = [
                f
                for f in transport.sent
                if f.get("type") == "location_poll_request"
            ]
            if polls:
                cid = polls[-1]["correlationId"]
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("location_poll_request never sent")
        await transport.inject_text(
            {
                "type": "data_stream",
                "streamType": "location_update",
                "data": {
                    "lat": 37.77,
                    "lng": -122.42,
                    "accuracy": 8.0,
                    "correlationId": cid,
                },
            }
        )

    inject_task = asyncio.create_task(_wait_and_inject())
    result = await session.location.request_update(timeout=2.0)
    await inject_task
    assert result.lat == pytest.approx(37.77)
    assert result.correlation_id != ""


@pytest.mark.asyncio
async def test_location_request_update_times_out() -> None:
    """Cloud never responds → caller sees ``asyncio.TimeoutError``
    instead of hanging forever."""
    session, _transport = await _make_connected_session()
    with pytest.raises(asyncio.TimeoutError):
        await session.location.request_update(timeout=0.1)


# ── MicManager — binary audio + VAD ────────────────────────────────


@pytest.mark.asyncio
async def test_mic_audio_chunk_handler_receives_binary_frames() -> None:
    """The session's binary-frame path forwards bytes to MicManager,
    which wraps them in ``AudioChunk`` and dispatches to subscribers."""
    session, transport = await _make_connected_session()
    received: list[Any] = []

    async def _on_chunk(chunk: Any) -> None:
        received.append(chunk)

    session.mic.on_audio_chunk(_on_chunk)
    await asyncio.sleep(0)  # let the fire-and-forget sub task run
    sub = [f for f in transport.sent if f.get("type") == "subscription_update"][-1]
    assert "audio_chunk" in sub["subscriptions"]

    # Drive a fake binary frame.
    pcm = bytes(range(64))  # 32 samples of fake PCM
    await transport.inject_binary(pcm)
    assert len(received) == 1
    assert received[0].data == pcm
    assert received[0].sample_rate == 16000
    assert received[0].channels == 1


@pytest.mark.asyncio
async def test_mic_binary_skipped_when_no_subscriber() -> None:
    """No active ``on_audio_chunk`` handler → binary frames silently
    dropped. We're paranoid about not adding overhead on devices
    that don't use audio."""
    session, transport = await _make_connected_session()
    # No subscriber registered.
    await transport.inject_binary(bytes(100))
    # No crash, no extra outbound frame.
    audio_subs = [
        f
        for f in transport.sent
        if f.get("type") == "subscription_update"
        and "audio_chunk" in f.get("subscriptions", [])
    ]
    assert audio_subs == []


@pytest.mark.asyncio
async def test_mic_vad_event_normalizes_string_status() -> None:
    """The cloud sometimes ships ``status`` as a JSON boolean and
    sometimes as the string ``"true"`` / ``"false"`` — both shapes
    have to normalize to the same boolean."""
    session, transport = await _make_connected_session()
    received: list[Any] = []

    async def _on_vad(ev: Any) -> None:
        received.append(ev)

    session.mic.on_voice_activity(_on_vad)
    # Boolean status.
    await transport.inject_text(
        {
            "type": "data_stream",
            "streamType": "VAD",
            "data": {"status": True},
        }
    )
    # String status.
    await transport.inject_text(
        {
            "type": "data_stream",
            "streamType": "VAD",
            "data": {"status": "false"},
        }
    )
    assert [r.is_speaking for r in received] == [True, False]
    assert session.mic.is_speaking is False


# ── CameraManager — photo + livestream ─────────────────────────────


@pytest.mark.asyncio
async def test_camera_take_photo_resolves_on_matching_response() -> None:
    session, transport = await _make_connected_session()

    async def _wait_and_inject() -> None:
        for _ in range(20):
            reqs = [
                f for f in transport.sent if f.get("type") == "photo_request"
            ]
            if reqs:
                rid = reqs[-1]["requestId"]
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("photo_request never sent")
        await transport.inject_text(
            {
                "type": "photo_response",
                "requestId": rid,
                "photoUrl": "https://cloud.example/photo.jpg",
                "width": 1280,
                "height": 720,
                "savedToGallery": True,
            }
        )

    inject_task = asyncio.create_task(_wait_and_inject())
    photo = await session.camera.take_photo(save_to_gallery=True, timeout=2.0)
    await inject_task
    assert photo.url == "https://cloud.example/photo.jpg"
    assert photo.width == 1280
    assert photo.height == 720
    assert photo.saved_to_gallery is True


@pytest.mark.asyncio
async def test_camera_take_photo_raises_on_explicit_failure() -> None:
    """Cloud returns ``success: false`` with an error message — the
    manager raises ``RuntimeError`` rather than returning a bogus
    ``PhotoData``."""
    session, transport = await _make_connected_session()

    async def _wait_and_inject() -> None:
        for _ in range(20):
            reqs = [
                f for f in transport.sent if f.get("type") == "photo_request"
            ]
            if reqs:
                rid = reqs[-1]["requestId"]
                break
            await asyncio.sleep(0)
        await transport.inject_text(
            {
                "type": "photo_response",
                "requestId": rid,
                "success": False,
                "error": {"code": "permission_denied", "message": "no cam"},
            }
        )

    inject_task = asyncio.create_task(_wait_and_inject())
    with pytest.raises(RuntimeError, match="no cam"):
        await session.camera.take_photo(timeout=2.0)
    await inject_task


@pytest.mark.asyncio
async def test_camera_start_managed_stream_resolves_on_active_status() -> None:
    """Stream start: send ``managed_stream_request`` → cloud emits
    ``managed_stream_status: active`` with URLs → caller gets a
    ``StreamResult``."""
    session, transport = await _make_connected_session()

    async def _wait_and_inject() -> None:
        for _ in range(20):
            reqs = [
                f
                for f in transport.sent
                if f.get("type") == "managed_stream_request"
            ]
            if reqs:
                break
            await asyncio.sleep(0)
        # Two events: initializing (no URLs), then active (URLs).
        await transport.inject_text(
            {
                "type": "data_stream",
                "streamType": "managed_stream_status",
                "data": {"status": "initializing", "streamId": "strm_1"},
            }
        )
        await transport.inject_text(
            {
                "type": "data_stream",
                "streamType": "managed_stream_status",
                "data": {
                    "status": "active",
                    "streamId": "strm_1",
                    "hlsUrl": "https://cloud.example/hls.m3u8",
                    "dashUrl": "https://cloud.example/dash.mpd",
                    "webrtcUrl": "https://cloud.example/whep",
                },
            }
        )

    inject_task = asyncio.create_task(_wait_and_inject())
    result = await session.camera.start_managed_stream(timeout=2.0)
    await inject_task
    assert result.hls_url == "https://cloud.example/hls.m3u8"
    assert result.dash_url == "https://cloud.example/dash.mpd"
    assert result.webrtc_url == "https://cloud.example/whep"
    assert result.stream_id == "strm_1"


@pytest.mark.asyncio
async def test_camera_stop_stream_emits_managed_stream_stop() -> None:
    session, transport = await _make_connected_session()
    await session.camera.stop_stream()
    frames = [
        f for f in transport.sent if f.get("type") == "managed_stream_stop"
    ]
    assert len(frames) == 1
    assert frames[-1]["sessionId"] == "sess_001"


# ── Reconnect handling ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reconnect_rejected_permanent_marks_session_closed() -> None:
    """``NOT_RUNNING`` / ``BOOT_TIMEOUT`` codes mean the cloud will
    never accept us back — flip the permanent flag so the service
    layer drops us from its registry."""
    session, transport = await _make_connected_session()
    await transport.inject_text(
        {
            "type": "reconnect_rejected",
            "code": "NOT_RUNNING",
            "message": "session terminated",
        }
    )
    assert session.is_permanently_closed is True


@pytest.mark.asyncio
async def test_reconnect_rejected_transient_does_not_mark_permanent() -> None:
    """Codes other than the two terminal ones are transient — leave
    the permanent flag clear so the service can decide whether to
    retry (cloud also re-fires the webhook for user-initiated
    opens, which is the v1 recovery path)."""
    session, transport = await _make_connected_session()
    await transport.inject_text(
        {
            "type": "reconnect_rejected",
            "code": "RATE_LIMITED",
            "message": "try again later",
        }
    )
    assert session.is_permanently_closed is False


@pytest.mark.asyncio
async def test_reconnect_deferred_logs_without_marking_permanent() -> None:
    """Cloud asks us to wait — no permanent close, no transport
    teardown. Just logs the timeout."""
    session, transport = await _make_connected_session()
    await transport.inject_text(
        {
            "type": "reconnect_deferred",
            "timeoutMs": 5000,
        }
    )
    assert session.is_permanently_closed is False
    assert session.is_connected is True


# ── Transport SSL ──────────────────────────────────────────────────


def test_transport_uses_certifi_ssl_context_for_wss_urls() -> None:
    """Python's default SSL context fails on NixOS hosts because the
    OS doesn't ship the CA bundle at the path Python expects. We use
    ``certifi``'s bundled Mozilla roots instead — the same fix every
    reasonable Python HTTP library applies.

    Regression test for the deployment to meridian: without this,
    every ``wss://`` open to Mentra Cloud raised
    ``CERTIFICATE_VERIFY_FAILED`` and the glasses showed "Can't
    connect to Gilbert"."""
    import ssl

    from gilbert_plugin_mentra.session.transport import (
        _DEFAULT_SSL_CONTEXT,
    )

    assert isinstance(_DEFAULT_SSL_CONTEXT, ssl.SSLContext)
    # Verify mode should be CERT_REQUIRED (the default for
    # ``create_default_context``). If a future refactor accidentally
    # neuters TLS verification, this test fails loudly.
    assert _DEFAULT_SSL_CONTEXT.verify_mode == ssl.CERT_REQUIRED
    # Default context should be configured to check hostnames too —
    # disabling that would let an attacker present any valid cert.
    assert _DEFAULT_SSL_CONTEXT.check_hostname is True
