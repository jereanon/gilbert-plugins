"""Display layout payloads.

Outbound ``DISPLAY_REQUEST`` messages carry a ``layout`` field that
must be one of these dataclass shapes. The cloud routes the rendered
layout to either ``ViewType.MAIN`` (foreground full-screen) or
``ViewType.DASHBOARD`` (persistent glanceable area).

Cloud-side rate limit: layout updates are throttled to 1 per 300 ms
to keep displays in sync — call ``DisplayManager`` rapidly and the
plugin will silently coalesce excess writes.

Mirrors upstream ``types/layouts.ts`` exactly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BitmapAnimation",
    "BitmapView",
    "ClearView",
    "DashboardCard",
    "DoubleTextWall",
    "Layout",
    "LayoutType",
    "ReferenceCard",
    "TextWall",
    "ViewType",
    "layout_to_dict",
]


class LayoutType(StrEnum):
    """Discriminator on layout payloads."""

    TEXT_WALL = "text_wall"
    DOUBLE_TEXT_WALL = "double_text_wall"
    DASHBOARD_CARD = "dashboard_card"
    REFERENCE_CARD = "reference_card"
    BITMAP_VIEW = "bitmap_view"
    BITMAP_ANIMATION = "bitmap_animation"
    CLEAR_VIEW = "clear_view"


class ViewType(StrEnum):
    """Which display surface to render into.

    ``MAIN`` is the foreground full-screen; ``DASHBOARD`` is the
    persistent area the user sees when they look up (only mode where
    the dashboard mirror updates).
    """

    MAIN = "main"
    DASHBOARD = "dashboard"


@dataclass
class TextWall:
    """Single block of text. The most common layout — use for prose,
    AI responses, notifications. Supports ``\\n`` for line breaks."""

    text: str
    layout_type: str = LayoutType.TEXT_WALL.value


@dataclass
class DoubleTextWall:
    """Two text blocks stacked vertically. Good for header/body or
    question/answer pairs."""

    top_text: str
    bottom_text: str
    layout_type: str = LayoutType.DOUBLE_TEXT_WALL.value


@dataclass
class ReferenceCard:
    """Titled card — best for structured info (meeting details,
    contact, search result). The title gets visual emphasis."""

    title: str
    text: str
    layout_type: str = LayoutType.REFERENCE_CARD.value


@dataclass
class DashboardCard:
    """Two-column layout sized for the persistent dashboard area.
    Use when contributing to ``ViewType.DASHBOARD``."""

    left_text: str
    right_text: str
    layout_type: str = LayoutType.DASHBOARD_CARD.value


@dataclass
class BitmapView:
    """Base64-encoded image bytes. The wire format the SDK accepts
    is a single ``data`` string; the cloud handles conversion to the
    glasses' native bitmap format."""

    data: str
    layout_type: str = LayoutType.BITMAP_VIEW.value


@dataclass
class BitmapAnimation:
    """Animated bitmap sequence — batched frames timed iOS-side for
    smooth playback. ``interval`` is per-frame ms."""

    frames: list[str]
    interval: int
    repeat: bool = False
    layout_type: str = LayoutType.BITMAP_ANIMATION.value


@dataclass
class ClearView:
    """Wipe the display. No payload fields."""

    layout_type: str = LayoutType.CLEAR_VIEW.value


# Union type — every concrete layout the SDK accepts.
Layout = (
    TextWall
    | DoubleTextWall
    | ReferenceCard
    | DashboardCard
    | BitmapView
    | BitmapAnimation
    | ClearView
)


def layout_to_dict(layout: Layout) -> dict[str, object]:
    """Serialize a layout dataclass to the wire JSON shape.

    The wire format uses camelCase keys (``layoutType``, ``topText``,
    ``leftText``, …) — Python uses snake_case internally. Translation
    happens here in one place so the rest of the code is idiomatic.
    """
    if isinstance(layout, TextWall):
        return {"layoutType": layout.layout_type, "text": layout.text}
    if isinstance(layout, DoubleTextWall):
        return {
            "layoutType": layout.layout_type,
            "topText": layout.top_text,
            "bottomText": layout.bottom_text,
        }
    if isinstance(layout, ReferenceCard):
        return {
            "layoutType": layout.layout_type,
            "title": layout.title,
            "text": layout.text,
        }
    if isinstance(layout, DashboardCard):
        return {
            "layoutType": layout.layout_type,
            "leftText": layout.left_text,
            "rightText": layout.right_text,
        }
    if isinstance(layout, BitmapView):
        return {"layoutType": layout.layout_type, "data": layout.data}
    if isinstance(layout, BitmapAnimation):
        return {
            "layoutType": layout.layout_type,
            "frames": list(layout.frames),
            "interval": layout.interval,
            "repeat": layout.repeat,
        }
    if isinstance(layout, ClearView):
        return {"layoutType": layout.layout_type}
    raise TypeError(f"Unknown layout type: {type(layout).__name__}")
