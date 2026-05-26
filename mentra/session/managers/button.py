"""Button-press manager — surfaces physical button events.

Glasses can have multiple buttons (left/right temple, side rocker).
``button_id`` identifies which one; ``press_type`` distinguishes
short vs long presses. Gilbert uses short-press-on-side as the
"wake gesture" for explicit AI dispatch when the user doesn't
want voice activation.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from gilbert.interfaces.mentra import ButtonPress

from ...protocol.streams import StreamType
from .base import ManagerDeps

logger = logging.getLogger(__name__)


ButtonHandler = Callable[[ButtonPress], Awaitable[None]]


class ButtonManager:
    """Subscribe to and surface ``button_press`` stream events."""

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps
        self._handlers: list[ButtonHandler] = []
        self._registered = False
        self._cleanup: Callable[[], None] | None = None

    def on_button_press(
        self, handler: ButtonHandler
    ) -> Callable[[], None]:
        self._handlers.append(handler)
        self._ensure_subscribed()

        def _unsub() -> None:
            try:
                self._handlers.remove(handler)
            except ValueError:
                pass
            if not self._handlers:
                self._unsubscribe()

        return _unsub

    def _ensure_subscribed(self) -> None:
        if self._registered:
            return
        self._cleanup = self._deps.register_stream_handler(
            StreamType.BUTTON_PRESS.value,
            self._dispatch,
        )
        self._deps.add_subscription(StreamType.BUTTON_PRESS.value)
        self._registered = True

    def _unsubscribe(self) -> None:
        if not self._registered:
            return
        self._deps.remove_subscription(StreamType.BUTTON_PRESS.value)
        if self._cleanup is not None:
            self._cleanup()
        self._cleanup = None
        self._registered = False

    async def _dispatch(self, stream_type: str, data: dict[str, Any]) -> None:
        parsed = ButtonPress(
            button_id=str(data.get("buttonId") or ""),
            press_type=str(data.get("pressType") or "short"),
        )
        for handler in list(self._handlers):
            try:
                await handler(parsed)
            except Exception:
                logger.exception("Mentra button handler raised")
