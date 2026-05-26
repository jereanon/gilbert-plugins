"""Outbound frame builders + inbound frame parsing helpers.

The Mentra protocol is JSON-over-WS — every text frame is a single
JSON object with a top-level ``type`` field. This module is the one
place where the wire JSON shape is defined; the rest of the plugin
talks in terms of typed dataclasses.

Frame shapes match the upstream TS SDK exactly (camelCase on the
wire, snake_case in Python).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .layouts import Layout, ViewType, layout_to_dict
from .message_types import AppToCloudMessageType

__all__ = [
    "SDK_VERSION",
    "build_connection_init",
    "build_dashboard_content_update",
    "build_display_request",
    "build_reconnect",
    "build_subscription_update",
    "encode_frame",
    "parse_frame",
]


# Identifies our client to the cloud for telemetry / feature gating.
# Mirrors what the upstream SDK sends; the `-py` suffix lets Mentra
# distinguish Python ports if/when they show up in their dashboards.
SDK_VERSION = "3.0.0-py.1"


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def build_connection_init(
    *, package_name: str, api_key: str
) -> dict[str, Any]:
    """First frame after WS open. Cloud responds with
    ``CONNECTION_ACK`` carrying settings + capabilities (or
    ``CONNECTION_ERROR`` if auth failed)."""
    return {
        "type": AppToCloudMessageType.CONNECTION_INIT.value,
        "packageName": package_name,
        "apiKey": api_key,
        "sdkVersion": SDK_VERSION,
        "timestamp": _iso_now(),
    }


def build_reconnect(*, session_id: str) -> dict[str, Any]:
    """First frame after a WS reconnect (vs initial connect). Cloud
    answers with ``RECONNECT_ACK`` / ``RECONNECT_REJECTED`` /
    ``RECONNECT_DEFERRED``. The session id is what tells the cloud
    we're the same logical session."""
    return {
        "type": AppToCloudMessageType.RECONNECT.value,
        "sessionId": session_id,
        "sdkVersion": SDK_VERSION,
        "timestamp": _iso_now(),
    }


def build_subscription_update(
    *,
    package_name: str,
    session_id: str,
    subscriptions: Iterable[str],
) -> dict[str, Any]:
    """Tell the cloud which streams we want forwarded over the WS.

    Subscriptions are stream-type strings (``"transcription"``,
    ``"button_press"``, ``"transcription:en-US"`` for language-tagged
    variants). The cloud honors this list strictly — anything not
    subscribed-to won't reach our app.
    """
    return {
        "type": AppToCloudMessageType.SUBSCRIPTION_UPDATE.value,
        "packageName": package_name,
        "sessionId": session_id,
        "subscriptions": list(subscriptions),
        "timestamp": _iso_now(),
    }


def build_display_request(
    *,
    package_name: str,
    layout: Layout,
    view: ViewType = ViewType.MAIN,
    duration_ms: int | None = None,
    force_display: bool = False,
) -> dict[str, Any]:
    """Render a layout to one of the glasses' display surfaces.

    ``duration_ms`` is the auto-clear timeout; ``None`` means the
    layout stays up until replaced. ``force_display=True`` bypasses
    the cloud's coalesce-when-busy heuristic — use sparingly for
    high-priority alerts.
    """
    out: dict[str, Any] = {
        "type": AppToCloudMessageType.DISPLAY_REQUEST.value,
        "packageName": package_name,
        "view": view.value,
        "layout": layout_to_dict(layout),
        "timestamp": _iso_now(),
    }
    if duration_ms is not None:
        out["durationMs"] = int(duration_ms)
    if force_display:
        out["forceDisplay"] = True
    return out


def build_dashboard_content_update(
    *,
    package_name: str,
    session_id: str,
    content: str,
    modes: Iterable[str] = ("main",),
) -> dict[str, Any]:
    """Write a string to the persistent dashboard surface.

    ``modes`` is which dashboard mode(s) this content applies to —
    ``"main"`` is the always-visible glance line; ``"expanded"`` is
    the multi-line view the user sees when they look up.
    """
    return {
        "type": AppToCloudMessageType.DASHBOARD_CONTENT_UPDATE.value,
        "packageName": package_name,
        "sessionId": session_id,
        "content": content,
        "modes": list(modes),
        "timestamp": _iso_now(),
    }


def encode_frame(frame: dict[str, Any]) -> str:
    """Serialize a frame dict to the wire string. Centralized so we
    can swap encoders (e.g. orjson) without chasing call sites."""
    return json.dumps(frame, separators=(",", ":"))


def parse_frame(raw: str) -> dict[str, Any]:
    """Parse an inbound WS text frame into a dict.

    Returns ``{}`` on malformed JSON rather than raising — the
    session loop logs and skips bad frames so one malformed message
    doesn't kill the whole connection.
    """
    try:
        out = json.loads(raw)
    except Exception:
        return {}
    return out if isinstance(out, dict) else {}
