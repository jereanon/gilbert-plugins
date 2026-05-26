"""Dashboard manager — write persistent glanceable content.

The dashboard is the upward-glance surface — it stays visible
while the user is looking up and disappears when they look back
ahead. Use it for ambient status (next meeting, current track,
unread count) rather than transient notifications (those go to
main-view text walls).
"""

from __future__ import annotations

from collections.abc import Iterable

from ...protocol.frames import build_dashboard_content_update
from .base import ManagerDeps


class DashboardManager:
    """Outbound dashboard API. ``write_to_main`` is the single-line
    glance; ``write_to_expanded`` is the multi-line view the user
    sees after the upward tilt."""

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps

    async def write_to_main(self, content: str) -> None:
        await self._write(content, modes=("main",))

    async def write_to_expanded(self, content: str) -> None:
        await self._write(content, modes=("expanded",))

    async def write(
        self, content: str, *, modes: Iterable[str] = ("main",)
    ) -> None:
        """Write to one or more specific modes. Use when the same
        content should appear in both glance and expanded views."""
        await self._write(content, modes=modes)

    async def _write(
        self, content: str, *, modes: Iterable[str]
    ) -> None:
        frame = build_dashboard_content_update(
            package_name=self._deps.package_name,
            session_id=self._deps.get_session_id(),
            content=content,
            modes=modes,
        )
        await self._deps.send_frame(frame)
