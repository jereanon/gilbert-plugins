"""Transport abstraction for Mentra sessions.

Mirrors the upstream ``Transport`` interface in
``transport/Transport.ts`` — an abstract send/receive pair with text
and binary channels. The session layer (``session.py``) talks to
this interface and is unaware whether it's running against a real
``websockets`` connection or a fake test harness.

``WebSocketTransport`` is the production implementation, layered on
``websockets`` (already a Gilbert dep — same library the phone
brain uses). Tests use a fake transport that lets the harness
inject inbound frames and capture outbound ones.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import IntEnum
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)


__all__ = [
    "Transport",
    "TransportState",
    "WebSocketTransport",
]


class TransportState(IntEnum):
    """Matches the values the upstream SDK's TransportState enum uses
    (which itself mirrors the standard ``WebSocket.readyState``
    constants). Callers that care about the live state of the
    transport can compare against these."""

    CONNECTING = 0
    OPEN = 1
    CLOSING = 2
    CLOSED = 3


# Handler signatures — async so handlers can do real work (persistence,
# AI dispatch, …) without having to bounce back through a queue. The
# session layer wraps user-provided sync handlers in a coroutine.
TextHandler = Callable[[str], Awaitable[None]]
BinaryHandler = Callable[[bytes], Awaitable[None]]
CloseHandler = Callable[[int, str], Awaitable[None]]
ErrorHandler = Callable[[BaseException], Awaitable[None]]


class Transport(ABC):
    """Abstract transport — send/receive primitives, no protocol
    knowledge. Concretes implement the actual wire layer."""

    @property
    @abstractmethod
    def ready_state(self) -> TransportState:
        ...

    @property
    def is_open(self) -> bool:
        return self.ready_state is TransportState.OPEN

    @abstractmethod
    async def connect(self) -> None:
        """Open the underlying connection. Resolves when ready to send;
        raises on failure or timeout."""

    @abstractmethod
    async def send(self, data: str) -> None:
        """Send a text frame. Silently drops if the transport isn't
        open — callers that care should check ``is_open`` first."""

    @abstractmethod
    async def send_binary(self, data: bytes) -> None:
        """Send a binary frame (used for PCM audio chunks)."""

    @abstractmethod
    async def close(self, code: int = 1000, reason: str = "") -> None:
        """Close gracefully."""

    @abstractmethod
    def on_text(self, handler: TextHandler) -> None:
        """Register the text-frame handler. Last registration wins —
        mirrors the upstream SDK's single-handler-per-event model."""

    @abstractmethod
    def on_binary(self, handler: BinaryHandler) -> None:
        ...

    @abstractmethod
    def on_close(self, handler: CloseHandler) -> None:
        ...

    @abstractmethod
    def on_error(self, handler: ErrorHandler) -> None:
        ...


class WebSocketTransport(Transport):
    """``Transport`` over the ``websockets`` library.

    The Mentra protocol authenticates via HTTP headers on the upgrade
    request (``x-api-key`` + correlation headers). The session id and
    user id are echoed in those headers as well as in the first JSON
    frame — the cloud uses the headers for routing decisions and the
    JSON body for app-level identification.

    Reconnect logic lives in the session layer, NOT here — the
    transport just reports closures and lets the session decide
    whether to dial again.
    """

    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        connect_timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._headers = dict(headers or {})
        self._connect_timeout = connect_timeout
        self._ws: ClientConnection | None = None
        self._state = TransportState.CLOSED
        self._reader_task: asyncio.Task[None] | None = None
        self._on_text: TextHandler | None = None
        self._on_binary: BinaryHandler | None = None
        self._on_close: CloseHandler | None = None
        self._on_error: ErrorHandler | None = None

    # ── Transport interface ────────────────────────────────────────

    @property
    def ready_state(self) -> TransportState:
        return self._state

    async def connect(self) -> None:
        if self._state is TransportState.OPEN:
            return
        self._state = TransportState.CONNECTING
        try:
            # ``connect`` is the async-context-manager form, but its
            # underlying ``ClientConnection`` is also awaitable as a
            # plain coroutine via the helper exported below. We use
            # the connection form so we can store + reuse the socket.
            self._ws = await asyncio.wait_for(
                ws_connect(
                    self._url,
                    additional_headers=self._headers,
                    open_timeout=self._connect_timeout,
                ),
                timeout=self._connect_timeout,
            )
        except TimeoutError as exc:
            self._state = TransportState.CLOSED
            await self._dispatch_error(exc)
            raise
        except Exception as exc:
            self._state = TransportState.CLOSED
            await self._dispatch_error(exc)
            raise
        self._state = TransportState.OPEN
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="mentra-ws-reader"
        )

    async def send(self, data: str) -> None:
        if self._ws is None or self._state is not TransportState.OPEN:
            return
        try:
            await self._ws.send(data)
        except Exception:
            # Mirror the TS SDK: send failures on closing sockets are
            # expected and shouldn't propagate. The reader loop will
            # surface the close event through the proper channel.
            logger.debug("Mentra WS send raised; treating as closing")

    async def send_binary(self, data: bytes) -> None:
        if self._ws is None or self._state is not TransportState.OPEN:
            return
        try:
            await self._ws.send(data)
        except Exception:
            logger.debug("Mentra WS binary send raised; treating as closing")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self._ws is None:
            self._state = TransportState.CLOSED
            return
        if self._state in (TransportState.CLOSING, TransportState.CLOSED):
            return
        self._state = TransportState.CLOSING
        try:
            await self._ws.close(code=code, reason=reason)
        except Exception:
            pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        self._state = TransportState.CLOSED

    def on_text(self, handler: TextHandler) -> None:
        self._on_text = handler

    def on_binary(self, handler: BinaryHandler) -> None:
        self._on_binary = handler

    def on_close(self, handler: CloseHandler) -> None:
        self._on_close = handler

    def on_error(self, handler: ErrorHandler) -> None:
        self._on_error = handler

    # ── Internal ───────────────────────────────────────────────────

    async def _read_loop(self) -> None:
        """Pump messages off the WS into the registered handlers."""
        assert self._ws is not None
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    await self._dispatch_text(message)
                elif isinstance(message, (bytes, bytearray, memoryview)):
                    await self._dispatch_binary(bytes(message))
                # ``websockets`` only yields str or bytes — no need
                # for an else branch.
        except websockets.exceptions.ConnectionClosed as exc:
            code = getattr(exc, "code", 1006)
            reason = getattr(exc, "reason", "") or ""
            self._state = TransportState.CLOSED
            await self._dispatch_close(int(code), str(reason))
        except Exception as exc:
            self._state = TransportState.CLOSED
            await self._dispatch_error(exc)

    async def _dispatch_text(self, raw: str) -> None:
        if self._on_text is None:
            return
        try:
            await self._on_text(raw)
        except Exception:
            logger.exception("Mentra WS text handler raised")

    async def _dispatch_binary(self, data: bytes) -> None:
        if self._on_binary is None:
            return
        try:
            await self._on_binary(data)
        except Exception:
            logger.exception("Mentra WS binary handler raised")

    async def _dispatch_close(self, code: int, reason: str) -> None:
        if self._on_close is None:
            return
        try:
            await self._on_close(code, reason)
        except Exception:
            logger.exception("Mentra WS close handler raised")

    async def _dispatch_error(self, exc: BaseException) -> None:
        if self._on_error is None:
            return
        try:
            await self._on_error(exc)
        except Exception:
            logger.exception("Mentra WS error handler raised")

    # ── Test helpers ───────────────────────────────────────────────
    #
    # Exposed so the test harness can drive the reader loop without
    # standing up a real WS server. Not part of the public Transport
    # contract; tests cast to WebSocketTransport.

    def _inject_text_for_tests(self, raw: str) -> Awaitable[None]:
        return self._dispatch_text(raw)


# Re-export so import-time references stay stable even if we swap
# implementations down the road.
def _websockets_version_check() -> Any:
    """Sanity-check the installed ``websockets`` exposes the
    asyncio-client API we depend on. Called at import time so a
    version mismatch surfaces immediately rather than at the first
    session open."""
    return ws_connect  # noqa: F841 — presence is the check


_websockets_version_check()
