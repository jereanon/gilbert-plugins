"""Tests that exercise the MQTT subscriber loop with a fake aiomqtt client."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from gilbert_plugin_frigate.mqtt_client import FrigateMQTT

from gilbert.interfaces.camera import CameraBackendError


class _FakeTopic:
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


class _FakeMessage:
    def __init__(self, topic: str, payload: bytes | str) -> None:
        self.topic = _FakeTopic(topic)
        self.payload = payload


class _FakeClient:
    """Minimal aiomqtt.Client substitute.

    Yields a configurable list of messages from ``messages`` then waits
    for a stop signal. Tests pass an instance via ``client_factory``.
    """

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.subscribed: list[str] = []
        self._messages: list[_FakeMessage] = []
        self._iter_done = asyncio.Event()
        self._closed = False

    @classmethod
    def with_messages(
        cls, messages: list[_FakeMessage]
    ) -> Any:
        """Return a factory that produces a client preloaded with messages."""

        def _factory(**kwargs: Any) -> _FakeClient:
            inst = cls(**kwargs)
            inst._messages = list(messages)
            return inst

        return _factory

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._closed = True
        self._iter_done.set()

    async def subscribe(self, topic: str) -> None:
        self.subscribed.append(topic)

    @property
    def messages(self) -> Any:
        async def _gen():
            for msg in self._messages:
                yield msg
            # Once buffered messages are exhausted, block until __aexit__.
            await self._iter_done.wait()

        return _gen()


@pytest.mark.asyncio
async def test_mqtt_yields_normalized_event() -> None:
    after = {
        "id": "evt-1",
        "camera": "porch",
        "label": "person",
        "score": 0.8,
        "start_time": 1.0,
        "has_snapshot": True,
    }
    payload = json.dumps({"type": "new", "after": after}).encode()
    factory = _FakeClient.with_messages(
        [_FakeMessage("frigate/events", payload)]
    )
    mqtt = FrigateMQTT(
        host="h",
        port=1883,
        prefix="frigate",
        client_factory=factory,
    )
    await mqtt.start()
    received = []

    async def consume() -> None:
        async for ev in mqtt.events():
            received.append(ev)

    task = asyncio.create_task(consume())
    # Give the loop a tick to pull the buffered message.
    await asyncio.sleep(0.05)
    await mqtt.stop()
    await task
    assert len(received) == 1
    assert received[0].event_id == "evt-1"


@pytest.mark.asyncio
async def test_mqtt_lwt_offline_raises_camera_backend_error() -> None:
    factory = _FakeClient.with_messages(
        [_FakeMessage("frigate/available", b"offline")]
    )
    mqtt = FrigateMQTT(
        host="h",
        port=1883,
        prefix="frigate",
        client_factory=factory,
    )
    await mqtt.start()
    consumed: list[Any] = []
    err: Exception | None = None

    async def consume() -> None:
        nonlocal err
        try:
            async for ev in mqtt.events():
                consumed.append(ev)
        except CameraBackendError as exc:
            err = exc

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await mqtt.stop()
    await task
    assert err is not None
    assert "frigate offline" in str(err)


@pytest.mark.asyncio
async def test_mqtt_invalid_json_payload_dropped() -> None:
    factory = _FakeClient.with_messages(
        [_FakeMessage("frigate/events", b"not-json{{")]
    )
    mqtt = FrigateMQTT(
        host="h",
        port=1883,
        prefix="frigate",
        client_factory=factory,
    )
    await mqtt.start()
    consumed: list[Any] = []

    async def consume() -> None:
        async for ev in mqtt.events():
            consumed.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    await mqtt.stop()
    await task
    # Invalid JSON dropped; no events emitted.
    assert consumed == []


@pytest.mark.asyncio
async def test_mqtt_subscribes_to_events_and_available() -> None:
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _FakeClient:
        client = _FakeClient(**kwargs)
        captured["client"] = client
        return client

    mqtt = FrigateMQTT(
        host="h",
        port=1883,
        prefix="frigate",
        client_factory=factory,
    )
    await mqtt.start()
    await asyncio.sleep(0.05)
    await mqtt.stop()
    client = captured["client"]
    assert "frigate/events" in client.subscribed
    assert "frigate/available" in client.subscribed
