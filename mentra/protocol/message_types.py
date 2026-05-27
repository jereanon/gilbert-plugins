"""Top-level message-type discriminators on the Mentra WebSocket
protocol.

Mirrors the upstream ``types/message-types.ts`` exactly — same string
values, same naming. Apps only ever send ``AppToCloud`` messages and
only ever receive ``CloudToApp`` messages; the other two enums are
included for symmetry / debugging when reading packet captures that
include the glasses ↔ cloud half.

Wire format: every JSON message has a top-level ``type`` field set to
one of these enum values. The ``CloudToApp.DATA_STREAM`` envelope
adds a second discriminator (``streamType``) — see ``streams.py``.

The ``tpa_`` prefix on ``CONNECTION_INIT`` / ``CONNECTION_ACK`` / etc.
is a historical artifact ("Third Party App") preserved for wire
compatibility — we don't try to "fix" it.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AppToCloudMessageType",
    "CloudToAppMessageType",
    "CloudToGlassesMessageType",
    "GlassesToCloudMessageType",
]


class AppToCloudMessageType(StrEnum):
    """Messages our app sends to Mentra Cloud."""

    # Lifecycle
    CONNECTION_INIT = "tpa_connection_init"
    RECONNECT = "reconnect"
    OWNERSHIP_RELEASE = "ownership_release"

    # Subscriptions
    SUBSCRIPTION_UPDATE = "subscription_update"

    # Display
    DISPLAY_REQUEST = "display_event"

    # Audio
    AUDIO_PLAY_REQUEST = "audio_play_request"
    AUDIO_STOP_REQUEST = "audio_stop_request"
    AUDIO_STREAM_START = "audio_stream_start"
    AUDIO_STREAM_END = "audio_stream_end"

    # Photo / camera
    PHOTO_REQUEST = "photo_request"
    CAMERA_FOV_SET = "camera_fov_set"

    # Streaming
    STREAM_REQUEST = "stream_request"
    STREAM_STOP = "stream_stop"
    MANAGED_STREAM_REQUEST = "managed_stream_request"
    MANAGED_STREAM_STOP = "managed_stream_stop"
    STREAM_STATUS_CHECK = "stream_status_check"

    # LED
    RGB_LED_CONTROL = "rgb_led_control"

    # Location
    LOCATION_POLL_REQUEST = "location_poll_request"

    # Dashboard
    DASHBOARD_CONTENT_UPDATE = "dashboard_content_update"
    DASHBOARD_MODE_CHANGE = "dashboard_mode_change"
    DASHBOARD_SYSTEM_UPDATE = "dashboard_system_update"

    # WiFi setup helper (Mentra Live)
    REQUEST_WIFI_SETUP = "request_wifi_setup"


class CloudToAppMessageType(StrEnum):
    """Messages Mentra Cloud sends to our app."""

    # Lifecycle responses
    CONNECTION_ACK = "tpa_connection_ack"
    CONNECTION_ERROR = "tpa_connection_error"
    RECONNECT_ACK = "reconnect_ack"
    RECONNECT_REJECTED = "reconnect_rejected"
    RECONNECT_DEFERRED = "reconnect_deferred"

    # Stream data envelope — ``streamType`` field disambiguates payload
    DATA_STREAM = "data_stream"

    # Settings + capabilities
    SETTINGS_UPDATE = "settings_update"
    CAPABILITIES_UPDATE = "capabilities_update"
    DEVICE_STATE_UPDATE = "device_state_update"

    # Lifecycle termination
    APP_STOPPED = "app_stopped"

    # Photo response (delivered out-of-band but the ack arrives via WS)
    PHOTO_RESPONSE = "photo_response"

    # Audio
    AUDIO_PLAY_RESPONSE = "audio_play_response"
    AUDIO_STREAM_READY = "audio_stream_ready"

    # Dashboard state mirror
    DASHBOARD_MODE_CHANGED = "dashboard_mode_changed"
    DASHBOARD_ALWAYS_ON_CHANGED = "dashboard_always_on_changed"

    # Permission / telemetry
    PERMISSION_ERROR = "permission_error"
    REQUEST_TELEMETRY = "request_telemetry"


class GlassesToCloudMessageType(StrEnum):
    """Messages the glasses send to the cloud. Included for symmetry
    when debugging captured streams — our app never sends or receives
    these directly (they're wrapped in ``DATA_STREAM`` envelopes by
    the time they reach us)."""

    CONNECTION_INIT = "connection_init"
    BUTTON_PRESS = "button_press"
    HEAD_POSITION = "head_position"
    LOCATION_UPDATE = "location_update"
    GLASSES_BATTERY_UPDATE = "glasses_battery_update"
    PHOTO_RESPONSE = "photo_response"
    KEEP_ALIVE_ACK = "keep_alive_ack"


class CloudToGlassesMessageType(StrEnum):
    """Directives from the cloud to the glasses. Same caveat as
    ``GlassesToCloudMessageType`` — apps don't see these directly."""

    CONNECTION_ACK = "connection_ack"
    DISPLAY_EVENT = "display_event"
    PHOTO_REQUEST = "photo_request"
    AUDIO_PLAY_REQUEST = "audio_play_request"
    RGB_LED_CONTROL = "rgb_led_control"
