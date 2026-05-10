"""Tests for the Frigate MQTT payload → CameraEvent normalization.

Drives ``FrigateMQTT._payload_to_event`` and the ``_parse_sub_label``
helper directly without spinning up an aiomqtt connection.
"""

from __future__ import annotations

from gilbert_plugin_frigate.mqtt_client import FrigateMQTT, _parse_sub_label

from gilbert.interfaces.camera import CameraEventPhase


def _client() -> FrigateMQTT:
    return FrigateMQTT(
        host="localhost",
        port=1883,
        prefix="frigate",
        client_factory=lambda **kw: None,  # not used in normalization tests
    )


def _new_event(after: dict, type_: str = "new") -> dict:
    return {"type": type_, "before": None, "after": after}


def test_new_event_yields_active() -> None:
    c = _client()
    ev = c._payload_to_event(
        _new_event(
            {
                "id": "1730851234.567890-abc",
                "camera": "front_door",
                "label": "person",
                "score": 0.81,
                "start_time": 1730851234.567,
                "current_zones": ["porch"],
                "has_snapshot": True,
                "has_clip": False,
            }
        )
    )
    assert ev is not None
    assert ev.event_id == "1730851234.567890-abc"
    assert ev.phase == CameraEventPhase.ACTIVE
    assert ev.zones == ("porch",)
    assert ev.has_snapshot is True


def test_update_event_yields_active_when_score_changes() -> None:
    c = _client()
    base_after = {
        "id": "evt-x",
        "camera": "porch",
        "label": "person",
        "score": 0.5,
        "start_time": 1730851234.0,
    }
    c._payload_to_event(_new_event(base_after, type_="new"))
    bigger = dict(base_after, score=0.9)
    ev = c._payload_to_event({"type": "update", "after": bigger})
    assert ev is not None
    assert ev.score == 0.9


def test_update_event_dropped_when_no_change() -> None:
    c = _client()
    base = {
        "id": "evt-y",
        "camera": "porch",
        "label": "person",
        "score": 0.5,
        "start_time": 1730851234.0,
    }
    c._payload_to_event(_new_event(base, type_="new"))
    # Identical update — should be deduped.
    out = c._payload_to_event({"type": "update", "after": dict(base)})
    assert out is None


def test_end_event_yields_ended_with_top_score() -> None:
    c = _client()
    after = {
        "id": "evt-z",
        "camera": "porch",
        "label": "package",
        "score": 0.5,
        "top_score": 0.95,
        "start_time": 1730851234.0,
        "end_time": 1730851300.0,
    }
    ev = c._payload_to_event({"type": "end", "after": after})
    assert ev is not None
    assert ev.phase == CameraEventPhase.ENDED
    assert ev.score == 0.95
    assert ev.ended_at == 1730851300000


def test_sub_label_string_form() -> None:
    assert _parse_sub_label("jeff") == "jeff"


def test_sub_label_list_form() -> None:
    assert _parse_sub_label(["jeff", 0.93]) == "jeff"


def test_sub_label_null_form() -> None:
    assert _parse_sub_label(None) == ""


def test_sub_label_unexpected_form_returns_empty() -> None:
    assert _parse_sub_label({"name": "jeff"}) == ""


def test_missing_required_field_dropped() -> None:
    c = _client()
    out = c._payload_to_event(
        {
            "type": "new",
            "after": {"camera": "porch", "label": "person"},
        }
    )
    assert out is None  # no event_id


def test_missing_end_time_handled() -> None:
    c = _client()
    after = {
        "id": "evt-no-end",
        "camera": "porch",
        "label": "person",
        "score": 0.5,
        "top_score": 0.5,
        "start_time": 1730851234.0,
    }
    ev = c._payload_to_event({"type": "end", "after": after})
    assert ev is not None
    assert ev.ended_at == 0


def test_false_positive_dropped() -> None:
    c = _client()
    out = c._payload_to_event(
        {
            "type": "new",
            "after": {
                "id": "evt-fp",
                "camera": "porch",
                "label": "person",
                "false_positive": True,
                "start_time": 0,
            },
        }
    )
    assert out is None


def test_audio_event_no_snapshot() -> None:
    c = _client()
    ev = c._payload_to_event(
        {
            "type": "new",
            "after": {
                "id": "audio-1",
                "camera": "porch",
                "label": "bark",
                "score": 0.7,
                "start_time": 0,
                "has_snapshot": False,
                "has_clip": False,
            },
        }
    )
    assert ev is not None
    assert ev.has_snapshot is False
    assert ev.label == "bark"


def test_update_event_yields_active_when_zones_change_with_unchanged_score() -> None:
    """The dedup check is: small-score-change AND same-zones AND same-snapshot.
    A zone change alone should let the update through even if score
    didn't move."""
    c = _client()
    base_after = {
        "id": "evt-zones",
        "camera": "porch",
        "label": "person",
        "score": 0.5,
        "start_time": 1730851234.0,
        "current_zones": ["porch"],
    }
    c._payload_to_event(_new_event(base_after, type_="new"))
    # Score unchanged, snapshot frame_time unchanged (both absent),
    # but a new zone appeared. Should yield, not dedup.
    new_zones = dict(base_after, current_zones=["porch", "doorway"])
    out = c._payload_to_event({"type": "update", "after": new_zones})
    assert out is not None
    assert out.zones == ("porch", "doorway")


def test_update_event_yields_active_when_snapshot_advances_with_unchanged_score() -> None:
    """A fresh snapshot frame (different ``snapshot.frame_time``) should
    let the update through even with the same score and zones."""
    c = _client()
    base_after = {
        "id": "evt-snap",
        "camera": "porch",
        "label": "person",
        "score": 0.5,
        "start_time": 1730851234.0,
        "current_zones": ["porch"],
        "snapshot": {"frame_time": 1730851234.5},
    }
    c._payload_to_event(_new_event(base_after, type_="new"))
    advanced = dict(base_after, snapshot={"frame_time": 1730851235.0})
    out = c._payload_to_event({"type": "update", "after": advanced})
    assert out is not None


def test_update_event_dropped_when_zones_match_and_snapshot_unchanged_and_small_score_delta() -> None:
    """All three conditions met → dedup. This is the AND of the three
    OR-branches above; a regression that drops any of the OR conditions
    would either over-yield or over-dedup."""
    c = _client()
    base_after = {
        "id": "evt-andset",
        "camera": "porch",
        "label": "person",
        "score": 0.5,
        "start_time": 1730851234.0,
        "current_zones": ["porch"],
        "snapshot": {"frame_time": 1730851234.5},
    }
    c._payload_to_event(_new_event(base_after, type_="new"))
    # Score moves by less than threshold (0.05), zones unchanged,
    # snapshot frame_time unchanged.
    minor_change = dict(
        base_after,
        score=0.51,  # delta 0.01 < 0.05 threshold
    )
    out = c._payload_to_event({"type": "update", "after": minor_change})
    assert out is None
