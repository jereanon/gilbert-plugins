"""Shared dependency container for per-feature managers.

Every manager needs the same plumbing — a way to send outbound
frames, register inbound handlers, and add stream subscriptions —
but the session ties them together. Rather than pass five
arguments to every manager constructor we collect them in a
dataclass and hand it through.

Mirrors the upstream SDK's ``ManagerDeps`` interface.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Sub-callable signatures the session exposes to managers.
SendFrame = Callable[[dict[str, Any]], Awaitable[None]]
AddSubscription = Callable[[str], None]
RemoveSubscription = Callable[[str], None]
RegisterMessageHandler = Callable[
    [str, Callable[[dict[str, Any]], Awaitable[None]]],
    Callable[[], None],
]
RegisterStreamHandler = Callable[
    [str, Callable[[str, dict[str, Any]], Awaitable[None]]],
    Callable[[], None],
]


@dataclass
class ManagerDeps:
    """Bundle of session-owned hooks every manager needs.

    Managers don't see the session directly — they operate against
    these primitives. Keeps the test surface tight (you can drive
    one manager in isolation by stubbing the four callables).
    """

    package_name: str
    get_session_id: Callable[[], str]
    send_frame: SendFrame
    add_subscription: AddSubscription
    remove_subscription: RemoveSubscription
    register_message_handler: RegisterMessageHandler
    register_stream_handler: RegisterStreamHandler
