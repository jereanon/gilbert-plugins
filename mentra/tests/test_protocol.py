"""Protocol layer tests — frame encoders + layouts + parser
round-trips. Pure protocol checks, no I/O."""

from __future__ import annotations

import json

import pytest


def test_message_type_enums_have_expected_wire_values() -> None:
    """Enum string values are the wire contract — drift here means
    we silently stop talking to the cloud."""
    from gilbert_plugin_mentra.protocol.message_types import (
        AppToCloudMessageType,
        CloudToAppMessageType,
    )

    assert AppToCloudMessageType.CONNECTION_INIT.value == "tpa_connection_init"
    assert AppToCloudMessageType.SUBSCRIPTION_UPDATE.value == "subscription_update"
    assert AppToCloudMessageType.DISPLAY_REQUEST.value == "display_event"
    assert (
        AppToCloudMessageType.DASHBOARD_CONTENT_UPDATE.value
        == "dashboard_content_update"
    )
    assert AppToCloudMessageType.AUDIO_PLAY_REQUEST.value == "audio_play_request"

    assert CloudToAppMessageType.CONNECTION_ACK.value == "tpa_connection_ack"
    assert CloudToAppMessageType.DATA_STREAM.value == "data_stream"
    assert CloudToAppMessageType.APP_STOPPED.value == "app_stopped"


def test_stream_type_enum_values_match_upstream() -> None:
    from gilbert_plugin_mentra.protocol.streams import StreamType

    assert StreamType.TRANSCRIPTION.value == "transcription"
    assert StreamType.BUTTON_PRESS.value == "button_press"
    assert StreamType.HEAD_POSITION.value == "head_position"
    assert StreamType.LOCATION_UPDATE.value == "location_update"


def test_build_connection_init_shape() -> None:
    """First frame after WS open must carry packageName, apiKey,
    sdkVersion + a timestamp — anything missing and the cloud
    rejects with CONNECTION_ERROR."""
    from gilbert_plugin_mentra.protocol.frames import build_connection_init

    frame = build_connection_init(
        package_name="com.example.gilbert", api_key="key_test"
    )
    assert frame["type"] == "tpa_connection_init"
    assert frame["packageName"] == "com.example.gilbert"
    assert frame["apiKey"] == "key_test"
    assert frame["sdkVersion"]  # non-empty
    assert frame["timestamp"]


def test_build_subscription_update_serializes_strings() -> None:
    from gilbert_plugin_mentra.protocol.frames import (
        build_subscription_update,
        encode_frame,
    )

    frame = build_subscription_update(
        package_name="com.example.gilbert",
        session_id="sess_123",
        subscriptions=["transcription", "button_press"],
    )
    assert frame["type"] == "subscription_update"
    assert frame["sessionId"] == "sess_123"
    assert frame["subscriptions"] == ["transcription", "button_press"]
    # Wire shape — must round-trip cleanly through JSON.
    decoded = json.loads(encode_frame(frame))
    assert decoded == frame


def test_layout_to_dict_text_wall() -> None:
    from gilbert_plugin_mentra.protocol.layouts import (
        TextWall,
        layout_to_dict,
    )

    out = layout_to_dict(TextWall(text="hello"))
    assert out == {"layoutType": "text_wall", "text": "hello"}


def test_layout_to_dict_double_text_wall_camelcase() -> None:
    """Snake-case dataclass fields → camelCase wire keys (the upstream
    SDK uses camelCase; mismatching breaks the cloud-side parser)."""
    from gilbert_plugin_mentra.protocol.layouts import (
        DoubleTextWall,
        layout_to_dict,
    )

    out = layout_to_dict(DoubleTextWall(top_text="hi", bottom_text="bye"))
    assert out == {
        "layoutType": "double_text_wall",
        "topText": "hi",
        "bottomText": "bye",
    }


def test_layout_to_dict_reference_card() -> None:
    from gilbert_plugin_mentra.protocol.layouts import (
        ReferenceCard,
        layout_to_dict,
    )

    out = layout_to_dict(ReferenceCard(title="Meeting", text="3pm tomorrow"))
    assert out == {
        "layoutType": "reference_card",
        "title": "Meeting",
        "text": "3pm tomorrow",
    }


def test_layout_to_dict_dashboard_card() -> None:
    from gilbert_plugin_mentra.protocol.layouts import (
        DashboardCard,
        layout_to_dict,
    )

    out = layout_to_dict(DashboardCard(left_text="🎵", right_text="now playing"))
    assert out == {
        "layoutType": "dashboard_card",
        "leftText": "🎵",
        "rightText": "now playing",
    }


def test_build_display_request_omits_duration_when_unset() -> None:
    """``durationMs`` is the auto-clear timeout; omitting it means
    the layout persists until replaced. Test we don't accidentally
    send ``durationMs=null`` which the cloud would reject."""
    from gilbert_plugin_mentra.protocol.frames import build_display_request
    from gilbert_plugin_mentra.protocol.layouts import TextWall

    frame = build_display_request(
        package_name="com.example.gilbert",
        layout=TextWall(text="hi"),
    )
    assert "durationMs" not in frame
    assert frame["view"] == "main"


def test_build_display_request_force_display_only_when_set() -> None:
    from gilbert_plugin_mentra.protocol.frames import build_display_request
    from gilbert_plugin_mentra.protocol.layouts import TextWall

    frame = build_display_request(
        package_name="com.example.gilbert",
        layout=TextWall(text="hi"),
        duration_ms=5000,
        force_display=True,
    )
    assert frame["durationMs"] == 5000
    assert frame["forceDisplay"] is True


def test_parse_frame_rejects_non_object_payloads() -> None:
    from gilbert_plugin_mentra.protocol.frames import parse_frame

    assert parse_frame("[]") == {}  # list at top level
    assert parse_frame("null") == {}
    assert parse_frame("not json at all") == {}
    assert parse_frame('{"type":"foo"}') == {"type": "foo"}


def test_layout_to_dict_unknown_type_raises() -> None:
    """If a future layout type gets added but layout_to_dict isn't
    updated, fail loudly rather than silently shipping a malformed
    frame."""
    from gilbert_plugin_mentra.protocol.layouts import layout_to_dict

    class _FakeLayout:
        layout_type = "future_layout"

    with pytest.raises(TypeError):
        layout_to_dict(_FakeLayout())  # type: ignore[arg-type]
