"""Display manager — outbound layout commands.

Convenience methods over the raw ``DISPLAY_REQUEST`` frame. App
code calls ``session.display.show_text_wall("hello")`` and this
manager builds the right layout dataclass + ships the frame.

Cloud rate-limits these to 1 per 300 ms — sending faster than
that just silently coalesces.
"""

from __future__ import annotations

from ...protocol.frames import build_display_request
from ...protocol.layouts import (
    BitmapView,
    DoubleTextWall,
    Layout,
    ReferenceCard,
    TextWall,
    ViewType,
)
from .base import ManagerDeps


class DisplayManager:
    """Outbound display API. All methods are async — they ship the
    frame as soon as the underlying transport accepts it.

    Each method takes an optional ``duration_ms`` — if set, the
    cloud auto-clears the layout after that timeout. ``None`` means
    the layout persists until replaced.
    """

    def __init__(self, deps: ManagerDeps) -> None:
        self._deps = deps

    async def show_text_wall(
        self,
        text: str,
        *,
        duration_ms: int | None = None,
        force: bool = False,
    ) -> None:
        await self._show(TextWall(text=text), duration_ms=duration_ms, force=force)

    async def show_double_text_wall(
        self,
        *,
        top: str,
        bottom: str,
        duration_ms: int | None = None,
        force: bool = False,
    ) -> None:
        await self._show(
            DoubleTextWall(top_text=top, bottom_text=bottom),
            duration_ms=duration_ms,
            force=force,
        )

    async def show_reference_card(
        self,
        *,
        title: str,
        text: str,
        duration_ms: int | None = None,
        force: bool = False,
    ) -> None:
        await self._show(
            ReferenceCard(title=title, text=text),
            duration_ms=duration_ms,
            force=force,
        )

    async def show_bitmap(
        self,
        *,
        base64_data: str,
        duration_ms: int | None = None,
    ) -> None:
        await self._show(BitmapView(data=base64_data), duration_ms=duration_ms)

    async def clear(self) -> None:
        # Mentra has no explicit "clear" command — sending a TextWall
        # with an empty string is the documented workaround. We could
        # also send the ClearView layout but the cloud's coverage of
        # that variant is patchy across device models.
        await self.show_text_wall("")

    async def _show(
        self,
        layout: Layout,
        *,
        duration_ms: int | None = None,
        force: bool = False,
    ) -> None:
        frame = build_display_request(
            package_name=self._deps.package_name,
            layout=layout,
            view=ViewType.MAIN,
            duration_ms=duration_ms,
            force_display=force,
        )
        await self._deps.send_frame(frame)
