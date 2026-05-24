"""Unit tests for the Telnyx telephony plugin.

These cover the pure-logic bits — webhook dispatch into session
queues, session bookkeeping, custom-state encoding — without hitting
the real Telnyx API. Anything that requires a real outbound call
(``place_call``, the actual HTTP request) is exercised manually
once the operator provisions a Telnyx number.
"""

from __future__ import annotations

import asyncio

import pytest
from gilbert_plugin_telnyx.telnyx_telephony import (  # type: ignore[import-not-found]
    TelnyxTelephony,
    _active_sessions,
    _call_control_to_gilbert,
    _encode_client_state,
    _TelnyxCallSession,
    deliver_webhook_event,
    find_session_by_token,
)

from gilbert.interfaces.telephony import (
    CallErrorEvent,
    CallStatus,
    CallStatusEvent,
    DtmfEvent,
    TelephonyBackend,
)


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    """Each test starts with an empty session registry — otherwise
    sessions from a previous test would leak across cases."""
    _active_sessions.clear()
    _call_control_to_gilbert.clear()


def _make_session(
    *,
    call_id: str = "call_xyz",
    cc_id: str = "cc_xyz",
    token: str = "tok_xyz",
) -> _TelnyxCallSession:
    """Build a session bypassing the real ``place_call`` path so tests
    don't need a TelnyxTelephony instance hitting the network."""
    session = _TelnyxCallSession(
        call_id=call_id,
        call_control_id=cc_id,
        webhook_token=token,
        backend=TelnyxTelephony(),
    )
    _active_sessions[call_id] = session
    _call_control_to_gilbert[cc_id] = call_id
    return session


# ── Registration / discovery ──────────────────────────────────────────


def test_backend_self_registers_with_abc() -> None:
    """Importing the module triggers ``__init_subclass__`` which puts
    ``TelnyxTelephony`` in the ABC's registry under ``"telnyx"``. This
    is what lets ``PhoneCallService`` discover it without a hard import."""
    registry = TelephonyBackend.registered_backends()
    assert "telnyx" in registry
    assert registry["telnyx"] is TelnyxTelephony


def test_backend_advertises_required_config_params() -> None:
    """Operator-facing settings come from ``backend_config_params``;
    if these names change, the Settings UI loses its keys and tests
    will catch it."""
    keys = {p.key for p in TelnyxTelephony.backend_config_params()}
    assert keys == {"api_key", "connection_id", "public_url"}
    # ``api_key`` must be marked sensitive so the UI redacts it.
    api_key_param = next(
        p for p in TelnyxTelephony.backend_config_params() if p.key == "api_key"
    )
    assert api_key_param.sensitive is True


def test_find_session_by_token_returns_only_live_sessions() -> None:
    sess = _make_session(token="tok_specific")
    assert find_session_by_token("tok_specific") is sess
    assert find_session_by_token("") is None
    assert find_session_by_token("tok_doesnt_exist") is None


# ── Webhook dispatch ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_webhook_call_initiated_pushes_status_event() -> None:
    sess = _make_session(cc_id="cc1")
    await deliver_webhook_event(
        {
            "data": {
                "event_type": "call.initiated",
                "payload": {"call_control_id": "cc1"},
            }
        }
    )
    ev = await asyncio.wait_for(sess._events_queue.get(), timeout=0.5)
    assert isinstance(ev, CallStatusEvent)
    assert ev.status is CallStatus.INITIATED


@pytest.mark.asyncio
async def test_webhook_call_answered_maps_to_connected() -> None:
    """Telnyx's ``call.answered`` is what we call ``CONNECTED`` — the
    brain starts being allowed to speak only when this fires."""
    sess = _make_session(cc_id="cc2")
    await deliver_webhook_event(
        {
            "data": {
                "event_type": "call.answered",
                "payload": {"call_control_id": "cc2"},
            }
        }
    )
    ev = await asyncio.wait_for(sess._events_queue.get(), timeout=0.5)
    assert isinstance(ev, CallStatusEvent)
    assert ev.status is CallStatus.CONNECTED


@pytest.mark.asyncio
async def test_webhook_hangup_evicts_session_from_registry() -> None:
    """On hangup the session leaves the active registry so subsequent
    webhooks for that call_control_id are no-ops and a fresh call can
    re-use the slot without conflict."""
    sess = _make_session(call_id="call_hangup", cc_id="cc_hangup")
    assert _active_sessions["call_hangup"] is sess

    await deliver_webhook_event(
        {
            "data": {
                "event_type": "call.hangup",
                "payload": {
                    "call_control_id": "cc_hangup",
                    "hangup_cause": "normal_clearing",
                },
            }
        }
    )

    ev = await asyncio.wait_for(sess._events_queue.get(), timeout=0.5)
    assert isinstance(ev, CallStatusEvent)
    assert ev.status is CallStatus.HUNG_UP
    assert ev.reason == "normal_clearing"
    assert "call_hangup" not in _active_sessions
    assert "cc_hangup" not in _call_control_to_gilbert


@pytest.mark.asyncio
async def test_webhook_dtmf_pushes_dtmf_event() -> None:
    sess = _make_session(cc_id="cc_dtmf")
    await deliver_webhook_event(
        {
            "data": {
                "event_type": "call.dtmf.received",
                "payload": {"call_control_id": "cc_dtmf", "digit": "5"},
            }
        }
    )
    ev = await asyncio.wait_for(sess._events_queue.get(), timeout=0.5)
    assert isinstance(ev, DtmfEvent)
    assert ev.digit == "5"


@pytest.mark.asyncio
async def test_webhook_streaming_failed_pushes_error_event() -> None:
    """A streaming.failed event is recoverable for the call (the call
    itself may still be up), but the brain needs to know — usually it
    won't be able to do anything useful after this and will time out
    on the watchdog."""
    sess = _make_session(cc_id="cc_stream")
    await deliver_webhook_event(
        {
            "data": {
                "event_type": "streaming.failed",
                "payload": {
                    "call_control_id": "cc_stream",
                    "reason": "carrier_error",
                },
            }
        }
    )
    ev = await asyncio.wait_for(sess._events_queue.get(), timeout=0.5)
    assert isinstance(ev, CallErrorEvent)
    assert "carrier_error" in ev.message
    assert ev.recoverable is False


@pytest.mark.asyncio
async def test_webhook_for_unknown_call_id_is_silently_dropped() -> None:
    """An event for a call_control_id we don't have a session for is a
    common race (Telnyx may webhook before our place_call returns, or
    a stale retry arrives long after hangup). It must not raise."""
    await deliver_webhook_event(
        {
            "data": {
                "event_type": "call.answered",
                "payload": {"call_control_id": "cc_unknown"},
            }
        }
    )
    # Nothing to assert except "didn't raise."


@pytest.mark.asyncio
async def test_webhook_without_call_control_id_is_silently_dropped() -> None:
    """Malformed payloads happen — Telnyx has occasionally shipped
    events with missing fields. The route must tolerate them and
    return 200 so they don't get retried into infinity."""
    await deliver_webhook_event({"data": {"event_type": "call.answered"}})
    await deliver_webhook_event({})


# ── Session bookkeeping ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_push_audio_in_queue_overflow_drops_oldest() -> None:
    """Producer outpacing consumer is normal — silence detector takes
    20ms+ to decide. We drop the OLDEST chunk so the latency tail
    doesn't grow; dropping the newest would feel like a stutter to the
    caller."""
    sess = _make_session()
    sess._audio_in_queue = asyncio.Queue(maxsize=3)
    await sess.push_audio_in(b"a")
    await sess.push_audio_in(b"b")
    await sess.push_audio_in(b"c")
    await sess.push_audio_in(b"d")  # forces eviction of "a"

    out = [await sess._audio_in_queue.get() for _ in range(3)]
    assert out == [b"b", b"c", b"d"]


@pytest.mark.asyncio
async def test_hang_up_is_idempotent() -> None:
    """Two-side hangup is a race we expect — the brain's hang_up tool
    and a remote-initiated webhook can both fire within the same
    millisecond. Calling _do_hang_up twice must not error or push
    duplicate events."""
    sess = _make_session()
    await sess._do_hang_up()
    # Must not raise / push extra events.
    await sess._do_hang_up()
    assert sess.closed is True


# ── client_state encoding ─────────────────────────────────────────────


def test_client_state_encoding_roundtrips() -> None:
    """``client_state`` is Telnyx's secondary correlation channel (in
    addition to call_control_id). Whatever we encode here must round-
    trip through base64 + JSON, since Telnyx echoes it back as a
    string."""
    import base64
    import json

    encoded = _encode_client_state(
        {"call_id": "call_x", "token": "tok_y"}
    )
    decoded = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert decoded == {"call_id": "call_x", "token": "tok_y"}
