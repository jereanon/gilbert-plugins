"""Stream-type discriminators for ``DATA_STREAM`` envelopes.

When the cloud sends ``{"type": "data_stream", "streamType": "...",
"data": {...}}``, the ``streamType`` field tells the session which
manager owns the inner payload. This is the SECOND level of dispatch
after the top-level message type — see ``DataStreamRouter`` in
``session/session.py``.

Mirrors upstream ``types/streams.ts``. The transcription / translation
stream types support a colon-suffix language hint
(``transcription:en-US``); the router treats those as prefix-matches
against ``TRANSCRIPTION`` / ``TRANSLATION``.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["StreamType"]


class StreamType(StrEnum):
    """Every named stream an app can subscribe to.

    Subscriptions are sent via ``subscription_update`` outbound; the
    cloud filters which ``DATA_STREAM`` messages it forwards based
    on this list. Subscribing to ``ALL`` / ``WILDCARD`` is allowed
    but mostly used for debugging — production code should
    subscribe narrowly to keep WS traffic low.
    """

    # Hardware (IMU, buttons, batteries, GPS)
    BUTTON_PRESS = "button_press"
    HEAD_POSITION = "head_position"
    TOUCH_EVENT = "touch_event"
    GLASSES_BATTERY_UPDATE = "glasses_battery_update"
    PHONE_BATTERY_UPDATE = "phone_battery_update"
    GLASSES_CONNECTION_STATE = "glasses_connection_state"
    LOCATION_UPDATE = "location_update"
    LOCATION_STREAM = "location_stream"

    # Audio
    TRANSCRIPTION = "transcription"
    TRANSLATION = "translation"
    VAD = "VAD"
    AUDIO_CHUNK = "audio_chunk"

    # Phone-side
    PHONE_NOTIFICATION = "phone_notification"
    PHONE_NOTIFICATION_DISMISSED = "phone_notification_dismissed"
    CALENDAR_EVENT = "calendar_event"

    # System
    START_APP = "start_app"
    STOP_APP = "stop_app"
    OPEN_DASHBOARD = "open_dashboard"
    CORE_STATUS_UPDATE = "core_status_update"

    # Camera / streaming
    VIDEO = "video"
    PHOTO_REQUEST = "photo_request"
    PHOTO_RESPONSE = "photo_response"
    PHOTO_TAKEN = "photo_taken"
    STREAM_STATUS = "stream_status"
    MANAGED_STREAM_STATUS = "managed_stream_status"

    # Wildcards
    ALL = "all"
    WILDCARD = "*"

    # Settings round-trip
    MENTRAOS_SETTINGS_UPDATE_REQUEST = "settings_update_request"
