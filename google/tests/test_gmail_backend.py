"""Unit tests for the Gmail email backend.

These tests focus on the MIME message construction inside ``send()`` —
specifically that Reply-To and a formatted From header are applied
correctly. The Gmail API is stubbed with a minimal fake service that
captures the raw base64-encoded payload so we can assert on the headers.
"""

from __future__ import annotations

import base64
import ssl
from email import message_from_bytes
from email.message import Message
from typing import Any

import pytest
from gilbert_plugin_google.gmail import GmailBackend

from gilbert.interfaces.email import EmailAddress, TransientEmailError


class _FakeSendRequest:
    def __init__(self, captured: dict[str, Any], body: dict[str, Any]) -> None:
        self._captured = captured
        self._body = body

    def execute(self) -> dict[str, str]:
        self._captured.update(self._body)
        return {"id": "sent_123"}


class _FakeMessagesResource:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def send(self, userId: str, body: dict[str, Any]) -> _FakeSendRequest:  # noqa: N803
        # Matches the google-api-python-client Gmail send signature.
        return _FakeSendRequest(self._captured, body)


class _FakeUsersResource:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def messages(self) -> _FakeMessagesResource:
        return _FakeMessagesResource(self._captured)


class _FakeGmailService:
    def __init__(self) -> None:
        self.captured: dict[str, Any] = {}

    def users(self) -> _FakeUsersResource:
        return _FakeUsersResource(self.captured)


def _decode_raw(raw: str) -> Message:
    # Gmail API expects url-safe base64; decode and parse back into a MIME message
    payload = base64.urlsafe_b64decode(raw.encode("ascii"))
    return message_from_bytes(payload)


@pytest.fixture
def backend_with_fake_service() -> tuple[GmailBackend, _FakeGmailService]:
    backend = GmailBackend()
    backend._email_address = "assistant@example.com"
    fake = _FakeGmailService()
    backend._service = fake
    return backend, fake


@pytest.mark.asyncio
async def test_send_default_from_and_no_reply_to(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["From"] == "assistant@example.com"
    assert msg["Reply-To"] is None
    assert msg["To"] == "customer@example.com"


@pytest.mark.asyncio
async def test_send_formats_from_name(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
        from_name="Example Co",
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["From"] == "Example Co <assistant@example.com>"


@pytest.mark.asyncio
async def test_send_sets_reply_to(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
        reply_to=EmailAddress(email="sales@example.com"),
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["Reply-To"] == "sales@example.com"


@pytest.mark.asyncio
async def test_send_sets_reply_to_with_name(
    backend_with_fake_service: tuple[GmailBackend, _FakeGmailService],
) -> None:
    backend, fake = backend_with_fake_service

    await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
        reply_to=EmailAddress(email="sales@example.com", name="Example Sales"),
        from_name="Example Co",
    )

    msg = _decode_raw(fake.captured["raw"])
    assert msg["From"] == "Example Co <assistant@example.com>"
    assert msg["Reply-To"] == "Example Sales <sales@example.com>"


# ── Transient transport-error retry ─────────────────────────────────


class _FlakySendRequest:
    """Send request that raises a transport-flavored exception the first
    time ``.execute()`` is called, then succeeds on subsequent calls."""

    def __init__(
        self,
        captured: dict[str, Any],
        body: dict[str, Any],
        failures_remaining: list[int],
        exc_to_raise: BaseException,
    ) -> None:
        self._captured = captured
        self._body = body
        self._failures_remaining = failures_remaining
        self._exc = exc_to_raise

    def execute(self) -> dict[str, str]:
        if self._failures_remaining[0] > 0:
            self._failures_remaining[0] -= 1
            raise self._exc
        self._captured.update(self._body)
        return {"id": "sent_after_retry"}


class _FlakyMessagesResource:
    def __init__(
        self,
        captured: dict[str, Any],
        failures_remaining: list[int],
        exc_to_raise: BaseException,
    ) -> None:
        self._captured = captured
        self._failures_remaining = failures_remaining
        self._exc = exc_to_raise

    def send(self, userId: str, body: dict[str, Any]) -> _FlakySendRequest:  # noqa: N803
        return _FlakySendRequest(
            self._captured, body, self._failures_remaining, self._exc
        )


class _FlakyUsersResource:
    def __init__(
        self,
        captured: dict[str, Any],
        failures_remaining: list[int],
        exc_to_raise: BaseException,
    ) -> None:
        self._captured = captured
        self._failures_remaining = failures_remaining
        self._exc = exc_to_raise

    def messages(self) -> _FlakyMessagesResource:
        return _FlakyMessagesResource(
            self._captured, self._failures_remaining, self._exc
        )


class _FlakyGmailService:
    """Stand-in Gmail service whose ``send().execute()`` raises a
    transport error a configurable number of times before succeeding."""

    def __init__(self, failures: int, exc: BaseException) -> None:
        self.captured: dict[str, Any] = {}
        self._failures_remaining = [failures]
        self._exc = exc

    def users(self) -> _FlakyUsersResource:
        return _FlakyUsersResource(
            self.captured, self._failures_remaining, self._exc
        )


@pytest.mark.asyncio
async def test_send_rebuilds_service_and_retries_on_broken_pipe() -> None:
    """A stale-connection BrokenPipeError should trigger one rebuild +
    retry. After the retry succeeds we get a normal sent_id back."""
    backend = GmailBackend()
    backend._email_address = "assistant@example.com"
    flaky = _FlakyGmailService(
        failures=1, exc=BrokenPipeError(32, "Broken pipe")
    )
    backend._service = flaky

    rebuild_calls: list[int] = []

    async def fake_rebuild() -> None:
        rebuild_calls.append(1)
        # Install a fresh service so the retry path uses it. The new
        # service still has 0 failures left, so the retry succeeds.
        backend._service = _FlakyGmailService(failures=0, exc=BrokenPipeError())

    backend._rebuild_service = fake_rebuild  # type: ignore[method-assign]

    sent_id = await backend.send(
        to=[EmailAddress(email="customer@example.com")],
        subject="Hello",
        body_html="<p>Hi</p>",
    )

    assert sent_id == "sent_after_retry"
    assert rebuild_calls == [1]


@pytest.mark.asyncio
async def test_send_raises_transient_when_retry_also_fails() -> None:
    """If the rebuild + retry still fails with a transport error we
    surface ``TransientEmailError`` so the outbox can back off."""
    backend = GmailBackend()
    backend._email_address = "assistant@example.com"
    backend._service = _FlakyGmailService(
        failures=2, exc=ssl.SSLError("record layer failure")
    )

    async def fake_rebuild() -> None:
        # Reinstall a still-broken service — both attempts must fail to
        # reach the TransientEmailError path.
        backend._service = _FlakyGmailService(
            failures=1, exc=ssl.SSLError("record layer failure")
        )

    backend._rebuild_service = fake_rebuild  # type: ignore[method-assign]

    with pytest.raises(TransientEmailError, match="record layer failure"):
        await backend.send(
            to=[EmailAddress(email="customer@example.com")],
            subject="Hello",
            body_html="<p>Hi</p>",
        )


@pytest.mark.asyncio
async def test_send_raises_transient_on_5xx_http_error() -> None:
    """A 503 HttpError from googleapiclient is transient — the backend
    should translate it to ``TransientEmailError``."""

    class _FakeResp:
        def __init__(self, status: int) -> None:
            self.status = status

    class _HttpError(Exception):
        def __init__(self, status: int) -> None:
            super().__init__(f"HttpError {status}")
            self.resp = _FakeResp(status)

    # Force the class name lookup in _is_transient_http_error to match.
    _HttpError.__name__ = "HttpError"

    class _BoomSendRequest:
        def execute(self) -> dict[str, str]:
            raise _HttpError(503)

    class _BoomMessages:
        def send(self, userId: str, body: dict[str, Any]) -> _BoomSendRequest:  # noqa: N803
            return _BoomSendRequest()

    class _BoomUsers:
        def messages(self) -> _BoomMessages:
            return _BoomMessages()

    class _BoomService:
        def users(self) -> _BoomUsers:
            return _BoomUsers()

    backend = GmailBackend()
    backend._email_address = "assistant@example.com"
    backend._service = _BoomService()

    with pytest.raises(TransientEmailError, match="503"):
        await backend.send(
            to=[EmailAddress(email="customer@example.com")],
            subject="Hello",
            body_html="<p>Hi</p>",
        )
