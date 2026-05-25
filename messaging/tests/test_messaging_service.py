"""Messaging service tests — protocol shape + send/receive round-trip.

Mocks the carrier backend so we test ``MessagingService`` orchestration:
backend dispatch, persistence, multi-user routing of inbound events,
ToolProvider surface, and the WS handlers.
"""

from __future__ import annotations

from typing import Any

import pytest

from gilbert.interfaces.messaging import (
    Message,
    MessageDirection,
    MessageStatus,
    MessagingBackend,
    MessagingProvider,
)


# ── Test doubles ────────────────────────────────────────────────────


class _FakeBackend(MessagingBackend):
    """In-memory MessagingBackend. Captures sends so tests can assert
    on them; supports a ``simulate_inbound()`` helper for the
    receive path."""

    backend_name = "fake_messaging"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.next_id_seq = 0
        self._deliverer: Any = None
        self.fail_next_send = False

    @classmethod
    def backend_config_params(cls):  # type: ignore[override]
        return []

    async def initialize(self, config: dict[str, object]) -> None:
        pass

    async def close(self) -> None:
        pass

    async def send_message(
        self,
        *,
        to: str,
        body: str,
        from_number: str = "",
        media_urls: list[str] | None = None,
    ) -> str:
        if self.fail_next_send:
            self.fail_next_send = False
            raise RuntimeError("simulated carrier failure")
        self.next_id_seq += 1
        msg_id = f"fake_{self.next_id_seq:04d}"
        self.sent.append(
            {
                "id": msg_id,
                "to": to,
                "from": from_number,
                "body": body,
                "media_urls": list(media_urls or []),
            }
        )
        return msg_id

    # Custom hook the service calls when wiring inbound delivery.
    def bind_inbound_deliverer(self, deliverer: Any) -> None:
        self._deliverer = deliverer

    async def simulate_inbound(self, message: Message) -> None:
        if self._deliverer is None:
            raise RuntimeError("no inbound deliverer bound")
        await self._deliverer(message)


class _InMemoryStorageBackend:
    """Minimal StorageBackend that lives in a dict — enough for these
    tests. The real backend is sqlite/duckdb; we don't need its
    behaviour here."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    async def put(self, collection: str, entity_id: str, data: dict[str, Any]) -> None:
        self.rows[(collection, entity_id)] = dict(data)

    async def get(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        return self.rows.get((collection, entity_id))

    async def delete(self, collection: str, entity_id: str) -> None:
        self.rows.pop((collection, entity_id), None)

    async def query(self, query: Any) -> list[dict[str, Any]]:
        # Tests only filter on user_id (+ optionally other_number /
        # our_number). Implement those, ignore sort semantics beyond
        # created_at order which is preserved by insertion order.
        out: list[dict[str, Any]] = []
        for (col, _), row in self.rows.items():
            if col != query.collection:
                continue
            keep = True
            for f in query.filters or []:
                if str(row.get(f.field) or "") != str(f.value):
                    keep = False
                    break
            if keep:
                out.append(row)
        out.sort(
            key=lambda r: str(r.get("created_at") or ""),
            reverse=False,
        )
        return out[: query.limit] if query.limit else out

    async def delete_query(self, query: Any) -> int:
        return 0


class _InMemoryStorage:
    """Stand-in for ``StorageProvider``. The service does
    ``self._storage.backend.put(...)`` etc, so this just wraps the
    real backend behind the ``.backend`` property."""

    def __init__(self) -> None:
        self._inner = _InMemoryStorageBackend()

    @property
    def backend(self) -> _InMemoryStorageBackend:
        return self._inner

    @property
    def raw_backend(self) -> _InMemoryStorageBackend:
        return self._inner

    def create_namespaced(self, namespace: str) -> _InMemoryStorageBackend:
        return self._inner

    # Convenience for assertions (tests poke at ``storage.rows``).
    @property
    def rows(self) -> dict[tuple[str, str], dict[str, Any]]:
        return self._inner.rows


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, ev: Any) -> None:
        self.events.append(ev)

    def subscribe(self, *_a, **_kw):
        return lambda: None


class _BusProvider:
    def __init__(self, bus: _Bus) -> None:
        self.bus = bus


class _Resolver:
    def __init__(self, **capabilities: Any) -> None:
        self._caps = capabilities

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


def _service_with_backend(*, owner_user_id: str = "usr_alice", auto_reply: bool = False):
    from gilbert_plugin_messaging.messaging_service import MessagingService

    svc = MessagingService()
    return svc, owner_user_id, auto_reply


async def _start_service(
    svc: Any,
    *,
    enabled: bool = True,
    owner_user_id: str = "usr_alice",
    auto_reply: bool = False,
    ai: Any = None,
):
    """Drive a service through start() with a stub resolver +
    in-memory storage + bus."""
    storage = _InMemoryStorage()
    bus = _Bus()
    config_section = {
        "enabled": enabled,
        "backend": "fake_messaging",
        "from_number": "+15551234567",
        "owner_user_id": owner_user_id,
        "auto_reply": auto_reply,
        "settings": {},
    }

    class _Cfg:
        # Stubbed surface to satisfy the @runtime_checkable
        # ConfigurationReader Protocol — isinstance checks the
        # attribute exists, not the signature.
        def get(self, path: str) -> Any:
            return None

        def get_section(self, name: str) -> dict[str, Any]:
            return dict(config_section) if name == "messaging" else {}

        def get_section_safe(self, name: str) -> dict[str, Any]:
            return self.get_section(name)

        async def set(self, path: str, value: Any) -> dict[str, Any]:
            return {}

    caps: dict[str, Any] = {
        "entity_storage": storage,
        "event_bus": _BusProvider(bus),
        "configuration": _Cfg(),
    }
    if ai is not None:
        caps["ai_chat"] = ai
    await svc.start(_Resolver(**caps))
    return storage, bus


# ── Test cases ──────────────────────────────────────────────────────


def test_service_satisfies_messaging_provider_protocol() -> None:
    """Runtime-checkable Protocol catches drift in the service surface."""
    from gilbert_plugin_messaging.messaging_service import MessagingService

    svc = MessagingService()
    assert isinstance(svc, MessagingProvider)


def test_disabled_by_default() -> None:
    """``enabled=False`` in config keeps the backend unwired and the
    service inert. Mirrors the voice-agent / phone defaults."""
    from gilbert_plugin_messaging.messaging_service import MessagingService

    svc = MessagingService()
    assert svc._enabled is False  # noqa: SLF001  — internal smoke
    # And service_info advertises the capability regardless so the
    # ``messaging`` capability resolves to "service exists, just off"
    # rather than ``None``.
    info = svc.service_info()
    assert "messaging" in info.capabilities
    assert info.toggleable is True


@pytest.mark.asyncio
async def test_outbound_send_persists_and_publishes() -> None:
    svc, _, _ = _service_with_backend()
    storage, bus = await _start_service(svc)
    assert svc._enabled is True  # noqa: SLF001

    msg = await svc.send(
        user_id="usr_alice",
        to_number="+15555550100",
        body="hello world",
    )

    assert msg.status == MessageStatus.SENT.value
    assert msg.direction == MessageDirection.OUTBOUND.value
    assert msg.our_number == "+15551234567"
    assert msg.other_number == "+15555550100"
    # Persisted under the backend-issued id.
    assert ("messages", msg.message_id) in storage.rows
    # Two events published: message_sent + thread_updated.
    event_types = sorted(getattr(e, "event_type", "") for e in bus.events)
    assert "messaging.message_sent" in event_types
    assert "messaging.thread_updated" in event_types


@pytest.mark.asyncio
async def test_outbound_send_failure_records_failed_status() -> None:
    """Carrier rejection mustn't lose the row — the SPA needs to show
    the failure so the user can retry."""
    svc, _, _ = _service_with_backend()
    storage, bus = await _start_service(svc)
    # Coerce the next send to fail. The backend is the one MessagingService
    # picked up during start; we need a reference to it.
    backend = svc._backend  # noqa: SLF001
    assert isinstance(backend, _FakeBackend)
    backend.fail_next_send = True

    msg = await svc.send(
        user_id="usr_alice",
        to_number="+15555550199",
        body="this will fail",
    )

    assert msg.status == MessageStatus.FAILED.value
    assert "simulated carrier failure" in msg.error
    assert msg.message_id.startswith("local_")
    assert ("messages", msg.message_id) in storage.rows


@pytest.mark.asyncio
async def test_inbound_delivery_resolves_owner_and_publishes() -> None:
    """The backend hands us a parsed inbound message with no
    ``user_id`` — the service must look up the owner from config
    (``owner_user_id``) and route the message there."""
    svc, _, _ = _service_with_backend(owner_user_id="usr_alice")
    storage, bus = await _start_service(svc, owner_user_id="usr_alice")
    backend = svc._backend  # noqa: SLF001
    assert isinstance(backend, _FakeBackend)

    inbound = Message(
        message_id="carrier_inbound_001",
        user_id="",  # backend doesn't know — service must resolve
        our_number="+15551234567",
        other_number="+15555550100",
        direction=MessageDirection.INBOUND.value,
        body="hey gilbert",
        status=MessageStatus.RECEIVED.value,
        created_at="2099-01-01T00:00:00Z",
    )
    await backend.simulate_inbound(inbound)

    saved = storage.rows.get(("messages", "carrier_inbound_001"))
    assert saved is not None
    assert saved["user_id"] == "usr_alice"
    assert saved["direction"] == "inbound"
    event_types = [getattr(e, "event_type", "") for e in bus.events]
    assert "messaging.message_received" in event_types
    assert "messaging.thread_updated" in event_types


@pytest.mark.asyncio
async def test_inbound_dropped_when_no_owner_configured() -> None:
    """Without ``owner_user_id`` we have no way to attribute the
    message. Better to drop + warn than to drop into ``user_id=""``
    where the recipient never sees it."""
    svc, _, _ = _service_with_backend(owner_user_id="")
    storage, _ = await _start_service(svc, owner_user_id="")
    backend = svc._backend  # noqa: SLF001

    inbound = Message(
        message_id="orphan",
        user_id="",
        our_number="+15551234567",
        other_number="+15555550199",
        direction=MessageDirection.INBOUND.value,
        body="hello",
        status=MessageStatus.RECEIVED.value,
        created_at="2099-01-01T00:00:00Z",
    )
    await backend.simulate_inbound(inbound)
    # Nothing persisted, no row.
    assert ("messages", "orphan") not in storage.rows


@pytest.mark.asyncio
async def test_list_threads_groups_by_other_number() -> None:
    """One sent + one received from the same number → one thread.
    Two sends to different numbers → two threads, most-recent first."""
    svc, _, _ = _service_with_backend(owner_user_id="usr_alice")
    storage, _ = await _start_service(svc, owner_user_id="usr_alice")

    await svc.send(
        user_id="usr_alice",
        to_number="+15555550100",
        body="ping #1",
    )
    await svc.send(
        user_id="usr_alice",
        to_number="+15555550200",
        body="other number",
    )
    backend = svc._backend  # noqa: SLF001
    await backend.simulate_inbound(
        Message(
            message_id="inb_1",
            user_id="",
            our_number="+15551234567",
            other_number="+15555550100",
            direction=MessageDirection.INBOUND.value,
            body="pong",
            status=MessageStatus.RECEIVED.value,
            created_at="2099-01-01T00:00:00Z",
        )
    )

    threads = await svc.list_threads("usr_alice")
    assert len(threads) == 2
    # Most-recent first → +15555550100 has the inbound reply at 14:00.
    assert threads[0].other_number == "+15555550100"
    assert threads[0].message_count == 2
    assert threads[1].other_number == "+15555550200"
    assert threads[1].message_count == 1


@pytest.mark.asyncio
async def test_get_messages_returns_thread_ordered() -> None:
    svc, _, _ = _service_with_backend(owner_user_id="usr_alice")
    storage, _ = await _start_service(svc, owner_user_id="usr_alice")
    await svc.send(
        user_id="usr_alice",
        to_number="+15555550100",
        body="first",
    )
    backend = svc._backend  # noqa: SLF001
    await backend.simulate_inbound(
        Message(
            message_id="inb",
            user_id="",
            our_number="+15551234567",
            other_number="+15555550100",
            direction=MessageDirection.INBOUND.value,
            body="reply",
            status=MessageStatus.RECEIVED.value,
            created_at="2099-01-01T00:00:00Z",
        )
    )

    msgs = await svc.get_messages(
        user_id="usr_alice",
        other_number="+15555550100",
    )
    assert [m.direction for m in msgs] == ["outbound", "inbound"]
    assert msgs[0].body == "first"
    assert msgs[1].body == "reply"


def test_tool_surface_only_when_enabled_and_backend_present() -> None:
    """Without start() the service hasn't enabled itself nor wired a
    backend — get_tools() must return [] so the LLM doesn't see a
    dead tool."""
    from gilbert_plugin_messaging.messaging_service import MessagingService

    svc = MessagingService()
    assert svc.get_tools() == []


@pytest.mark.asyncio
async def test_tool_visible_after_start_with_backend() -> None:
    svc, _, _ = _service_with_backend()
    await _start_service(svc)
    tools = svc.get_tools()
    assert len(tools) == 1
    t = tools[0]
    assert t.name == "send_text_message"
    assert t.slash_command == "send"
    assert t.slash_help  # non-empty
    assert {p.name for p in t.parameters} == {"to_number", "body"}


@pytest.mark.asyncio
async def test_send_requires_from_number_configured() -> None:
    svc, _, _ = _service_with_backend()
    await _start_service(svc)
    # Wipe the configured from_number to simulate missing config.
    svc._from_number = ""  # noqa: SLF001
    with pytest.raises(RuntimeError, match="from_number"):
        await svc.send(
            user_id="usr_alice",
            to_number="+15555550100",
            body="hi",
        )


@pytest.mark.asyncio
async def test_auto_reply_skipped_when_disabled() -> None:
    """``auto_reply=False`` (the default) keeps Gilbert quiet — inbound
    arrives, persists, but no outbound send is triggered."""
    svc, _, _ = _service_with_backend()
    storage, _ = await _start_service(svc, auto_reply=False)
    backend = svc._backend  # noqa: SLF001
    sent_before = len(backend.sent)

    await backend.simulate_inbound(
        Message(
            message_id="inb_skip",
            user_id="",
            our_number="+15551234567",
            other_number="+15555550100",
            direction=MessageDirection.INBOUND.value,
            body="should not auto-reply",
            status=MessageStatus.RECEIVED.value,
            created_at="2099-01-01T00:00:00Z",
        )
    )
    assert len(backend.sent) == sent_before


@pytest.mark.asyncio
async def test_auto_reply_sends_when_enabled_and_llm_returns_text() -> None:
    class _FakeAI:
        async def chat(self, **kwargs: Any) -> Any:
            class _Result:
                response_text = "got it, thanks"
                conversation_id = ""
                ui_blocks: list[dict[str, Any]] = []
                tool_usage: list[dict[str, Any]] = []
                attachments: list[Any] = []
                rounds: list[dict[str, Any]] = []
                interrupted = False
                model = ""
                turn_usage = None

            return _Result()

    svc, _, _ = _service_with_backend()
    storage, _ = await _start_service(
        svc, auto_reply=True, ai=_FakeAI()
    )
    backend = svc._backend  # noqa: SLF001
    sent_before = len(backend.sent)

    await backend.simulate_inbound(
        Message(
            message_id="inb_reply",
            user_id="",
            our_number="+15551234567",
            other_number="+15555550100",
            direction=MessageDirection.INBOUND.value,
            body="hey",
            status=MessageStatus.RECEIVED.value,
            created_at="2099-01-01T00:00:00Z",
        )
    )
    assert len(backend.sent) == sent_before + 1
    reply = backend.sent[-1]
    assert reply["body"] == "got it, thanks"
    assert reply["to"] == "+15555550100"
    assert reply["from"] == "+15551234567"


@pytest.mark.asyncio
async def test_auto_reply_silent_when_llm_returns_empty() -> None:
    """The system prompt explicitly tells the LLM that empty text =
    don't reply. Service must respect that."""

    class _SilentAI:
        async def chat(self, **kwargs: Any) -> Any:
            class _Result:
                response_text = "   "  # whitespace only
                conversation_id = ""
                ui_blocks: list[dict[str, Any]] = []
                tool_usage: list[dict[str, Any]] = []
                attachments: list[Any] = []
                rounds: list[dict[str, Any]] = []
                interrupted = False
                model = ""
                turn_usage = None

            return _Result()

    svc, _, _ = _service_with_backend()
    await _start_service(svc, auto_reply=True, ai=_SilentAI())
    backend = svc._backend  # noqa: SLF001
    sent_before = len(backend.sent)

    await backend.simulate_inbound(
        Message(
            message_id="inb_silent",
            user_id="",
            our_number="+15551234567",
            other_number="+15555550100",
            direction=MessageDirection.INBOUND.value,
            body="don't bother",
            status=MessageStatus.RECEIVED.value,
            created_at="2099-01-01T00:00:00Z",
        )
    )
    assert len(backend.sent) == sent_before
