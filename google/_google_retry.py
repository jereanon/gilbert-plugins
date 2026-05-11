"""Shared transport-error retry helper for Google API backends.

google-api-python-client wraps an ``httplib2.Http`` that keeps long-lived
TLS sockets. The upstream edge silently drops idle sockets after a while,
and the next ``.execute()`` raises one of ``BrokenPipeError`` /
``ssl.SSLError`` / ``ConnectionResetError`` — the surface for a stale
keep-alive, not a real bug. This helper:

  1. Runs ``build_call(service)`` (which should *construct* — not execute —
     an API request) and ``.execute()``s it inside ``asyncio.to_thread``.
  2. On a transport-flavored exception, calls ``rebuild()`` to install a
     fresh service, then retries the call once.
  3. Re-raises if the second attempt fails too — the caller decides how
     to surface that (e.g. translating into a domain-specific transient
     error for retry budgeting elsewhere).

Backends that need this behaviour cache their credentials on
construction so ``rebuild()`` can build a brand-new service (with a
fresh ``Http``) without re-reading config.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Exceptions that mean "the socket is dead, get a new one."
# ``ssl.SSLError`` covers ``[SSL] record layer failure`` on a kept-alive
# TLS session that the upstream edge silently closed. ``BrokenPipeError``
# and ``ConnectionError`` cover the writes-into-half-closed-socket cases.
TRANSIENT_TRANSPORT_EXCS: tuple[type[BaseException], ...] = (
    BrokenPipeError,
    ConnectionError,
    ssl.SSLError,
    TimeoutError,
)


def is_transient_http_error(exc: BaseException) -> bool:
    """``googleapiclient.errors.HttpError`` with a retryable status code.

    Matched by class name to avoid importing googleapiclient at module
    scope — it's a heavy dep and only relevant when a Google backend is
    actually loaded.
    """
    if type(exc).__name__ != "HttpError":
        return False
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None) if resp is not None else None
    return status in (429, 500, 502, 503, 504)


async def call_with_retry(
    *,
    get_service: Callable[[], Any],
    rebuild: Callable[[], Awaitable[None]],
    build_call: Callable[[Any], Any],
    name: str = "google-api",
) -> Any:
    """Run a Google API call with one-shot retry on stale connections.

    ``build_call(svc)`` should construct an API request object (e.g.
    ``svc.users().list(domain="…")``) without calling ``.execute()``.
    The helper invokes ``.execute()`` inside ``asyncio.to_thread`` so
    blocking I/O doesn't stall the event loop. On a transport-flavored
    failure it ``await rebuild()``s the cached service and retries once.

    Transient HTTP responses (429, 5xx) are NOT retried inline — the
    upstream is unhappy, so spinning here doesn't help. Callers that
    care can detect them via ``is_transient_http_error`` and apply
    their own backoff (e.g. the inbox outbox retry budget).
    """
    svc = get_service()
    try:
        return await asyncio.to_thread(build_call(svc).execute)
    except TRANSIENT_TRANSPORT_EXCS as exc:
        logger.warning(
            "%s transport error (%s: %s) — rebuilding service and retrying once",
            name,
            type(exc).__name__,
            exc,
        )
        await rebuild()
        return await asyncio.to_thread(build_call(get_service()).execute)
