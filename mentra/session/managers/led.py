"""LED manager — RGB indicator control.

Mentra glasses with onboard LEDs (Mentra Live, certain G1 SKUs) accept
``rgb_led_control`` commands to flash colored notifications. The wire
format the upstream SDK uses:

    {"type": "rgb_led_control", "packageName": "...", "sessionId": "...",
     "requestId": "...", "action": "on" | "off",
     "color": "red" | "green" | "blue" | "orange" | "white",
     "ontime": <ms>, "offtime": <ms>, "count": <int>}

LED commands are fire-and-forget — methods return immediately after
shipping the frame. The cloud may emit ``rgb_led_control_response``
events for diagnostics but no app code waits on them in v1.

Gilbert uses the LED for non-display feedback — a quick green flash to
acknowledge a successful tool call when the user's eyes aren't on the
display, a red flash on errors, etc. The exposed enum captures the
five colors the upstream SDK formally supports.
"""

from __future__ import annotations

import logging
import uuid
from enum import StrEnum

from ...protocol.message_types import AppToCloudMessageType
from .base import ManagerDeps

logger = logging.getLogger(__name__)


__all__ = ["LedColor", "LedManager"]


class LedColor(StrEnum):
    """Colors the upstream SDK accepts on the wire. The glasses
    interpret unknown values inconsistently — stick to this enum."""

    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    ORANGE = "orange"
    WHITE = "white"


class LedManager:
    """Fire-and-forget RGB LED control. Three calling styles:

    - ``set_color(color)`` — on for 1s
    - ``set_color(color, duration_ms=500)`` — on for the given duration
    - ``set_color(color, on_time_ms=200, off_time_ms=200, count=3)`` —
      blink pattern
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps

    async def set_color(
        self,
        color: LedColor | str,
        *,
        duration_ms: int = 1000,
        on_time_ms: int | None = None,
        off_time_ms: int = 0,
        count: int = 1,
    ) -> None:
        """Turn the LED on with the given color. ``on_time_ms`` /
        ``off_time_ms`` / ``count`` override the simple
        ``duration_ms`` argument when a blink pattern is wanted."""
        ontime = int(on_time_ms if on_time_ms is not None else duration_ms)
        await self._deps.send_frame(
            {
                "type": AppToCloudMessageType.RGB_LED_CONTROL.value,
                "packageName": self._deps.package_name,
                "sessionId": self._deps.get_session_id(),
                "requestId": _request_id(),
                "action": "on",
                "color": str(color),
                "ontime": ontime,
                "offtime": int(off_time_ms),
                "count": int(count),
            }
        )

    async def solid(
        self, color: LedColor | str, *, duration_ms: int = 1000
    ) -> None:
        """Convenience: one steady-on pulse. Equivalent to
        ``set_color(color, duration_ms=duration_ms)``."""
        await self.set_color(color, duration_ms=duration_ms)

    async def blink(
        self,
        color: LedColor | str,
        *,
        on_time_ms: int = 200,
        off_time_ms: int = 200,
        count: int = 3,
    ) -> None:
        """Convenience: blink pattern with sensible defaults
        (200 ms on / 200 ms off, 3 cycles)."""
        await self.set_color(
            color,
            on_time_ms=on_time_ms,
            off_time_ms=off_time_ms,
            count=count,
        )

    async def turn_off(self) -> None:
        """Force the LED off immediately. Cancels any in-progress
        blink pattern."""
        await self._deps.send_frame(
            {
                "type": AppToCloudMessageType.RGB_LED_CONTROL.value,
                "packageName": self._deps.package_name,
                "sessionId": self._deps.get_session_id(),
                "requestId": _request_id(),
                "action": "off",
            }
        )


def _request_id() -> str:
    return f"led_req_{uuid.uuid4().hex[:16]}"
